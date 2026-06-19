import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def new_id(prefix: str = "") -> str:
    uid = uuid.uuid4().hex[:12]
    return f"{prefix}_{uid}" if prefix else uid


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Message:
    id: str
    session_id: str
    role: str
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls_json: str | None = None
    tool_call_id: str | None = None
    parent_message_id: str | None = None
    reverted: bool = False
    token_count: int = 0
    created_at: str = field(default_factory=now_iso)

    @property
    def tool_calls(self) -> list[dict] | None:
        # Cache the parsed result, invalidating if the source JSON changes,
        # so repeated accesses (e.g. in to_openai) don't re-parse every time.
        if not self.tool_calls_json:
            self._tool_calls_cache = None
            self._tool_calls_cache_src = None
            return None
        if getattr(self, "_tool_calls_cache_src", None) != self.tool_calls_json:
            self._tool_calls_cache = json.loads(self.tool_calls_json)
            self._tool_calls_cache_src = self.tool_calls_json
        return self._tool_calls_cache

    def to_openai(self) -> dict:
        msg: dict = {"role": self.role}
        if self.content is not None:
            msg["content"] = self.content
        if self.reasoning_content:
            msg["reasoning_content"] = self.reasoning_content
        tcs = self.tool_calls
        if tcs:
            msg["tool_calls"] = tcs
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        return msg


@dataclass
class Session:
    id: str
    project_path: str
    title: str | None = None
    parent_id: str | None = None
    branch_point_message_id: str | None = None
    remote_session_id: str | None = None
    provider: str = "deepseek"
    model: str = "deepseek-v4-pro"
    thinking_enabled: bool = False
    reasoning_effort: str = "high"
    system_prompt_hash: str = ""
    mode: str = "build"
    status: str = "active"
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cache_hit_tokens: int = 0
    total_cache_miss_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    total_requests: int = 0
    pinned: bool = False
    archived_at: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @property
    def cache_hit_rate(self) -> float:
        total = (
            self.total_cache_hit_tokens
            + self.total_cache_miss_tokens
            + self.total_cache_creation_tokens
        )
        if total == 0:
            return 0.0
        return self.total_cache_hit_tokens / total


@dataclass
class FileSnapshot:
    id: str
    session_id: str
    message_id: str
    file_path: str
    content_before: str | None = None
    content_after: str | None = None
    created_at: str = field(default_factory=now_iso)


@dataclass
class UsageEntry:
    id: str
    session_id: str | None = None
    provider: str = "deepseek"
    model: str = "deepseek-v4-pro"
    endpoint: str = "/v1/chat/completions"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    status_code: int = 200
    error_type: str | None = None
    streamed: bool = False
    created_at: str = field(default_factory=now_iso)
