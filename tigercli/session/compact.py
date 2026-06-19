"""Conversation compaction — LLM-driven summary generation.

Like Claude Code's /compact, this takes the full conversation history, calls the
LLM to produce a structured summary, marks old messages as reverted, and inserts
the summary as a fresh system message so the next turn has a clean context.

The summary preserves:
- User's original goal / request
- Work completed so far (files changed, tools used, results)
- Errors encountered and how they were fixed
- Pending tasks and next steps
- Key files and their locations
- Conventions and patterns learned
"""

from __future__ import annotations

import json
from tigercli.session.models import Message, new_id, now_iso
from tigercli.session.messages import estimate_tokens, estimate_message_tokens

COMPACT_PROMPT = """You are a context compaction assistant. Your job is to summarize a conversation
between a user and an AI coding agent into a dense, structured summary.

The summary must be COMPREHENSIVE — nothing important may be lost. It will REPLACE
the full conversation history, so every relevant detail must survive.

## Rules for the summary

1. **Goal**: State the user's original request and any follow-up goals clearly.
2. **Work done**: List every substantive action the agent took (files read, files written,
   files edited, commands run, searches performed). Include file paths.
3. **Errors and fixes**: Every error encountered, its root cause, and how it was fixed.
   If an approach was tried and abandoned, note why.
4. **Pending tasks**: Tasks the user asked for that are not yet complete.
5. **Key files**: List the most important files with their paths and what they contain.
6. **Conventions**: Coding patterns, idioms, naming conventions, or project structure
   that the agent observed and followed.
7. **Decisions**: Architectural or design decisions made during the conversation.
8. **Next steps**: What the agent should do next when the conversation resumes.

## Format

Write the summary as a single <summary> block. Use markdown inside. Keep it dense —
bullet points are fine. Aim for 500-1500 words depending on conversation length.

<summary>
[Your structured summary here]
</summary>"""


def build_compact_messages(
    history: list[Message],
    focus: str | None = None,
) -> list[dict]:
    """Build the messages array to send to the LLM for compaction.

    We send a system prompt explaining the compaction task, then include
    the full conversation history formatted as a transcript.
    """
    prompt = COMPACT_PROMPT
    if focus:
        prompt += (
            f"\n\n**IMPORTANT FOCUS**: {focus}\n"
            "Pay special attention to the above focus area in your summary. "
            "Preserve all details related to it."
        )

    messages: list[dict] = [
        {"role": "system", "content": prompt},
    ]

    # Format the conversation history as a transcript
    transcript_parts: list[str] = []
    for msg in history:
        role_label = msg.role.upper()
        content = msg.content or ""

        if msg.role == "tool":
            # Truncate very long tool results
            if len(content) > 2000:
                content = content[:2000] + "\n... [truncated for compaction]"
            transcript_parts.append(
                f"[{role_label} tool_call_id={msg.tool_call_id}]\n{content}"
            )
        elif msg.role == "assistant":
            # Include tool calls if present
            if msg.tool_calls_json:
                try:
                    tcs = json.loads(msg.tool_calls_json)
                    tc_summary = ", ".join(
                        tc["function"]["name"] for tc in tcs
                    )
                    content = f"[Tool calls: {tc_summary}]\n{content}"
                except (json.JSONDecodeError, KeyError):
                    pass
            if msg.reasoning_content:
                reasoning = msg.reasoning_content
                if len(reasoning) > 1000:
                    reasoning = reasoning[:1000] + "..."
                transcript_parts.append(
                    f"[{role_label} reasoning]\n{reasoning}"
                )
            transcript_parts.append(f"[{role_label}]\n{content}")
        else:
            transcript_parts.append(f"[{role_label}]\n{content}")

    transcript = "\n\n".join(transcript_parts)

    # If the transcript is very long, truncate the middle
    max_chars = 50000
    if len(transcript) > max_chars:
        half = max_chars // 2
        transcript = (
            transcript[:half]
            + "\n\n... [middle section truncated for compaction] ...\n\n"
            + transcript[-half:]
        )

    messages.append({"role": "user", "content": transcript})
    return messages


def parse_compact_summary(content: str) -> str:
    """Extract the summary from the LLM response."""
    # Try to find <summary>...</summary> tags
    start = content.find("<summary>")
    end = content.find("</summary>")
    if start != -1 and end != -1:
        summary = content[start + len("<summary>"):end].strip()
        if summary:
            return summary
    # Fallback: return the whole content
    return content.strip()


async def compact_session(
    store,
    session_id: str,
    provider: str,
    model: str,
    focus: str | None = None,
) -> dict:
    """Compact a session's message history.

    1. Load all messages
    2. Generate a compact summary via LLM
    3. Mark all existing messages as reverted
    4. Insert the summary as a system message
    5. Return stats (before/after counts)
    """
    from tigercli.client import call

    # Load all messages for full context
    messages = await store.get_messages(session_id, include_reverted=False)
    if not messages:
        return {
            "before_tokens": 0,
            "after_tokens": 0,
            "saved_tokens": 0,
            "message_count": 0,
            "summary_text": "(No messages to compact)",
        }

    msg_count = len(messages)
    before_tokens = sum(
        estimate_tokens(m.content or "") for m in messages
    )

    # Build the compaction request
    compact_msgs = build_compact_messages(messages, focus)

    # Call the LLM (non-streaming, with reduced thinking for speed)
    try:
        response = await call(
            provider=provider,
            model=model,
            messages=compact_msgs,
            tools=None,
            thinking=False,
            stream=False,
        )
        summary_content = response.get("content") or ""
    except Exception as exc:
        # Fallback: create a basic summary from message metadata
        summary_content = _fallback_summary(messages, str(exc))

    summary_text = parse_compact_summary(summary_content)
    after_tokens = estimate_tokens(summary_text)

    # Mark all existing messages as reverted
    db = await store.connect()
    await db.execute(
        "UPDATE messages SET reverted = 1 WHERE session_id = ?",
        (session_id,),
    )
    await db.commit()

    # Insert the compact summary as a system message
    compact_msg = Message(
        id=new_id("msg"),
        session_id=session_id,
        role="system",
        content=(
            "📦 **Conversation Compaction**\n\n"
            f"The following is a summary of {msg_count} previous messages "
            f"({before_tokens:,} estimated tokens → {after_tokens:,} tokens).\n\n"
            "---\n\n"
            f"{summary_text}\n\n"
            "---\n\n"
            "Previous messages have been compacted. Use this summary as context "
            "for the next user request."
        ),
        token_count=after_tokens,
    )
    await store.add_message(compact_msg)

    return {
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "saved_tokens": max(0, before_tokens - after_tokens),
        "message_count": msg_count,
        "summary_text": summary_text,
        "fallback": summary_content == "",
    }


def _fallback_summary(messages: list[Message], error: str) -> str:
    """Generate a basic summary when LLM call fails."""
    parts: list[str] = []
    parts.append(f"## Fallback Summary (LLM compaction failed: {error})\n")

    user_msgs = [m for m in messages if m.role == "user"]
    assistant_msgs = [m for m in messages if m.role == "assistant"]
    tool_msgs = [m for m in messages if m.role == "tool"]

    parts.append(f"- Total messages: {len(messages)}")
    parts.append(f"- User messages: {len(user_msgs)}")
    parts.append(f"- Assistant messages: {len(assistant_msgs)}")
    parts.append(f"- Tool results: {len(tool_msgs)}")

    if user_msgs:
        parts.append("\n### User requests:")
        for m in user_msgs:
            content = (m.content or "").strip()
            if len(content) > 200:
                content = content[:200] + "..."
            if content:
                parts.append(f"- {content}")

    # Collect file references from tool calls
    files_seen: set[str] = set()
    for m in assistant_msgs:
        if m.tool_calls_json:
            try:
                tcs = json.loads(m.tool_calls_json)
                for tc in tcs:
                    args = json.loads(tc["function"].get("arguments", "{}"))
                    for key in ("filePath", "file_path", "path"):
                        if key in args:
                            files_seen.add(str(args[key]))
            except (json.JSONDecodeError, KeyError):
                pass

    if files_seen:
        parts.append("\n### Files referenced:")
        for f in sorted(files_seen):
            parts.append(f"- `{f}`")

    return "\n".join(parts)
