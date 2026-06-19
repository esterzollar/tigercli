"""Unified prompt-caching layer for TigerLiteCode.

Three-tier strategy that works across providers:

1. **Provider-native caching** (free, automatic).
   We make the message *prefix* deterministic and stable so the upstream
   provider's KV / prompt cache hits maximally. We then read whichever cache
   field that provider exposes and surface a unified
   `(cache_hit_tokens, cache_miss_tokens)` pair to the rest of the app.

   Supported provider response shapes:
     - DeepSeek:  usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens
     - OpenAI:    usage.prompt_tokens_details.cached_tokens (auto)
     - Anthropic: usage.cache_read_input_tokens / cache_creation_input_tokens
                  (when the provider passes through cache_control markers)
     - Generic:   anything OpenAI-compatible falls back to OpenAI shape.

2. **Anthropic-style cache_control passthrough**.
   For OpenAI-compatible endpoints that advertise Anthropic-style caching
   (some routers do, opencode-go can), we tag the stable prefix message
   blocks with `{"cache_control": {"type": "ephemeral"}}`. Providers that
   don't support it ignore the unknown field — safe by default.

3. **Local content-addressed prefix cache** (client-side, optional).
   Hash the rendered system + project-context prefix; record what we last
   sent under that hash. If the same prefix repeats next turn we tell the
   user "warm prefix" so they know to expect a cache hit. We DO NOT cache
   responses — that breaks correctness for tool-using agents.

   The local cache is also used to compute a "session warmth" indicator:
   a fresh session is cold; a session reusing the same project prefix as a
   prior session is warm.

The whole module is best-effort: if any of this fails we fall through to
"no cache info, charge full price" rather than blocking the agent.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tigercli.config import settings


# ── Capability matrix ──────────────────────────────────────────────────

# Providers known to support Anthropic-style cache_control passthrough.
# When a provider isn't on this list we still try native caching but skip
# emitting cache_control markers (some servers reject unknown fields).
_CACHE_CONTROL_PROVIDERS: frozenset[str] = frozenset({
    "anthropic",
    "opencode-go",   # opencode router supports passthrough for Claude models
    "opencode-zen",  # same router family
})

# Providers that report cache_hit_tokens automatically without any client
# action. The agent-loop reads these via `extract_cache_usage` below.
_NATIVE_CACHE_PROVIDERS: frozenset[str] = frozenset({
    "deepseek",
    "openai",
    "anthropic",
    "opencode-go",
    "opencode-zen",
})


def supports_cache_control(provider: str, model: str = "") -> bool:
    """True iff we should add `cache_control` markers for this provider."""
    if provider in _CACHE_CONTROL_PROVIDERS:
        # cache_control is meaningful for Claude-family models only
        ml = model.lower()
        return any(t in ml for t in ("claude", "anthropic", "sonnet", "haiku", "opus"))
    return False


def supports_native_cache(provider: str) -> bool:
    """True iff the provider reports cache stats in usage."""
    return provider in _NATIVE_CACHE_PROVIDERS


# ── Cache-usage extraction ────────────────────────────────────────────


@dataclass(frozen=True)
class CacheUsage:
    """Normalized cache accounting for one request."""
    hit_tokens: int = 0
    miss_tokens: int = 0
    creation_tokens: int = 0  # Anthropic-only: tokens written into cache

    @property
    def total_input(self) -> int:
        return self.hit_tokens + self.miss_tokens + self.creation_tokens

    @property
    def hit_rate(self) -> float:
        total = self.total_input
        return self.hit_tokens / total if total > 0 else 0.0


def extract_cache_usage(usage: dict[str, Any] | Any, provider: str) -> CacheUsage:
    """Extract a normalized CacheUsage from a provider response.

    Accepts both dict-shaped usage (already parsed) and the OpenAI SDK's
    object-shaped usage (with `prompt_tokens_details.cached_tokens` etc.).
    """
    if usage is None:
        return CacheUsage()

    def _get(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    prompt_tokens = int(_get(usage, "prompt_tokens", 0) or 0)

    # DeepSeek native fields
    ds_hit = int(_get(usage, "prompt_cache_hit_tokens", 0) or 0)
    ds_miss = int(_get(usage, "prompt_cache_miss_tokens", 0) or 0)
    if ds_hit or ds_miss:
        return CacheUsage(hit_tokens=ds_hit, miss_tokens=ds_miss)

    # Anthropic native fields (also surface via opencode-go for Claude)
    a_read = int(_get(usage, "cache_read_input_tokens", 0) or 0)
    a_create = int(_get(usage, "cache_creation_input_tokens", 0) or 0)
    if a_read or a_create:
        miss = max(0, prompt_tokens - a_read - a_create)
        return CacheUsage(
            hit_tokens=a_read,
            miss_tokens=miss,
            creation_tokens=a_create,
        )

    # OpenAI prompt_tokens_details.cached_tokens
    details = _get(usage, "prompt_tokens_details")
    if details is not None:
        cached = int(_get(details, "cached_tokens", 0) or 0)
        if cached or prompt_tokens:
            return CacheUsage(
                hit_tokens=cached,
                miss_tokens=max(0, prompt_tokens - cached),
            )

    # Nothing reported — treat the whole prompt as a miss so we can still
    # show *some* token counts.
    if prompt_tokens:
        return CacheUsage(hit_tokens=0, miss_tokens=prompt_tokens)
    return CacheUsage()


# ── cache_control passthrough ─────────────────────────────────────────


def annotate_cache_breakpoints(
    messages: list[dict],
    provider: str,
    model: str,
    max_breakpoints: int = 4,
) -> list[dict]:
    """Add Anthropic `cache_control: ephemeral` markers to stable prefix blocks.

    Anthropic allows up to 4 cache breakpoints. We place them on the LAST
    stable-prefix message: the runtime-context block (or the project-context,
    or the system prompt — whichever is the last system/user message before
    fresh conversation history).

    Mutates a *copy* of the messages list — the original is untouched.
    Returns the (possibly modified) list. If the provider/model doesn't
    support passthrough we return `messages` unchanged.
    """
    if not supports_cache_control(provider, model):
        return messages

    out = [dict(m) for m in messages]

    # Find candidate breakpoint indices: trailing edge of system/file blocks,
    # i.e. the last `system` message at the front, plus optionally the last
    # `user` message that contains a <file …> block.
    system_indices = [i for i, m in enumerate(out) if m.get("role") == "system"]
    file_indices = [
        i for i, m in enumerate(out)
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and m["content"].startswith("<file")
    ]

    # Keep at most `max_breakpoints` markers, prioritising rightmost stable
    # blocks (those have the most cumulative cached tokens).
    candidates = sorted(set(system_indices + file_indices))[-max_breakpoints:]

    for idx in candidates:
        msg = out[idx]
        content = msg.get("content")
        if isinstance(content, str):
            # Convert string content → blocks form so cache_control attaches.
            msg["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }]
        elif isinstance(content, list) and content:
            # Already block form — tag the last block.
            last = dict(content[-1])
            last["cache_control"] = {"type": "ephemeral"}
            msg["content"] = list(content[:-1]) + [last]

    return out


# ── Local prompt-prefix cache (warmth tracker) ────────────────────────


def _prefix_cache_dir() -> Path:
    d = settings.cache_home / "prefix"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hash_messages_prefix(messages: list[dict], prefix_len: int) -> str:
    """Hash a stable prefix of N messages to a content-addressed key."""
    h = hashlib.sha256()
    for m in messages[:prefix_len]:
        # Include role + canonicalised content
        h.update((m.get("role") or "").encode())
        h.update(b"\x1e")
        content = m.get("content")
        if isinstance(content, str):
            h.update(content.encode("utf-8", errors="replace"))
        else:
            try:
                h.update(json.dumps(content, sort_keys=True).encode())
            except (TypeError, ValueError):
                h.update(str(content).encode())
        h.update(b"\x1f")
    return h.hexdigest()[:16]


def stable_prefix_length(messages: list[dict]) -> int:
    """Number of leading messages that form the cache-stable prefix.

    Heuristic: every message at the head whose role is `system` or whose
    content is a `<file …>…</file>` user block is part of the prefix.
    The first conversational user/assistant turn breaks the prefix.
    """
    n = 0
    for m in messages:
        role = m.get("role")
        if role == "system":
            n += 1
            continue
        if role == "user" and isinstance(m.get("content"), str) and m["content"].startswith("<file"):
            n += 1
            continue
        break
    return n


def record_prefix(provider: str, model: str, messages: list[dict]) -> dict:
    """Record/update the local prefix cache and return warmth metadata.

    Returns a dict:
      {
        "prefix_hash": "<16-hex>",
        "prefix_messages": <int>,
        "warm": bool,           # True iff we've sent this prefix before
        "first_seen": <iso-ts>,
        "hit_count": <int>,
      }
    """
    plen = stable_prefix_length(messages)
    if plen == 0:
        return {"prefix_hash": "", "prefix_messages": 0, "warm": False, "hit_count": 0}

    prefix_hash = _hash_messages_prefix(messages, plen)
    cache_dir = _prefix_cache_dir()
    record_path = cache_dir / f"{provider}_{prefix_hash}.json"

    now = int(time.time())
    if record_path.exists():
        try:
            data = json.loads(record_path.read_text())
            data["hit_count"] = int(data.get("hit_count", 0)) + 1
            data["last_seen"] = now
            data["model"] = model
            record_path.write_text(json.dumps(data))
            return {
                "prefix_hash": prefix_hash,
                "prefix_messages": plen,
                "warm": True,
                "hit_count": data["hit_count"],
                "first_seen": data.get("first_seen", now),
            }
        except (OSError, ValueError):
            pass

    # Fresh prefix
    data = {
        "provider": provider,
        "model": model,
        "first_seen": now,
        "last_seen": now,
        "hit_count": 1,
        "prefix_messages": plen,
    }
    try:
        record_path.write_text(json.dumps(data))
    except OSError:
        pass
    return {
        "prefix_hash": prefix_hash,
        "prefix_messages": plen,
        "warm": False,
        "hit_count": 1,
        "first_seen": now,
    }


def gc_prefix_cache(max_age_days: int = 14) -> int:
    """Remove cold prefix records older than `max_age_days`. Returns removed count."""
    cache_dir = _prefix_cache_dir()
    cutoff = int(time.time()) - max_age_days * 86400
    removed = 0
    for f in cache_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if int(data.get("last_seen", 0)) < cutoff:
                f.unlink()
                removed += 1
        except (OSError, ValueError):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# ── Pricing-aware cost estimation ─────────────────────────────────────


# Per-1M-tokens pricing for known models. Values in USD.
# Keep this list small and current — exotic models default to a "miss" rate.
_PRICING: dict[str, dict[str, float]] = {
    # DeepSeek
    "deepseek-v4-pro": {"input": 0.435, "output": 0.87, "cache_hit": 0.003625},
    "deepseek-v4-flash": {"input": 0.14, "output": 0.28, "cache_hit": 0.0028},
    "deepseek-chat": {"input": 0.14, "output": 0.28, "cache_hit": 0.0028},
    "deepseek-reasoner": {"input": 0.435, "output": 0.87, "cache_hit": 0.003625},
    # OpenAI (latest as of 2026 — adjust as needed)
    "gpt-4o": {"input": 2.50, "output": 10.00, "cache_hit": 1.25},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_hit": 0.075},
    # Anthropic via opencode-go
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_hit": 0.30, "cache_creation": 3.75},
    "claude-haiku-4": {"input": 1.00, "output": 5.00, "cache_hit": 0.10, "cache_creation": 1.25},
}


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_hit_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Estimate USD cost for one request, given normalized token counts.

    Falls back to the cheapest known model's miss rate if `model` is unknown,
    so we still produce a directionally-correct number rather than zero.
    """
    pricing = _PRICING.get(model)
    if pricing is None:
        # Try a fuzzy match so e.g. "deepseek-chat-2026-01" picks up the
        # deepseek-chat tier.
        for known, p in _PRICING.items():
            if model.startswith(known):
                pricing = p
                break
    if pricing is None:
        pricing = {"input": 0.14, "output": 0.28, "cache_hit": 0.0028, "cache_creation": 0.14}

    miss = max(0, input_tokens - cache_hit_tokens - cache_creation_tokens)
    cost = (
        cache_hit_tokens / 1_000_000 * pricing.get("cache_hit", pricing["input"])
        + cache_creation_tokens / 1_000_000 * pricing.get("cache_creation", pricing["input"])
        + miss / 1_000_000 * pricing["input"]
        + output_tokens / 1_000_000 * pricing["output"]
    )
    return round(cost, 6)
