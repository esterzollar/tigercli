import aiosqlite
from pathlib import Path

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES sessions(id),
    branch_point_message_id TEXT,
    title TEXT,
    project_path TEXT NOT NULL,
    remote_session_id TEXT,
    provider TEXT NOT NULL DEFAULT 'deepseek',
    model TEXT NOT NULL DEFAULT 'deepseek-v4-pro',
    thinking_enabled INTEGER DEFAULT 0,
    reasoning_effort TEXT DEFAULT 'high',
    system_prompt_hash TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'build',
    status TEXT NOT NULL DEFAULT 'active',
    total_tokens_in INTEGER DEFAULT 0,
    total_tokens_out INTEGER DEFAULT 0,
    total_cache_hit_tokens INTEGER DEFAULT 0,
    total_cache_miss_tokens INTEGER DEFAULT 0,
    total_cache_creation_tokens INTEGER DEFAULT 0,
    total_cost_usd REAL DEFAULT 0.0,
    total_requests INTEGER DEFAULT 0,
    pinned INTEGER DEFAULT 0,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT,
    reasoning_content TEXT,
    tool_calls_json TEXT,
    tool_call_id TEXT,
    parent_message_id TEXT REFERENCES messages(id),
    reverted INTEGER DEFAULT 0,
    token_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_snapshots (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id TEXT REFERENCES messages(id),
    file_path TEXT NOT NULL,
    content_before TEXT,
    content_after TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    endpoint TEXT NOT NULL DEFAULT '/v1/chat/completions',
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_hit_tokens INTEGER DEFAULT 0,
    cache_miss_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    latency_ms INTEGER DEFAULT 0,
    status_code INTEGER DEFAULT 200,
    error_type TEXT,
    streamed INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plugins (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    version TEXT NOT NULL DEFAULT '0.1.0',
    description TEXT,
    author TEXT,
    hooks_json TEXT NOT NULL DEFAULT '[]',
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    installed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    transport TEXT NOT NULL DEFAULT 'stdio',
    command TEXT,
    args_json TEXT NOT NULL DEFAULT '[]',
    env_json TEXT NOT NULL DEFAULT '{}',
    url TEXT,
    enabled INTEGER DEFAULT 1,
    tools_cache_json TEXT,
    tools_cache_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON file_snapshots(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_log(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_log(created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_path, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_id);

-- Full-text search on user/assistant message content. Built lazily by the
-- store via INSERT triggers on the messages table.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    session_id UNINDEXED,
    role UNINDEXED,
    content,
    tokenize='porter unicode61'
);
"""


async def get_db(db_path: str | Path) -> aiosqlite.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await _migrate(db)
    return db


async def _migrate(db: aiosqlite.Connection) -> None:
    await db.executescript(SCHEMA)

    async with db.execute("SELECT MAX(version) FROM schema_version") as cur:
        row = await cur.fetchone()
    current = row[0] if row[0] is not None else 0

    if current < 1:
        await db.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (1)")
        current = 1

    if current < 2:
        # v1 → v2: add mode, total_* columns, rename tokens_* → total_tokens_*
        for col in ["mode"]:
            try:
                await db.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT DEFAULT 'build'")
            except Exception:
                pass
        for col in ["total_tokens_in", "total_tokens_out", "total_cache_hit_tokens", "total_cache_miss_tokens", "total_requests"]:
            try:
                await db.execute(f"ALTER TABLE sessions ADD COLUMN {col} INTEGER DEFAULT 0")
            except Exception:
                pass
        for col in ["total_cost_usd"]:
            try:
                await db.execute(f"ALTER TABLE sessions ADD COLUMN {col} REAL DEFAULT 0.0")
            except Exception:
                pass
        for col in ["endpoint", "streamed"]:
            try:
                await db.execute(f"ALTER TABLE usage_log ADD COLUMN {col} TEXT")
            except Exception:
                pass
        try:
            await db.execute("ALTER TABLE usage_log ADD COLUMN streamed INTEGER DEFAULT 0")
        except Exception:
            pass
        # Copy old values to new columns if they exist
        try:
            await db.execute("UPDATE sessions SET total_tokens_in = tokens_in, total_tokens_out = tokens_out, total_cache_hit_tokens = cache_hit_tokens, total_cache_miss_tokens = cache_miss_tokens, total_cost_usd = cost_usd")
        except Exception:
            pass
        await db.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (2)")
        current = 2

    if current < 3:
        try:
            await db.execute("ALTER TABLE sessions ADD COLUMN remote_session_id TEXT")
        except Exception:
            pass
        await db.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (3)")
        current = 3

    if current < 4:
        # v3 → v4: branch tracking, cache creation tokens, pinned/archived,
        # FTS index over message bodies.
        for stmt in [
            "ALTER TABLE sessions ADD COLUMN branch_point_message_id TEXT",
            "ALTER TABLE sessions ADD COLUMN total_cache_creation_tokens INTEGER DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN pinned INTEGER DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN archived_at TEXT",
        ]:
            try:
                await db.execute(stmt)
            except Exception:
                pass
        await db.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (4)")
        current = 4

    if current < SCHEMA_VERSION:
        await db.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
    await db.commit()
