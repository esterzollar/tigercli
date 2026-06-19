import json
import aiosqlite
from pathlib import Path
from tigercli.session.models import Session, Message, FileSnapshot, UsageEntry, new_id, now_iso
from tigercli.config import settings


class SessionStore:
    def __init__(self):
        self.db = None

    async def connect(self) -> aiosqlite.Connection:
        from tigercli.db import get_db
        if self.db is None:
            self.db = await get_db(settings.db_path)
        return self.db

    # ── Sessions ──────────────────────────────────────────────

    async def create_session(
        self,
        project_path: str,
        title: str | None = None,
        parent_id: str | None = None,
        branch_point_message_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        thinking_enabled: bool = False,
        reasoning_effort: str = "high",
        mode: str | None = None,
    ) -> Session:
        db = await self.connect()
        sid = new_id("ses")
        now = now_iso()
        sys_hash = _hash_system_prompt(project_path)

        await db.execute(
            """INSERT INTO sessions
               (id, parent_id, branch_point_message_id, title, project_path,
                remote_session_id, provider, model, thinking_enabled,
                reasoning_effort, system_prompt_hash, mode, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sid, parent_id, branch_point_message_id,
                title or "(untitled)", str(project_path),
                None,
                provider or settings.default_provider,
                model or settings.default_model,
                1 if thinking_enabled else 0,
                reasoning_effort, sys_hash,
                mode or settings.default_mode, now, now,
            ),
        )
        await db.commit()
        return await self.get_session(sid)

    async def fork_session(
        self,
        source_session_id: str,
        at_message_id: str | None = None,
        title: str | None = None,
    ) -> Session | None:
        """Branch a session at a specific message (or its current tail).

        Copies all messages up to and including `at_message_id` (or all
        messages if None) into a new session. The new session has parent_id
        pointing to the source and branch_point_message_id set so the UI
        can render the tree.
        """
        source = await self.get_session(source_session_id)
        if source is None:
            return None

        history = await self.get_messages(source_session_id, include_reverted=False)
        if at_message_id:
            cut: list[Message] = []
            for m in history:
                cut.append(m)
                if m.id == at_message_id:
                    break
            history = cut

        forked = await self.create_session(
            project_path=source.project_path,
            title=title or f"{source.title or 'session'} (fork)",
            parent_id=source.id,
            branch_point_message_id=at_message_id,
            provider=source.provider,
            model=source.model,
            thinking_enabled=source.thinking_enabled,
            reasoning_effort=source.reasoning_effort,
            mode=source.mode,
        )
        if forked is None:
            return None

        # Copy messages, regenerating IDs so they don't collide with the source.
        for m in history:
            cloned = Message(
                id=new_id("msg"),
                session_id=forked.id,
                role=m.role,
                content=m.content,
                reasoning_content=m.reasoning_content,
                tool_calls_json=m.tool_calls_json,
                tool_call_id=m.tool_call_id,
                parent_message_id=None,
                reverted=False,
                token_count=m.token_count,
            )
            await self.add_message(cloned)
        return forked

    async def get_session(self, session_id: str) -> Session | None:
        db = await self.connect()
        async with db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_session(row) if row else None

    async def list_sessions(self, limit: int = 20, status: str = "active") -> list[Session]:
        db = await self.connect()
        async with db.execute(
            "SELECT * FROM sessions WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_session(r) for r in rows]

    async def update_session(self, session_id: str, **kwargs) -> bool:
        db = await self.connect()
        allowed = {
            "title", "provider", "model", "thinking_enabled", "reasoning_effort",
            "status", "mode", "project_path", "remote_session_id",
            "pinned", "archived_at",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [now_iso(), session_id]
        await db.execute(
            f"UPDATE sessions SET {set_clause}, updated_at = ? WHERE id = ?", values
        )
        await db.commit()
        return True

    async def update_session_usage(
        self, session_id: str, tokens_in: int, tokens_out: int,
        cache_hit: int, cache_miss: int, cost_usd: float,
        cache_creation: int = 0,
    ) -> None:
        db = await self.connect()
        await db.execute(
            """UPDATE sessions SET
               total_tokens_in = total_tokens_in + ?,
               total_tokens_out = total_tokens_out + ?,
               total_cache_hit_tokens = total_cache_hit_tokens + ?,
               total_cache_miss_tokens = total_cache_miss_tokens + ?,
               total_cache_creation_tokens = total_cache_creation_tokens + ?,
               total_cost_usd = total_cost_usd + ?,
               total_requests = total_requests + 1,
               updated_at = ?
               WHERE id = ?""",
            (tokens_in, tokens_out, cache_hit, cache_miss, cache_creation,
             cost_usd, now_iso(), session_id),
        )
        await db.commit()

    async def archive_session(self, session_id: str) -> bool:
        return await self.update_session(session_id, status="archived", archived_at=now_iso())

    async def unarchive_session(self, session_id: str) -> bool:
        return await self.update_session(session_id, status="active", archived_at=None)

    async def pin_session(self, session_id: str, pinned: bool = True) -> bool:
        return await self.update_session(session_id, pinned=1 if pinned else 0)

    async def delete_session(self, session_id: str) -> None:
        db = await self.connect()
        # Defense-in-depth: explicit deletes in case FK pragma is off
        await db.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))
        await db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await db.execute("DELETE FROM file_snapshots WHERE session_id = ?", (session_id,))
        await db.execute("DELETE FROM usage_log WHERE session_id = ?", (session_id,))
        await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await db.commit()

    async def search_sessions(
        self,
        query: str = "",
        project_path: str | None = None,
        status: str | None = "active",
        limit: int = 50,
    ) -> list[Session]:
        """Search sessions by title and message content (FTS5)."""
        db = await self.connect()
        if query.strip():
            sql = """
                SELECT DISTINCT s.* FROM sessions s
                LEFT JOIN messages_fts fts ON fts.session_id = s.id
                WHERE (s.title LIKE ? OR fts.content MATCH ?)
            """
            params: list = [f"%{query}%", query]
        else:
            sql = "SELECT * FROM sessions s WHERE 1=1"
            params = []
        if status:
            sql += " AND s.status = ?"
            params.append(status)
        if project_path:
            sql += " AND s.project_path = ?"
            params.append(project_path)
        sql += " ORDER BY s.pinned DESC, s.updated_at DESC LIMIT ?"
        params.append(limit)
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_session(r) for r in rows]

    async def get_session_children(self, session_id: str) -> list[Session]:
        """Return sessions that branched off from this one."""
        db = await self.connect()
        async with db.execute(
            "SELECT * FROM sessions WHERE parent_id = ? ORDER BY created_at ASC",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_session(r) for r in rows]

    async def export_session(self, session_id: str, fmt: str = "markdown") -> str:
        """Export a session as Markdown or JSON."""
        session = await self.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        messages = await self.get_messages(session_id)

        if fmt == "json":
            payload = {
                "session": {
                    "id": session.id,
                    "title": session.title,
                    "project_path": session.project_path,
                    "provider": session.provider,
                    "model": session.model,
                    "mode": session.mode,
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                    "stats": {
                        "tokens_in": session.total_tokens_in,
                        "tokens_out": session.total_tokens_out,
                        "cache_hit": session.total_cache_hit_tokens,
                        "cache_miss": session.total_cache_miss_tokens,
                        "cost_usd": session.total_cost_usd,
                        "requests": session.total_requests,
                    },
                },
                "messages": [
                    {
                        "id": m.id,
                        "role": m.role,
                        "content": m.content,
                        "reasoning_content": m.reasoning_content,
                        "tool_calls": m.tool_calls,
                        "tool_call_id": m.tool_call_id,
                        "created_at": m.created_at,
                    }
                    for m in messages
                ],
            }
            return json.dumps(payload, indent=2, ensure_ascii=False)

        # Markdown export
        lines = [
            f"# {session.title or '(untitled)'}",
            "",
            f"- **Session:** `{session.id}`",
            f"- **Project:** {session.project_path}",
            f"- **Model:** {session.provider} / {session.model}",
            f"- **Created:** {session.created_at}",
            f"- **Tokens:** {session.total_tokens_in:,} in / {session.total_tokens_out:,} out",
            f"- **Cost:** ${session.total_cost_usd:.4f}",
            "",
            "---",
            "",
        ]
        for m in messages:
            if m.role == "user":
                lines.append(f"### 👤 User\n\n{m.content or ''}\n")
            elif m.role == "assistant":
                if m.reasoning_content:
                    lines.append(
                        f"### 🤖 Assistant\n\n<details><summary>Thinking</summary>\n\n"
                        f"{m.reasoning_content}\n\n</details>\n\n{m.content or ''}\n"
                    )
                else:
                    lines.append(f"### 🤖 Assistant\n\n{m.content or ''}\n")
            elif m.role == "tool":
                lines.append(f"<details><summary>🔧 Tool result</summary>\n\n```\n{m.content or ''}\n```\n\n</details>\n")
            elif m.role == "system":
                lines.append(f"_System: {m.content or ''}_\n")
        return "\n".join(lines)

    # ── Messages ──────────────────────────────────────────────

    async def add_message(self, msg: Message) -> None:
        db = await self.connect()
        await db.execute(
            """INSERT INTO messages
               (id, session_id, role, content, reasoning_content, tool_calls_json,
                tool_call_id, parent_message_id, reverted, token_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg.id, msg.session_id, msg.role, msg.content,
                msg.reasoning_content, msg.tool_calls_json,
                msg.tool_call_id, msg.parent_message_id,
                1 if msg.reverted else 0, msg.token_count, msg.created_at,
            ),
        )
        # Index user/assistant message bodies for full-text search. Tool and
        # system messages are noisy and rarely the target of a search.
        if msg.role in ("user", "assistant") and msg.content:
            try:
                await db.execute(
                    "INSERT INTO messages_fts (session_id, role, content) VALUES (?, ?, ?)",
                    (msg.session_id, msg.role, msg.content),
                )
            except Exception:
                pass
        await db.commit()

    async def get_messages(self, session_id: str, include_reverted: bool = False) -> list[Message]:
        db = await self.connect()
        query = "SELECT * FROM messages WHERE session_id = ?"
        params = [session_id]
        if not include_reverted:
            query += " AND reverted = 0"
        query += " ORDER BY created_at ASC"
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_message(r) for r in rows]

    async def revert_last_messages(self, session_id: str, count: int = 1) -> list[Message]:
        db = await self.connect()
        messages = await self.get_messages(session_id)
        to_revert = [m for m in reversed(messages) if m.role in ("user", "assistant")][:count]
        for m in to_revert:
            await db.execute("UPDATE messages SET reverted = 1 WHERE id = ?", (m.id,))
        await db.commit()
        return to_revert

    async def unrevert_last(self, session_id: str) -> Message | None:
        db = await self.connect()
        async with db.execute(
            "SELECT * FROM messages WHERE session_id = ? AND reverted = 1 ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if row:
            await db.execute("UPDATE messages SET reverted = 0 WHERE id = ?", (row["id"],))
            await db.commit()
            return _row_to_message(row)
        return None

    async def get_message_tree(self, message_id: str) -> list[Message]:
        db = await self.connect()
        async with db.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ) as cur:
            msg = await cur.fetchone()
        if not msg:
            return []
        async with db.execute(
            "SELECT * FROM messages WHERE session_id = ? AND created_at >= (SELECT created_at FROM messages WHERE id = ?)",
            (msg["session_id"], message_id),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_message(r) for r in rows]

    # ── Snapshots ─────────────────────────────────────────────

    async def create_snapshot(self, snap: FileSnapshot) -> None:
        db = await self.connect()
        await db.execute(
            """INSERT INTO file_snapshots (id, session_id, message_id, file_path, content_before, content_after, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (snap.id, snap.session_id, snap.message_id, snap.file_path,
             snap.content_before, snap.content_after, snap.created_at),
        )
        await db.commit()

    async def get_snapshots_for_message(self, message_id: str) -> list[FileSnapshot]:
        db = await self.connect()
        async with db.execute(
            "SELECT * FROM file_snapshots WHERE message_id = ?", (message_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_snapshot(r) for r in rows]

    async def revert_snapshots(self, message_id: str) -> list[FileSnapshot]:
        snaps = await self.get_snapshots_for_message(message_id)
        for snap in snaps:
            if snap.content_before is not None:
                path = Path(snap.file_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(snap.content_before)
        return snaps

    # ── Usage ─────────────────────────────────────────────────

    async def log_usage(self, entry: UsageEntry) -> None:
        db = await self.connect()
        await db.execute(
            """INSERT INTO usage_log
               (id, session_id, provider, model, endpoint, input_tokens, output_tokens,
                cache_hit_tokens, cache_miss_tokens, cost_usd, latency_ms,
                status_code, error_type, streamed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.id, entry.session_id, entry.provider, entry.model,
                entry.endpoint,
                entry.input_tokens, entry.output_tokens,
                entry.cache_hit_tokens, entry.cache_miss_tokens,
                entry.cost_usd, entry.latency_ms, entry.status_code,
                entry.error_type, 1 if entry.streamed else 0,
                entry.created_at,
            ),
        )
        await db.commit()

    async def get_session_stats(self, session_id: str) -> dict:
        db = await self.connect()
        async with db.execute(
            """SELECT SUM(input_tokens) as total_in, SUM(output_tokens) as total_out,
               SUM(cache_hit_tokens) as cache_hit, SUM(cache_miss_tokens) as cache_miss,
               SUM(cost_usd) as cost, COUNT(*) as requests
               FROM usage_log WHERE session_id = ?""",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        return {k: (row[k] or 0) for k in row.keys()} if row else {}

    async def get_total_stats(self) -> dict:
        db = await self.connect()
        async with db.execute(
            """SELECT SUM(input_tokens) as total_in, SUM(output_tokens) as total_out,
               SUM(cache_hit_tokens) as cache_hit, SUM(cache_miss_tokens) as cache_miss,
               SUM(cost_usd) as cost, COUNT(*) as requests
               FROM usage_log"""
        ) as cur:
            row = await cur.fetchone()
        return {k: (row[k] or 0) for k in row.keys()} if row else {}

    # ── Plugins ───────────────────────────────────────────────

    async def add_plugin(self, name: str, version: str = "0.1.0",
                         description: str | None = None, author: str | None = None,
                         hooks: list[str] | None = None) -> str:
        db = await self.connect()
        pid = new_id("plg")
        await db.execute(
            """INSERT INTO plugins (id, name, version, description, author, hooks_json, installed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (pid, name, version, description, author, json.dumps(hooks or []), now_iso()),
        )
        await db.commit()
        return pid

    async def list_plugins(self) -> list[dict]:
        db = await self.connect()
        async with db.execute("SELECT * FROM plugins ORDER BY name") as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def remove_plugin(self, name: str) -> bool:
        db = await self.connect()
        cur = await db.execute("DELETE FROM plugins WHERE name = ?", (name,))
        await db.commit()
        return cur.rowcount > 0

    # ── MCP Servers ───────────────────────────────────────────

    async def add_mcp_server(self, name: str, transport: str = "stdio",
                             command: str | None = None, args: list[str] | None = None,
                             env: dict | None = None, url: str | None = None) -> str:
        db = await self.connect()
        mid = new_id("mcp")
        await db.execute(
            """INSERT INTO mcp_servers (id, name, transport, command, args_json, env_json, url, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (mid, name, transport, command, json.dumps(args or []),
             json.dumps(env or {}), url, now_iso()),
        )
        await db.commit()
        return mid

    async def list_mcp_servers(self) -> list[dict]:
        db = await self.connect()
        async with db.execute("SELECT * FROM mcp_servers ORDER BY name") as cur:
            rows = await cur.fetchall()
        return [_row_to_mcp(r) for r in rows]

    async def remove_mcp_server(self, server_id: str) -> bool:
        db = await self.connect()
        cur = await db.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
        await db.commit()
        return cur.rowcount > 0

    async def update_mcp_tools_cache(self, server_id: str, tools: list[dict]) -> None:
        db = await self.connect()
        await db.execute(
            "UPDATE mcp_servers SET tools_cache_json = ?, tools_cache_at = ? WHERE id = ?",
            (json.dumps(tools), now_iso(), server_id),
        )
        await db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None


# ── Row converters ─────────────────────────────────────────────

def _safe_row(row, key, default=None):
    try:
        v = row[key]
        return v if v is not None else default
    except (KeyError, IndexError):
        return default


def _row_to_session(row) -> Session:
    return Session(
        id=row["id"], parent_id=row["parent_id"], title=row["title"],
        branch_point_message_id=_safe_row(row, "branch_point_message_id"),
        project_path=row["project_path"], provider=row["provider"],
        remote_session_id=_safe_row(row, "remote_session_id"),
        model=row["model"],
        thinking_enabled=bool(row["thinking_enabled"]),
        reasoning_effort=row["reasoning_effort"],
        system_prompt_hash=row["system_prompt_hash"],
        mode=_safe_row(row, "mode", "build"),
        status=row["status"],
        total_tokens_in=_safe_row(row, "total_tokens_in") or _safe_row(row, "tokens_in") or 0,
        total_tokens_out=_safe_row(row, "total_tokens_out") or _safe_row(row, "tokens_out") or 0,
        total_cache_hit_tokens=_safe_row(row, "total_cache_hit_tokens") or _safe_row(row, "cache_hit_tokens") or 0,
        total_cache_miss_tokens=_safe_row(row, "total_cache_miss_tokens") or _safe_row(row, "cache_miss_tokens") or 0,
        total_cache_creation_tokens=_safe_row(row, "total_cache_creation_tokens") or 0,
        total_cost_usd=_safe_row(row, "total_cost_usd") or _safe_row(row, "cost_usd") or 0.0,
        total_requests=_safe_row(row, "total_requests") or 0,
        pinned=bool(_safe_row(row, "pinned") or 0),
        archived_at=_safe_row(row, "archived_at"),
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _row_to_message(row) -> Message:
    return Message(
        id=row["id"], session_id=row["session_id"], role=row["role"],
        content=row["content"], reasoning_content=row["reasoning_content"],
        tool_calls_json=row["tool_calls_json"],
        tool_call_id=row["tool_call_id"],
        parent_message_id=row["parent_message_id"],
        reverted=bool(row["reverted"]),
        token_count=row["token_count"] or 0,
        created_at=row["created_at"],
    )


def _row_to_snapshot(row) -> FileSnapshot:
    return FileSnapshot(
        id=row["id"], session_id=row["session_id"],
        message_id=row["message_id"], file_path=row["file_path"],
        content_before=row["content_before"],
        content_after=row["content_after"],
        created_at=row["created_at"],
    )


def _row_to_mcp(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "transport": row["transport"],
        "command": row["command"],
        "args": json.loads(row["args_json"] or "[]"),
        "env": json.loads(row["env_json"] or "{}"),
        "url": row["url"],
        "enabled": bool(row["enabled"]),
        "tools": json.loads(row["tools_cache_json"]) if row["tools_cache_json"] else [],
        "created_at": row["created_at"],
    }


def _hash_system_prompt(project_path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    h.update(f"tigercli-v1:{project_path}".encode())
    return h.hexdigest()[:16]
