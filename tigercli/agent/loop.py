import asyncio
import difflib
import json
import os
import time

from tigercli.agent.permissions import check_permission, format_permission_prompt
from tigercli.agent.skills import describe_skills, resolve_slash_skill
from tigercli.agent.subagents import describe_subagents
from tigercli.agent.tools import TOOL_SCHEMAS, execute_tool, tool_needs_permission
from tigercli.client import call
from tigercli.common.cache import estimate_cost
from tigercli.config import settings
from tigercli.session.messages import build_messages, estimate_message_tokens
from tigercli.session.models import Message, Session, UsageEntry, new_id, now_iso
from tigercli.session.store import SessionStore


def _tool_param_summary(name: str, args: dict) -> str:
    """One-line human summary of a tool call, derived from its arguments.

    Mirrors deepcode-cli's ToolSummary.params: the displayed tool line should
    show WHAT the tool acted on (file path, glob pattern, command, query), not
    just the tool name. Returns "" when nothing meaningful is available.
    """
    if not isinstance(args, dict):
        return ""

    def _first_line(val: str, limit: int = 120) -> str:
        for line in str(val).splitlines():
            s = line.strip()
            if s:
                return s if len(s) <= limit else s[: limit - 1] + "…"
        return ""

    n = name.lower()
    # Tool-specific keys take precedence over generic path keys.
    if "glob" in n and isinstance(args.get("pattern"), str) and args["pattern"].strip():
        return _first_line(args["pattern"])
    if "grep" in n:
        for key in ("regex", "pattern"):
            if isinstance(args.get(key), str) and args[key].strip():
                return _first_line(args[key])
    if n == "bash" and isinstance(args.get("command"), str) and args["command"].strip():
        return _first_line(args["command"])
    if ("search" in n or "web" in n):
        for key in ("search_term", "query", "url"):
            if isinstance(args.get(key), str) and args[key].strip():
                return _first_line(args[key])
    # Path-like tools
    for key in ("file_path", "path", "filename", "dir_path"):
        if isinstance(args.get(key), str) and args[key].strip():
            return _first_line(args[key])
    # Generic pattern / regex (non-glob/grep tools that still take one)
    for key in ("pattern", "regex"):
        if isinstance(args.get(key), str) and args[key].strip():
            return _first_line(args[key])
    # Web / search fallbacks
    if isinstance(args.get("url"), str) and args["url"].strip():
        return _first_line(args["url"])
    for key in ("search_term", "query"):
        if isinstance(args.get(key), str) and args[key].strip():
            return _first_line(args[key])
    # Subagent / skill / generic description
    for key in ("description", "instructions", "skill"):
        if isinstance(args.get(key), str) and args[key].strip():
            return _first_line(args[key])
    # Fallback: first non-empty string argument
    for val in args.values():
        if isinstance(val, str) and val.strip():
            return _first_line(val)
    return ""


def _resolve_diff_path(file_path: str, project_path: str) -> str:
    p = file_path or ""
    if os.path.isabs(p):
        return p
    return os.path.join(project_path or "", p)


def _tool_diff_preview(name: str, args: dict, project_path: str) -> dict | None:
    """Build a unified-diff preview for write/edit tool calls.

    Returns {"path", "diff", "added", "removed"} where diff is a list of lines
    each tagged with a leading marker: " " context, "+" added, "-" removed,
    "@" hunk header. The TUI renders this like modern coding TUIs (Claude Code,
    Codex, Aider) so users see WHAT is being written, not just "ok".
    """
    if not isinstance(args, dict):
        return None
    n = name.lower()
    if n not in {"write", "edit"}:
        return None
    file_path = args.get("filePath") or args.get("file_path") or ""
    if not isinstance(file_path, str) or not file_path.strip():
        return None

    resolved = _resolve_diff_path(file_path, project_path)
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            before_text = f.read()
        existed = True
    except (FileNotFoundError, IsADirectoryError, OSError):
        before_text = ""
        existed = False

    if n == "write":
        after_text = args.get("content")
        if not isinstance(after_text, str):
            return None
    else:  # edit
        old = args.get("oldString")
        new = args.get("newString")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        if not existed:
            after_text = before_text
        elif args.get("replaceAll"):
            after_text = before_text.replace(old, new)
        else:
            after_text = before_text.replace(old, new, 1)

    if before_text == after_text:
        return None

    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    diff_lines: list[str] = []
    added = 0
    removed = 0
    # Cap how much we render so a huge file write doesn't flood the transcript.
    MAX_DIFF_LINES = 200
    for line in difflib.unified_diff(
        before_lines, after_lines, lineterm="", n=3
    ):
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("@@"):
            diff_lines.append("@" + line)
        elif line.startswith("+"):
            added += 1
            diff_lines.append("+" + line[1:])
        elif line.startswith("-"):
            removed += 1
            diff_lines.append("-" + line[1:])
        else:
            diff_lines.append(" " + (line[1:] if line.startswith(" ") else line))
        if len(diff_lines) >= MAX_DIFF_LINES:
            diff_lines.append("@… diff truncated …")
            break

    if not diff_lines:
        return None
    return {
        "path": file_path,
        "diff": diff_lines,
        "added": added,
        "removed": removed,
        "new_file": not existed,
    }


class AgentLoop:
    def __init__(self, store: SessionStore, on_event=None):
        self.store = store
        self._on_event = on_event
        self._pending_permissions: dict[str, asyncio.Future] = {}
        # Decisions that arrived BEFORE the corresponding wait registered its
        # future (UI is fast; the response can race ahead of _wait_for_permission).
        # Buffering them prevents a spurious 5-minute timeout → silent deny.
        self._resolved_permissions: dict[str, bool] = {}
        self._auto_approve = False

    def set_auto_approve(self, value: bool) -> None:
        self._auto_approve = value

    async def _emit(self, event_type: str, data: dict):
        if self._on_event:
            await self._on_event(event_type, data)

    async def run(
        self,
        session: Session,
        user_message: str,
        files: list[tuple[str, str]] | None = None,
        auto_approve: bool = False,
        extra_tools: list[dict] | None = None,
        image_urls: list[str] | None = None,
    ) -> str:
        slash_skill = resolve_slash_skill(user_message, session.project_path)
        if slash_skill is not None:
            user_message = slash_skill

        history = await self.store.get_messages(session.id)

        # Auto-compaction: when the stored conversation reaches the configured
        # token threshold (default 500K of a 1M window, adjustable via
        # /compact_size), summarize the history in place and continue the turn
        # on the compacted context. A failure here is non-fatal — we keep the
        # original history and proceed.
        try:
            history_tokens = estimate_message_tokens([
                {"role": m.role, "content": m.content or ""} for m in history
            ])
            if history and history_tokens >= settings.compact_size:
                await self._emit("token", {"content": (
                    f"\n[Auto-compacting context: ~{history_tokens:,} tokens "
                    f"reached the {settings.compact_size:,}-token limit. "
                    "Summarizing earlier conversation, then continuing…]\n"
                )})
                from tigercli.session.compact import compact_session

                result = await compact_session(
                    store=self.store,
                    session_id=session.id,
                    provider=session.provider,
                    model=session.model,
                )
                await self._emit("compact_complete", {
                    "beforeTokens": result.get("before_tokens", 0),
                    "afterTokens": result.get("after_tokens", 0),
                    "savedTokens": result.get("saved_tokens", 0),
                    "messageCount": result.get("message_count", 0),
                    "auto": True,
                })
                # Re-read the now-compacted history before building messages.
                history = await self.store.get_messages(session.id)
        except Exception as exc:  # never let compaction abort the turn
            await self._emit("token", {"content": f"\n[Auto-compaction skipped: {exc}]\n"})

        all_tools = TOOL_SCHEMAS + (extra_tools or [])
        messages = build_messages(
            session.id, session.project_path,
            history, user_message, files,
            model=session.model,
            mode=session.mode,
            image_urls=image_urls,
            provider=session.provider,
        )
        messages.insert(1, {
            "role": "system",
            "content": (
                "Available subagents for the task tool:\n"
                f"{describe_subagents(session.project_path)}\n\n"
                "Available skills for the skill tool or /skill-name invocation:\n"
                f"{describe_skills(session.project_path)}\n\n"
                "Use task for independent codebase exploration or bounded research. "
                "Pass subagent_type exactly as one of the listed names. "
                "Use skill when a listed reusable workflow matches the user's request."
            ),
        })

        user_msg = Message(
            id=new_id("msg"), session_id=session.id,
            role="user", content=user_message,
            token_count=estimate_message_tokens([messages[-1]]),
        )
        await self.store.add_message(user_msg)

        for turn in range(settings.max_turns):
            start = time.monotonic()

            await self._emit("thinking", {"turn": turn + 1})

            try:
                streamed_content = False

                async def on_token(token: str) -> None:
                    nonlocal streamed_content
                    streamed_content = True
                    await self._emit("token", {"content": token})

                async def on_reasoning(token: str) -> None:
                    await self._emit("reasoning", {"content": token})

                response = await call(
                    provider=session.provider,
                    model=session.model,
                    messages=messages,
                    tools=all_tools,
                    thinking=session.thinking_enabled,
                    reasoning_effort=session.reasoning_effort,
                    stream=True,
                    on_token=on_token,
                    on_reasoning=on_reasoning,
                )
            except Exception as e:
                await self._emit("error", {"message": str(e)})
                return f"API error: {e}"

            latency_ms = int((time.monotonic() - start) * 1000)
            usage = response.get("usage", {})

            cost = estimate_cost(
                session.model,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("cache_hit_tokens", 0),
                usage.get("cache_creation_tokens", 0),
            )

            await self.store.log_usage(UsageEntry(
                id=new_id("use"), session_id=session.id,
                provider=session.provider, model=session.model,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_hit_tokens=usage.get("cache_hit_tokens", 0),
                cache_miss_tokens=usage.get("cache_miss_tokens", 0),
                cost_usd=cost,
                latency_ms=latency_ms,
            ))

            await self.store.update_session_usage(
                session.id,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("cache_hit_tokens", 0),
                usage.get("cache_miss_tokens", 0),
                cost,
                cache_creation=usage.get("cache_creation_tokens", 0),
            )

            tool_calls = response.get("tool_calls")
            content = response.get("content") or ""
            reasoning = response.get("reasoning_content")

            if content and not streamed_content:
                await self._emit("token", {"content": content})

            assistant_msg = Message(
                id=new_id("msg"), session_id=session.id,
                role="assistant",
                content=content,
                reasoning_content=reasoning,
                tool_calls_json=json.dumps(tool_calls) if tool_calls else None,
            )
            await self.store.add_message(assistant_msg)

            assistant_dict = assistant_msg.to_openai()
            messages.append(assistant_dict)

            if not tool_calls:
                session = await self.store.get_session(session.id) or session
                # Guard against a blank turn: if the model returned no content
                # and nothing was streamed, show a short note instead of an
                # empty assistant block.
                if not content and not streamed_content:
                    await self._emit("token", {"content": "(no response)"})
                await self._emit("done", {
                    "content": content,
                    "usage": {
                        "tokens_in": session.total_tokens_in,
                        "tokens_out": session.total_tokens_out,
                        "cache_hit_rate": session.cache_hit_rate,
                        "cost_usd": session.total_cost_usd,
                    },
                })
                return content

            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                try:
                    tool_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    tool_args = {}

                await self._emit("tool_call", {
                    "id": tc["id"],
                    "name": tool_name,
                    "arguments": tool_args,
                    "summary": _tool_param_summary(tool_name, tool_args),
                    "diff": _tool_diff_preview(tool_name, tool_args, session.project_path),
                })

                if tool_needs_permission(tool_name) and not auto_approve and not self._auto_approve:
                    perm = check_permission(tool_name, tool_args, session.mode)
                    if perm.level == "deny":
                        result = f"Permission denied: {perm.reason}"
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result,
                        })
                        await self.store.add_message(Message(
                            id=new_id("msg"), session_id=session.id,
                            role="tool", content=result,
                            tool_call_id=tc["id"],
                        ))
                        await self._emit("tool_result", {
                            "id": tc["id"],
                            "name": tool_name,
                            "result": result,
                            "permission": "deny",
                        })
                        continue

                    elif perm.level == "ask":
                        prompt_text = format_permission_prompt(tool_name, tool_args)
                        permission_id = f"{session.id}_{tc['id']}"
                        await self._emit("permission_required", {
                            "id": permission_id,
                            "tool": tool_name,
                            "arguments": tool_args,
                            "prompt": prompt_text,
                        })
                        approved = await self._wait_for_permission(permission_id)
                        if not approved:
                            result = "Permission denied by user."
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": result,
                            })
                            await self.store.add_message(Message(
                                id=new_id("msg"), session_id=session.id,
                                role="tool", content=result,
                                tool_call_id=tc["id"],
                            ))
                            await self._emit("tool_result", {
                                "id": tc["id"],
                                "name": tool_name,
                                "result": result,
                                "permission": "deny",
                            })
                            continue

                if tool_name in {"task", "skill"}:
                    tool_args.setdefault("_provider", session.provider)
                    tool_args.setdefault("_model", session.model)
                result = await execute_tool(tool_name, tool_args, session.project_path)

                tool_msg = {"role": "tool", "tool_call_id": tc["id"], "content": result}
                messages.append(tool_msg)

                await self.store.add_message(Message(
                    id=new_id("msg"), session_id=session.id,
                    role="tool", content=result,
                    tool_call_id=tc["id"],
                ))

                await self._emit("tool_result", {
                    "id": tc["id"],
                    "name": tool_name,
                    "result": result,
                    "permission": "allow",
                })

        # Loop exhausted: surface a final assistant message AND a clean `done`
        # so the UI finalizes the turn (instead of only an error that can leave
        # the transcript looking like it produced no response).
        note = "Reached the maximum number of tool turns before finishing."
        await self._emit("token", {"content": note})
        session = await self.store.get_session(session.id) or session
        await self._emit("done", {
            "content": note,
            "usage": {
                "tokens_in": session.total_tokens_in,
                "tokens_out": session.total_tokens_out,
                "cache_hit_rate": session.cache_hit_rate,
                "cost_usd": session.total_cost_usd,
            },
        })
        return note

    async def _wait_for_permission(self, permission_id: str) -> bool:
        # If the decision already arrived (raced ahead of us), use it now.
        if permission_id in self._resolved_permissions:
            return self._resolved_permissions.pop(permission_id)
        future = asyncio.get_event_loop().create_future()
        self._pending_permissions[permission_id] = future
        try:
            return await asyncio.wait_for(future, timeout=300)
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending_permissions.pop(permission_id, None)
            self._resolved_permissions.pop(permission_id, None)

    def resolve_permission(self, permission_id: str, approved: bool) -> None:
        future = self._pending_permissions.get(permission_id)
        if future and not future.done():
            future.set_result(approved)
        else:
            # The wait hasn't registered yet (or already finished). Buffer the
            # decision so the upcoming _wait_for_permission picks it up instead
            # of blocking until the timeout and denying.
            self._resolved_permissions[permission_id] = approved
