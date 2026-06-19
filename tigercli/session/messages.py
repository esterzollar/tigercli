"""Message prefix builder — deterministic ordering for DeepSeek KV cache hits.

The system prompt and project-context blocks are stable across turns so the
provider's KV cache (DeepSeek native, OpenAI prompt caching, Anthropic
cache_control) can hit on the prefix. Anything dynamic (timestamps, working
state) goes in a separate later message that's allowed to vary.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from tigercli.session.models import Message
from tigercli.common.settings import supports_multimodal


def _get_system_prompt(project_path: str, model: str | None = None, mode: str = "build") -> str:
    """Return the rich Codex/Claude-Code-style system prompt.

    Falls back to a minimal prompt if `tigercli.prompt` isn't available
    (keeps the agent loop runnable even with a partial install).
    """
    try:
        from tigercli.prompt import getSystemPrompt

        opts: dict = {}
        if model:
            opts["model"] = model
        if mode:
            opts["mode"] = mode
        return getSystemPrompt(project_path, opts)
    except Exception:
        return (
            "You are TigerLiteCode, an interactive CLI coding agent. "
            "Be concise and direct. Use the available tools to investigate "
            "before editing. Prefer editing existing files over creating new ones."
        )


def build_system_message(project_path: str | None = None, model: str | None = None, mode: str = "build") -> Message:
    """Build the stable system-prompt message."""
    prompt = _get_system_prompt(project_path or ".", model, mode)
    return Message(
        id=f"sys_{uuid.uuid4().hex[:12]}",
        session_id="",
        role="system",
        content=prompt,
    )


def build_project_context(project_path: str) -> str:
    """Load project-level grounding files.

    Order of precedence (first hit wins for the README fallback):
      1. ./AGENTS.md         — open agent-instructions standard (OpenCode/Codex compatible)
      2. ./TIGER.md          — TigerCLI-specific overrides
      3. ./README.md         — only if neither AGENTS.md nor TIGER.md exists
      4. ~/.config/tigercli/AGENTS.md  — user-level global instructions
    """
    parts: list[str] = []
    root = Path(project_path)

    agents_md = root / "AGENTS.md"
    if agents_md.exists():
        try:
            parts.append(f"<!-- AGENTS.md -->\n{agents_md.read_text()}")
        except OSError:
            pass

    tiger_md = root / "TIGER.md"
    if tiger_md.exists():
        try:
            parts.append(f"<!-- TIGER.md -->\n{tiger_md.read_text()}")
        except OSError:
            pass

    if not parts:
        readme = root / "README.md"
        if readme.exists():
            try:
                parts.append(f"<!-- README.md -->\n{readme.read_text()}")
            except OSError:
                pass

    # User-level global instructions (XDG-aware; no Claude assumptions).
    user_global_paths = [
        Path.home() / ".config" / "tigercli" / "AGENTS.md",
        Path.home() / ".tigercli" / "AGENTS.md",
    ]
    for p in user_global_paths:
        if p.exists():
            try:
                parts.append(f"<!-- {p} -->\n{p.read_text()}")
                break
            except OSError:
                pass

    if not parts:
        return ""

    return "<project_context>\n" + "\n\n".join(parts) + "\n</project_context>"


def build_runtime_context(project_path: str, model: str | None = None) -> str:
    """Dynamic context (date, env, runtime versions). Kept in a SEPARATE
    message so the stable prefix above stays cache-friendly."""
    try:
        from tigercli.prompt import getRuntimeContext

        return getRuntimeContext(project_path, model or "")
    except Exception:
        return ""


def build_messages(
    session_id: str,
    project_path: str,
    history: list[Message],
    user_message: str,
    files: list[tuple[str, str]] | None = None,
    model: str | None = None,
    mode: str = "build",
    image_urls: list[str] | None = None,
    provider: str | None = None,
) -> list[dict]:
    """Build the messages array with deterministic prefix ordering.

    The order matters for prompt caching. Providers such as DeepSeek serve a
    disk KV cache that only hits when the message *prefix* matches byte-for-byte
    a previously seen prefix. So the strategy (cf. DeepSeek-Reasonix's
    "prefix-cache stability") is: keep the longest possible prefix identical
    across turns, and push everything volatile to the very end.

    1. System prompt (stable across turns/sessions for the same project+mode)
    2. Project context (AGENTS.md, CLAUDE.md, README) — stable until edited
    3. File attachments (stable per content)
    4. Conversation history (append-only; never mutated, so the prefix grows
       monotonically and earlier turns stay cache-hittable)
    5. New user message, prefixed with the VOLATILE runtime context (date, env,
       versions, model). Folding the runtime block into the final user turn —
       instead of a separate leading system message — keeps the entire prefix
       above byte-identical across turns, so the date changing or the model
       switching never invalidates the cached prefix.
    """
    messages: list[dict] = []

    # 1. System prompt (stable prefix root)
    sys_msg = build_system_message(project_path, model, mode)
    messages.append({"role": "system", "content": sys_msg.content})

    # 2. Project context (stable until files edited)
    ctx = build_project_context(project_path)
    if ctx:
        messages.append({"role": "system", "content": ctx})

    # 3. File attachments (stable per content)
    if files:
        for fpath, fcontent in files:
            messages.append({
                "role": "user",
                "content": f"<file path='{fpath}'>\n{fcontent}\n</file>",
            })

    # 4. Conversation history (non-reverted, in order; append-only)
    messages.extend(_history_to_openai(history))

    # 5. New user message. The volatile runtime context (date/model/env) is
    #    prepended here rather than emitted as a leading system message, so the
    #    cache-stable prefix [system, project, files, history] never changes.
    runtime = build_runtime_context(project_path, model)
    if runtime:
        user_content = f"{runtime}\n\n{user_message}"
    else:
        user_content = user_message

    # Attach pasted clipboard images as multimodal content parts, but only when
    # the active model AND provider route can actually accept image input. When
    # they can't, we never send image_url parts (the provider would reject them
    # with a 400). Instead we tell the model, in-band, that the user attached an
    # image it cannot see, so it can apologise and respond gracefully rather than
    # the turn erroring out.
    images = [u for u in (image_urls or []) if isinstance(u, str) and u]
    if images and supports_multimodal(model or "", provider):
        parts: list[dict] = [{"type": "text", "text": user_content}]
        for url in images:
            parts.append({"type": "image_url", "image_url": {"url": url}})
        messages.append({"role": "user", "content": parts})
    elif images:
        count = len(images)
        note = (
            f"\n\n[System note: The user attached {count} "
            f"image{'s' if count != 1 else ''} to this message, but the current "
            "model cannot receive image input, so the image data is not included. "
            "Briefly let the user know you can't see the image and that they should "
            "switch to a vision-capable model to share images, then help with "
            "anything else they asked.]"
        )
        messages.append({"role": "user", "content": user_content + note})
    else:
        messages.append({"role": "user", "content": user_content})

    return messages


def _valid_tool_result_run(history: list[Message], start: int, tool_call_ids: list[str]) -> bool:
    expected = set(tool_call_ids)
    if not expected:
        return True
    seen: set[str] = set()
    idx = start + 1
    while idx < len(history):
        msg = history[idx]
        if msg.reverted:
            idx += 1
            continue
        if msg.role != "tool":
            break
        if msg.tool_call_id:
            seen.add(msg.tool_call_id)
        idx += 1
    return expected.issubset(seen)


def _history_to_openai(history: list[Message]) -> list[dict]:
    out: list[dict] = []
    pending_tool_ids: set[str] = set()
    for i, msg in enumerate(history):
        if msg.reverted:
            continue
        if msg.role == "tool":
            # Tool messages are only valid immediately after an assistant
            # message with matching tool_calls. Orphan tools corrupt the next
            # request, so skip them here.
            if not pending_tool_ids:
                continue
            if msg.tool_call_id not in pending_tool_ids:
                continue
            out.append(msg.to_openai())
            pending_tool_ids.discard(msg.tool_call_id)
            continue

        pending_tool_ids.clear()

        if msg.role == "assistant" and msg.tool_calls:
            tool_call_ids = [tc.get("id") for tc in msg.tool_calls if tc.get("id")]
            if not _valid_tool_result_run(history, i, tool_call_ids):
                if msg.content:
                    plain = msg.to_openai()
                    plain.pop("tool_calls", None)
                    out.append(plain)
                continue

            pending_tool_ids = set(tool_call_ids)
            out.append(msg.to_openai())
            continue

        out.append(msg.to_openai())
    return out


def estimate_tokens(text: str) -> int:
    """Rough token estimation (4 chars ≈ 1 token for English)."""
    return max(1, len(text) // 4)


def estimate_message_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
    return total
