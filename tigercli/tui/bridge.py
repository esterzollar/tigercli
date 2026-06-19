"""Bridge between the TypeScript TUI and the Python agent engine.

The TUI process owns the terminal. JSON-RPC therefore runs over a local Unix
socket instead of stdio, leaving stdin/stdout free for Ink.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from tigercli.agent.loop import AgentLoop
from tigercli.common.settings import supports_multimodal
from tigercli.config import (
    KNOWN_PROVIDERS,
    get_active_provider_model,
    is_provider_configured,
    load_auth,
    save_auth,
    save_compact_size,
    save_default_selection,
    settings,
)
from tigercli.session.store import SessionStore


# asyncio stream readers default to a 64 KiB line limit. A single JSON-RPC line
# carrying a pasted clipboard image (base64 data URL) easily exceeds that, which
# makes readline() raise LimitOverrunError and drops the connection. Raise the
# limit generously so image payloads fit on one line.
_STREAM_LIMIT = 64 * 1024 * 1024  # 64 MiB


class TUIBridge:
    def __init__(self, binary_path: str | Path | None = None):
        if binary_path is None:
            binary_path = Path(__file__).parent.parent.parent / "tui-ts" / "package.json"
        self.binary_path = Path(binary_path)
        self.process: subprocess.Popen | None = None
        self._server: asyncio.AbstractServer | None = None
        self._sock_dir: str | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._send_lock = asyncio.Lock()
        self._store = SessionStore()
        self._agent_loop: AgentLoop | None = None
        self._session_id: str | None = None
        self._run_task: asyncio.Task | None = None
        # Set when the TUI sends "quit" so the parent process can shut down
        # cleanly instead of waiting on a Ctrl-C.
        self._quit_event: asyncio.Event = asyncio.Event()
        # Hold strong references to fire-and-forget tasks so the event loop
        # does not garbage-collect (and cancel) them mid-flight.
        self._background_tasks: set[asyncio.Task] = set()

    def _debug(self, message: str) -> None:
        try:
            log_path = settings.cache_home / "tui-bridge.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except Exception:
            pass

    async def start(self) -> None:
        if not self.binary_path.exists():
            raise FileNotFoundError(str(self.binary_path))

        addr = await self._start_engine_server()

        env = os.environ.copy()
        env["TIGERCLI_BRIDGE_ADDR"] = addr
        tui_dir = self.binary_path.parent
        self.process = subprocess.Popen(["npm", "run", "start", "--silent"], cwd=str(tui_dir), env=env)

    async def stop(self) -> None:
        # Cancel any in-flight agent turn and background tasks so their
        # coroutines (and DB handles) don't leak on shutdown.
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                pass
        for task in list(self._background_tasks):
            task.cancel()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                # process.wait() is blocking; run it off the event loop so we
                # don't stall pending socket writes during shutdown.
                await asyncio.wait_for(asyncio.to_thread(self.process.wait), timeout=2.0)
            except (asyncio.TimeoutError, subprocess.TimeoutExpired):
                self.process.kill()
        # Restore terminal in case TUI didn't clean up
        sys.stdout.write("\x1b[?25h")  # show cursor
        sys.stdout.write("\x1b[2J\x1b[H")  # clear screen
        sys.stdout.write("\x1b[?1049l")  # exit alt screen
        sys.stdout.flush()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # Remove the private socket directory we created in _start_engine_server.
        if self._sock_dir:
            try:
                import shutil

                shutil.rmtree(self._sock_dir, ignore_errors=True)
            except Exception:
                pass
            self._sock_dir = None
        await self._store.close()

    async def _start_engine_server(self) -> str:
        # Own a private directory so no other process can pre-create or hijack
        # the socket path (mktemp alone is a TOCTOU race). Unix socket paths are
        # limited to ~108 bytes, so prefer a short temp root ("/tmp") when it is
        # available and fall back to the platform default otherwise.
        tmp_root = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
        self._sock_dir = tempfile.mkdtemp(prefix="tigercli-", dir=tmp_root)
        sock_path = os.path.join(self._sock_dir, "engine.sock")
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=sock_path, limit=_STREAM_LIMIT
        )
        return f"unix:{sock_path}"

    async def wait(self) -> None:
        while self.process and self.process.poll() is None:
            if self._quit_event.is_set():
                break
            try:
                await asyncio.wait_for(self._quit_event.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Close any stale writer from a previous connection so its transport /
        # file descriptor is released instead of leaking on reconnect.
        if self._writer is not None and self._writer is not writer:
            try:
                self._writer.close()
            except Exception:
                pass
        self._writer = writer
        await self._send_startup_state()
        try:
            while not reader.at_eof():
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError) as exc:
                    # A single line exceeded the (already large) stream limit.
                    # Drain whatever is buffered and skip this message rather
                    # than tearing down the whole connection.
                    self._debug(f"oversized line skipped: {exc}")
                    try:
                        await reader.read(_STREAM_LIMIT)
                    except Exception:
                        pass
                    await self._send("error", {"message": "Message too large — skipped. Try a smaller image."})
                    continue
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                try:
                    await self._handle_notification(msg.get("method", ""), msg.get("params") or {})
                except Exception as exc:
                    await self._send("error", {"message": f"Bridge handler failed: {exc}"})
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError) as exc:
            # TUI was killed abruptly; treat as a clean disconnect.
            self._debug(f"client read ended: {exc}")
        finally:
            if self._writer is writer:
                self._writer = None

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        self._debug(f"recv {method} {params}")
        if method == "user_input":
            text = str(params.get("text") or "")
            raw_images = params.get("imageUrls") or params.get("image_urls") or []
            images = [u for u in raw_images if isinstance(u, str) and u] if isinstance(raw_images, list) else []
            if self._run_task and not self._run_task.done():
                await self._send("message", {"role": "system", "content": "Turn is still running. Press Ctrl+C to interrupt."})
            else:
                self._run_task = asyncio.create_task(self._handle_user_input(text, images))
        elif method == "interrupt":
            await self._interrupt()
        elif method == "quit":
            # TUI is closing; interrupt any running turn and signal the parent
            # process to shut down cleanly (so it never waits on a Ctrl-C).
            await self._interrupt()
            self._quit_event.set()
        elif method == "create_session":
            session = await self._create_session()
            await self._send("messages", {"sessionId": session.id, "messages": []})
        elif method == "slash_command":
            command = str(params.get("command") or "")
            if self._run_task and not self._run_task.done():
                await self._send("message", {"role": "system", "content": "Turn is still running. Press Ctrl+C to interrupt."})
            else:
                # Run as a task so interrupt/quit can cancel it and the
                # notification loop stays responsive while it runs.
                self._run_task = asyncio.create_task(self._handle_slash_command(command))
        elif method == "resume_session":
            await self._handle_resume_session(params)
        elif method == "activate_session":
            await self._handle_activate_session(params)
        elif method == "permission_response" and self._agent_loop:
            permission_id = str(params.get("toolCallId") or "")
            approved = bool(params.get("approved"))
            always = bool(params.get("always"))
            self._agent_loop.resolve_permission(permission_id, approved)
            if always:
                self._agent_loop.set_auto_approve(True)
        elif method == "model_change":
            provider = str(params.get("provider") or settings.default_provider)
            model = str(params.get("model") or settings.default_model)
            provider, model = self._normalize_provider_model(provider, model)
            save_default_selection(provider, model)
            if self._session_id:
                await self._store.update_session(self._session_id, provider=provider, model=model)
                session = await self._store.get_session(self._session_id)
                if session:
                    await self._send_session(session)
            else:
                # No active session yet: reflect the explicit choice directly.
                # Do NOT call _send_startup_state here — it recomputes the active
                # provider/model from auth and would override the user's pick.
                await self._send("config", {
                    "provider": provider,
                    "model": model,
                    "mode": settings.default_mode,
                    "reasoningEffort": settings.reasoning_effort,
                })
        elif method == "mode_change":
            mode = str(params.get("mode") or "build")
            if self._session_id:
                await self._store.update_session(self._session_id, mode=mode)
        elif method == "reasoning_effort_change":
            effort = str(params.get("effort") or settings.reasoning_effort)
            if effort in {"low", "medium", "high", "max"}:
                settings.reasoning_effort = effort
                if self._session_id:
                    await self._store.update_session(self._session_id, reasoning_effort=effort)
                    session = await self._store.get_session(self._session_id)
                    if session:
                        await self._send_session(session)
                await self._send_startup_state()
        elif method == "provider_config":
            await self._handle_provider_config(params)
        elif method == "request_messages":
            await self._handle_request_messages(params)
        elif method == "compact":
            await self._handle_compact(params)
        elif method == "context_info":
            await self._handle_context_info(params)
        elif method == "compact_size":
            await self._handle_compact_size(params)
        elif method == "fetch_models":
            await self._handle_fetch_models()
        elif method == "request_sessions":
            # Frontend-driven session listing scoped to the current project so
            # the resume list never surfaces unrelated projects' conversations.
            # `scope` is "project" (default) or "all". Kept as a dedicated
            # method because slash_command's /sessions path carries no scope.
            await self._handle_request_sessions(params)

    async def _interrupt(self) -> None:
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            # The CancelledError handler in _handle_user_input sends the
            # "Turn interrupted" message and the finally block resets busy.

    async def _handle_user_input(self, text: str, images: list[str] | None = None) -> None:
        text = text.strip()
        images = images or []
        if not text and not images:
            return
        if text.startswith("/"):
            await self._handle_slash_command(text)
            return

        session = await self._get_or_create_session()

        # When the model/provider can't accept image input we still pass the
        # images through to the agent loop: build_messages strips the binary
        # image_url parts (which the provider would reject with a 400) and adds
        # an in-band note so the model knows an image was attached and can tell
        # the user it can't see it. We also surface a short notice in the UI.
        if images and not supports_multimodal(session.model, session.provider):
            await self._send("message", {"role": "system", "content": (
                f"Model '{session.provider}/{session.model}' can't receive images — "
                f"{'the image was' if len(images) == 1 else 'images were'} not sent to it. "
                "Switch to a vision-capable model with /model to share images."
            )})

        await self._send("busy", {"busy": True, "text": "Working"})

        self._agent_loop = AgentLoop(self._store, on_event=self._on_agent_event)
        try:
            await self._agent_loop.run(session=session, user_message=text, image_urls=images)
        except asyncio.CancelledError:
            await self._send("error", {"message": "Turn interrupted."})
            raise
        finally:
            self._agent_loop = None
            refreshed = await self._store.get_session(session.id)
            if refreshed:
                await self._send_session(refreshed)
            await self._send("busy", {"busy": False})
            # Auto-generate title after first turn if still untitled
            if session.title in (None, "", "(untitled)"):
                task = asyncio.create_task(self._generate_title(session.id, text))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

    async def _generate_title(self, session_id: str, first_message: str) -> None:
        """Auto-generate a session title using the LLM (best-effort)."""
        try:
            from tigercli.client import call

            session = await self._store.get_session(session_id)
            if not session or session.title not in (None, "", "(untitled)"):
                return

            # Get the assistant's first response for context
            msgs = await self._store.get_messages(session_id)
            assistant_content = ""
            for m in msgs:
                if m.role == "assistant" and m.content:
                    assistant_content = m.content[:500]
                    break

            prompt = [
                {"role": "system", "content": (
                    "Generate a short, descriptive title (3-6 words) for this "
                    "conversation between a user and an AI coding agent. "
                    "Capture the main task or topic. "
                    "Return ONLY the title — no quotes, no prefixes, no markdown."
                )},
                {"role": "user", "content": (
                    f"User request: {first_message[:300]}\n\n"
                    f"Assistant response summary: {assistant_content[:300]}\n\n"
                    "Title:"
                )},
            ]

            response = await call(
                provider=session.provider,
                model=session.model,
                messages=prompt,
                tools=None,
                thinking=False,
                stream=False,
            )

            title = (response.get("content") or "").strip().strip('"\'').strip()
            title = title.split("\n")[0]  # First line only
            if len(title) > 80:
                title = title[:77] + "..."

            if title and title not in ("", "(untitled)"):
                await self._store.update_session(session_id, title=title)
                refreshed = await self._store.get_session(session_id)
                if refreshed:
                    await self._send_session(refreshed)
        except Exception as exc:
            # Title generation is best-effort; don't block, but log so an
            # underlying DB/LLM failure isn't completely invisible.
            self._debug(f"title generation failed: {exc}")

    async def _handle_slash_command(self, command: str) -> None:
        cmd = command.split()[0].lower() if command.split() else ""
        if cmd in ("/sessions", "/resume"):
            await self._send_sessions()
        elif cmd == "/new":
            session = await self._create_session()
            await self._send("message", {"role": "system", "content": f"New session: {session.id}"})
        elif cmd == "/init":
            agents = Path.cwd() / "AGENTS.md"
            if agents.exists():
                await self._send("message", {"role": "system", "content": "AGENTS.md already exists"})
            else:
                agents.write_text("# Instructions\n\nDescribe build, test, and project conventions here.\n")
                await self._send("message", {"role": "system", "content": f"Created {agents}"})
        else:
            from tigercli.agent.skills import resolve_slash_skill

            if resolve_slash_skill(command, str(Path.cwd())) is None:
                await self._send("message", {"role": "system", "content": f"Unknown command: {cmd}. Try /help"})
                return
            session = await self._get_or_create_session()
            await self._send("busy", {"busy": True, "text": "Working"})
            self._agent_loop = AgentLoop(self._store, on_event=self._on_agent_event)
            try:
                await self._agent_loop.run(session=session, user_message=command)
            except asyncio.CancelledError:
                await self._send("error", {"message": "Turn interrupted."})
                raise
            finally:
                self._agent_loop = None
                refreshed = await self._store.get_session(session.id)
                if refreshed:
                    await self._send_session(refreshed)
                await self._send("busy", {"busy": False})

    async def _handle_provider_config(self, params: dict[str, Any]) -> None:
        provider = str(params.get("provider") or "")
        if not provider:
            return
        auth = load_auth()
        cfg = auth.get(provider, {})
        if not isinstance(cfg, dict):
            cfg = {}

        api_key = str(params.get("apiKey") or "")
        base_url = str(params.get("baseURL") or "")
        models = params.get("models")
        if api_key:
            cfg["api_key"] = api_key
        if base_url:
            cfg["base_url"] = base_url
        if isinstance(models, list):
            cfg["models"] = [{"id": str(model), "name": str(model), "owned_by": provider} for model in models]

        auth[provider] = cfg
        save_auth(auth)
        # Drop any cached client for this provider so the new key/base_url is
        # used on the next call instead of stale cached credentials.
        try:
            from tigercli.client import evict_client

            evict_client(provider)
        except Exception:
            pass
        await self._send("providers", {"providers": self._provider_payload(auth)})

    async def _handle_request_messages(self, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        if not session_id:
            return
        await self._send_session_messages(session_id)

    async def _handle_resume_session(self, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        self._debug(f"resume start {session_id}")
        if not session_id:
            return
        session = await self._store.get_session(session_id)
        if not session:
            self._debug(f"resume missing {session_id}")
            await self._send("message", {"role": "system", "content": f"Session not found: {session_id}"})
            return
        self._session_id = session.id
        messages = await self._visible_message_payloads(session.id)
        self._debug(f"resume send {session.id} messages={len(messages)}")
        await self._send("session_resume", {
            "session": self._session_payload(session),
            "messages": messages,
        })
        self._debug(f"resume sent {session.id}")

    async def _handle_activate_session(self, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        self._debug(f"activate start {session_id}")
        if not session_id:
            return
        session = await self._store.get_session(session_id)
        if not session:
            self._debug(f"activate missing {session_id}")
            return
        self._session_id = session.id
        self._debug(f"activate done {session.id}")

    async def _send_session_messages(self, session_id: str) -> None:
        await self._send("messages", {
            "sessionId": session_id,
            "messages": await self._visible_message_payloads(session_id),
        })

    async def _visible_message_payloads(self, session_id: str) -> list[dict[str, Any]]:
        msgs = await self._store.get_messages(session_id)
        # Send the visible workspace, not only the chat transcript. Tool rows
        # restore expandable result cards on resume; empty assistant tool-call
        # carrier rows are skipped because the tool result row follows them.
        display_roles = {"user", "assistant", "system", "tool", "thinking"}
        display_msgs = [
            m for m in msgs
            if m.role in display_roles and not getattr(m, "reverted", False)
            and ((m.content or "").strip() or m.role == "assistant")
            and not (m.role == "assistant" and not (m.content or "").strip())
        ]
        return [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content or "",
                "reasoningContent": m.reasoning_content or "",
                "detail": (m.content or "") if m.role == "tool" else "",
                "toolCallId": m.tool_call_id,
                "toolCallsJson": m.tool_calls_json,
            }
            for m in display_msgs
        ]

    async def _handle_compact(self, params: dict[str, Any]) -> None:
        """Handle /compact command — generate summary and replace history."""
        if not self._session_id:
            await self._send("message", {
                "role": "system",
                "content": "No active session to compact. Start a conversation first.",
            })
            return

        focus = str(params.get("focus") or "").strip() or None

        session = await self._store.get_session(self._session_id)
        if not session:
            await self._send("message", {
                "role": "system",
                "content": "Session not found.",
            })
            return

        await self._send("busy", {"busy": True, "text": "Compacting"})
        await self._send("message", {
            "role": "system",
            "content": "📦 Compacting conversation history..." +
                (f"\nFocus: {focus}" if focus else ""),
        })

        try:
            from tigercli.session.compact import compact_session

            result = await compact_session(
                store=self._store,
                session_id=self._session_id,
                provider=session.provider,
                model=session.model,
                focus=focus,
            )

            # Send the compact summary as a new message
            await self._send("message", {
                "role": "system",
                "content": (
                    f"📦 **Compacted!**\n"
                    f"  {result['message_count']} messages → 1 summary\n"
                    f"  {result['before_tokens']:,} tokens → {result['after_tokens']:,} tokens\n"
                    f"  Saved: {result['saved_tokens']:,} tokens "
                    f"({_pct(result['saved_tokens'], result['before_tokens'])})\n"
                    + ("\n[dim]Note: Used fallback summary — LLM call failed.[-]" if result.get("fallback") else "")
                ),
            })

            # Send the compact_complete event with stats
            await self._send("compact_complete", {
                "beforeTokens": result["before_tokens"],
                "afterTokens": result["after_tokens"],
                "savedTokens": result["saved_tokens"],
                "messageCount": result["message_count"],
                "summaryText": result["summary_text"],
                "fallback": result.get("fallback", False),
            })

        except Exception as exc:
            await self._send("message", {
                "role": "system",
                "content": f"Compaction failed: {exc}",
            })
        finally:
            await self._send("busy", {"busy": False})

    async def _handle_compact_size(self, params: dict[str, Any]) -> None:
        """Handle /compact_size — set the auto-compaction token threshold.

        Accepts a plain integer or a value with a k/m suffix (e.g. 500k, 1m).
        With no value, reports the current threshold and window.
        """
        raw = str(params.get("value") or "").strip().lower()
        if not raw:
            await self._send("message", {"role": "system", "content": (
                f"Auto-compact threshold: {settings.compact_size:,} tokens "
                f"(context window {settings.context_window:,}).\n"
                "Set a new value with /compact_size <tokens>, e.g. /compact_size 300k."
            )})
            return

        try:
            mult = 1
            num = raw
            if raw.endswith("k"):
                mult, num = 1_000, raw[:-1]
            elif raw.endswith("m"):
                mult, num = 1_000_000, raw[:-1]
            value = int(round(float(num) * mult))
        except ValueError:
            await self._send("message", {"role": "system", "content": (
                f"Invalid size '{raw}'. Use a number like 500000, 500k, or 1m."
            )})
            return

        if value < 1000:
            await self._send("message", {"role": "system", "content": (
                "Auto-compact threshold must be at least 1000 tokens."
            )})
            return

        # Keep the window at least as large as the threshold so the bar stays
        # sensible; grow it to match if the user sets a threshold above it.
        new_window = max(settings.context_window, value)
        save_compact_size(value, new_window)
        await self._send("message", {"role": "system", "content": (
            f"Auto-compact threshold set to {value:,} tokens "
            f"(context window {new_window:,}). "
            "The conversation will summarize itself when it reaches this size."
        )})

    async def _handle_context_info(self, params: dict[str, Any]) -> None:
        """Handle /context command — show token/context usage."""
        if not self._session_id:
            await self._send("message", {
                "role": "system",
                "content": "No active session. Start a conversation first.",
            })
            return

        from tigercli.session.messages import estimate_tokens

        msgs = await self._store.get_messages(self._session_id)
        session = await self._store.get_session(self._session_id)

        total_tokens = sum(estimate_tokens(m.content or "") for m in msgs)
        msg_count = len(msgs)

        user_count = sum(1 for m in msgs if m.role == "user")
        assistant_count = sum(1 for m in msgs if m.role == "assistant")
        tool_count = sum(1 for m in msgs if m.role == "tool")
        system_count = sum(1 for m in msgs if m.role == "system")

        # Build a visual bar (10 segments). The window and the auto-compact
        # threshold come from settings (default 1M window, 500K auto-compact).
        max_tokens = settings.context_window
        bar_len = 10
        filled = min(bar_len, int(total_tokens / max_tokens * bar_len))
        bar = "█" * filled + "░" * (bar_len - filled)
        pct = min(100, int(total_tokens / max_tokens * 100))

        info = (
            f"📊 **Context Usage**\n\n"
            f"  [{bar}] {pct}%\n"
            f"  {total_tokens:,} / {max_tokens:,} estimated tokens\n"
            f"  Auto-compacts at {settings.compact_size:,} tokens (change with /compact_size)\n\n"
            f"**Messages**: {msg_count} total\n"
            f"  👤 User: {user_count}\n"
            f"  🤖 Assistant: {assistant_count}\n"
            f"  🔧 Tool: {tool_count}\n"
            f"  ⚙ System: {system_count}\n\n"
        )

        if session:
            info += (
                f"**Session usage**:\n"
                f"  Input: {session.total_tokens_in:,} tokens\n"
                f"  Output: {session.total_tokens_out:,} tokens\n"
                f"  Cost: ${session.total_cost_usd:.4f}\n"
                f"  Requests: {session.total_requests}\n"
            )

        if total_tokens >= settings.compact_size:
            info += "\n[yellow]⚠ At the auto-compact threshold — the next turn will summarize history.[-]"
        elif pct >= 75:
            info += "\n[yellow]⚠ Approaching context limit. Consider [/compact][-]"

        await self._send("message", {
            "role": "system",
            "content": info,
        })

        await self._send("context_info", {
            "totalTokens": total_tokens,
            "maxTokens": max_tokens,
            "compactSize": settings.compact_size,
            "pct": pct,
            "messageCount": msg_count,
            "userCount": user_count,
            "assistantCount": assistant_count,
            "toolCount": tool_count,
            "systemCount": system_count,
        })

    async def _get_or_create_session(self):
        if self._session_id:
            session = await self._store.get_session(self._session_id)
            if session:
                return session
        return await self._create_session()

    async def _create_session(self):
        provider, model = get_active_provider_model(load_auth())
        provider, model = self._normalize_provider_model(provider, model)
        session = await self._store.create_session(
            project_path=str(Path.cwd()),
            provider=provider,
            model=model,
            thinking_enabled=settings.thinking_enabled,
            reasoning_effort=settings.reasoning_effort,
            mode=settings.default_mode,
        )
        self._session_id = session.id
        await self._send_session(session)
        return session

    def _normalize_provider_model(self, provider: str, model: str) -> tuple[str, str]:
        # If the model name has a provider prefix, correct both
        model_lower = model.lower()
        for known_id in KNOWN_PROVIDERS:
            prefix = known_id + "/"
            if model_lower.startswith(prefix.lower()):
                return known_id, model.removeprefix(known_id + "/")
        if provider in {"opencode-go", "opencode-zen"}:
            prefix = provider + "/"
            if model.startswith(prefix):
                return provider, model.removeprefix(prefix)
        return provider, model

    async def _on_agent_event(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == "token":
            await self._send("token", {"content": data.get("content") or ""})
        elif event_type == "tool_result":
            payload = dict(data)
            payload["result"] = self._format_tool_result(str(payload.get("result") or ""))
            await self._send(event_type, payload)
        elif event_type in {"tool_call", "permission_required", "error", "done", "thinking", "reasoning"}:
            await self._send(event_type, data)

    def _format_tool_result(self, result: str) -> str:
        try:
            payload = json.loads(result)
        except Exception:
            return result
        if not isinstance(payload, dict):
            return result
        output = str(payload.get("output") or payload.get("error") or "")
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata.get("diff_preview"):
            diff = str(metadata["diff_preview"])
            return f"{output}\n\n```diff\n{diff}\n```" if output else f"```diff\n{diff}\n```"
        return output or result

    async def _send_startup_state(self) -> None:
        auth = load_auth()
        provider, model = get_active_provider_model(auth)
        provider, model = self._normalize_provider_model(provider, model)
        await self._send("config", {
            "provider": provider,
            "model": model,
            "mode": settings.default_mode,
            "reasoningEffort": settings.reasoning_effort,
            "cwd": str(Path.cwd()),
        })
        await self._send("providers", {"providers": self._provider_payload(auth)})
        try:
            await asyncio.wait_for(self._send_sessions(), timeout=1.0)
        except asyncio.TimeoutError:
            await self._send("sessions", {"sessions": []})
        except Exception as exc:
            # A real DB error (locked/corrupt) must not silently look like an
            # empty session list with no trace.
            self._debug(f"send_sessions failed: {exc}")
            await self._send("sessions", {"sessions": []})

    def _provider_payload(self, auth: dict[str, Any]) -> list[dict[str, Any]]:
        providers: list[dict[str, Any]] = []
        for provider_id, info in KNOWN_PROVIDERS.items():
            cfg = auth.get(provider_id, {})
            # Legacy key fallback: "opencode-go" ↔ "opencode"
            if not cfg and provider_id == "opencode-go":
                cfg = auth.get("opencode", {})
            if not isinstance(cfg, dict):
                cfg = {}
            models: list[str] = []
            raw_models = cfg.get("models", info.get("models", []))
            if isinstance(raw_models, list):
                for model in raw_models:
                    if isinstance(model, dict) and isinstance(model.get("id"), str):
                        models.append(model["id"])
                    elif isinstance(model, str):
                        models.append(model)
            providers.append({
                "id": provider_id,
                "configured": is_provider_configured(provider_id, auth),
                "apiKey": cfg.get("api_key", ""),
                "baseURL": cfg.get("base_url", info.get("base_url", "")),
                "models": models,
            })
        return providers

    async def _send_session(self, session) -> None:
        payload = self._session_payload(session)
        payload["contextTokens"] = await self._context_tokens(session.id)
        await self._send("session", payload)

    async def _context_tokens(self, session_id: str) -> int:
        """Estimate the current conversation's context size (tokens) for the
        live status-bar indicator, mirroring the /context calculation."""
        try:
            from tigercli.session.messages import estimate_tokens

            msgs = await self._store.get_messages(session_id)
            return sum(estimate_tokens(m.content or "") for m in msgs)
        except Exception:
            return 0

    def _session_payload(self, session) -> dict[str, Any]:
        return {
            "id": session.id,
            "title": session.title,
            "provider": session.provider,
            "model": session.model,
            "mode": session.mode,
            "reasoningEffort": session.reasoning_effort,
            "updatedAt": session.updated_at,
            "cost": session.total_cost_usd,
            "cacheHitRate": session.cache_hit_rate,
            "cacheHitTokens": session.total_cache_hit_tokens,
            "cacheMissTokens": session.total_cache_miss_tokens,
            "tokensIn": session.total_tokens_in,
            "tokensOut": session.total_tokens_out,
            "requests": session.total_requests,
            "contextWindow": settings.context_window,
            "compactSize": settings.compact_size,
        }

    async def _session_payload_with_messages(self, session) -> dict[str, Any]:
        payload = self._session_payload(session)
        payload["contextTokens"] = await self._context_tokens(session.id)
        payload["messages"] = await self._visible_message_payloads(session.id)
        return payload

    async def _send_sessions(self, scope: str = "project") -> None:
        """List sessions. Default scope is the current project path so a fresh
        project never inherits another project's history in the resume list."""
        cwd = str(Path.cwd())
        if scope == "all":
            sessions = await self._store.list_sessions(limit=50)
        else:
            sessions = await self._store.search_sessions(
                query="", project_path=cwd, status="active", limit=50
            )
        payloads = [await self._session_payload_with_messages(s) for s in sessions]
        await self._send("sessions", {
            "sessions": payloads,
            "scope": scope,
            "projectPath": cwd,
        })

    async def _handle_fetch_models(self) -> None:
        """Push the known model lists immediately, then refresh them live.

        Sends the currently-cached providers first so the model picker is never
        blank while waiting on (possibly slow) live `models.list()` calls. Then
        refreshes from each configured provider and sends an update if anything
        changed. Honours the legacy "opencode" auth key (used by opencode-go) and
        never discards a provider's already-known models if a live fetch fails.
        """
        from tigercli.client import get_client

        auth = load_auth()
        # 1) Immediate response from cache so the menu always has data.
        await self._send("providers", {"providers": self._provider_payload(auth)})

        # 2) Live refresh (best-effort, per-provider, isolated failures).
        updated = False
        for provider_id in KNOWN_PROVIDERS:
            if not is_provider_configured(provider_id, auth):
                continue
            try:
                client = get_client(provider_id)
                page = await asyncio.wait_for(client.models.list(), timeout=8.0)
                model_ids: list[str] = [m.id for m in page.data] if hasattr(page, "data") else []
                if not model_ids:
                    continue
                # Resolve the auth slot, honouring the legacy opencode-go ↔ opencode key.
                cfg = auth.get(provider_id, {})
                target_key = provider_id
                if not cfg and provider_id == "opencode-go" and isinstance(auth.get("opencode"), dict):
                    cfg = auth.get("opencode", {})
                    target_key = "opencode"
                if not isinstance(cfg, dict):
                    cfg = {}
                cfg["models"] = [{"id": mid, "name": mid, "owned_by": provider_id} for mid in model_ids]
                auth[target_key] = cfg
                updated = True
            except Exception:
                # Keep any previously-known models for this provider; just skip the refresh.
                pass

        if updated:
            save_auth(auth)
            await self._send("providers", {"providers": self._provider_payload(auth)})

    async def _handle_request_sessions(self, params: dict[str, Any]) -> None:
        scope = str(params.get("scope") or "project")
        if scope not in {"project", "all"}:
            scope = "project"
        await self._send_sessions(scope=scope)

    async def _send(self, method: str, params: Any = None) -> None:
        if not self._writer:
            self._debug(f"send skipped no-writer {method}")
            return
        async with self._send_lock:
            msg = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            try:
                self._writer.write((json.dumps(msg) + "\n").encode("utf-8"))
                await self._writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                # TUI went away mid-write. Drop the writer so we don't recurse
                # into another failing _send (which would raise here again).
                self._debug(f"send failed {method}: {exc}")
                self._writer = None
                return
            self._debug(f"sent {method}")


def _pct(part: int, whole: int) -> str:
    """Format a percentage string, handling zero denominator."""
    if whole <= 0:
        return "0%"
    return f"{part / whole * 100:.0f}%"


async def run_tui() -> None:
    root = Path(__file__).parent.parent.parent
    binary_path = root / "tui-ts" / "package.json"
    if not binary_path.exists():
        raise FileNotFoundError(str(binary_path))
    bridge = TUIBridge(binary_path)
    try:
        await bridge.start()
        await bridge.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await bridge.stop()


class StdoutBridgeWriter:
    def write(self, data: bytes) -> None:
        sys.stdout.buffer.write(data)

    async def drain(self) -> None:
        sys.stdout.buffer.flush()


async def run_engine_stdio() -> None:
    bridge = TUIBridge(binary_path="")
    bridge._writer = StdoutBridgeWriter()  # type: ignore[assignment]
    await bridge._send_startup_state()
    while True:
        line = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not line:
            break
        try:
            msg = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        try:
            await bridge._handle_notification(msg.get("method", ""), msg.get("params") or {})
        except Exception as exc:
            await bridge._send("error", {"message": f"Bridge handler failed: {exc}"})
    try:
        await asyncio.wait_for(bridge.stop(), timeout=1.0)
    except Exception:
        pass
    # stop() has already flushed the store; exit via SystemExit so atexit
    # handlers and any remaining finalizers still run (os._exit would skip them).
    sys.exit(0)


async def run_engine_server() -> None:
    """Run only the Python engine side and print its bridge address.

    This is used when a frontend is launched directly. The address is the
    first stdout line; all terminal UI remains owned by the frontend.
    """
    bridge = TUIBridge(binary_path="")
    addr = await bridge._start_engine_server()
    print(addr, flush=True)
    try:
        await bridge._server.serve_forever()
    finally:
        await bridge.stop()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--engine":
        asyncio.run(run_engine_server())
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--engine-stdio":
        asyncio.run(run_engine_stdio())
        return
    asyncio.run(run_tui())


if __name__ == "__main__":
    main()
