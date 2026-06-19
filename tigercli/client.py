"""Provider clients — direct OpenAI-compatible HTTP for every provider.

We never go through `opencode serve`. We hit each provider's API directly:
  - DeepSeek    → https://api.deepseek.com
  - OpenAI      → https://api.openai.com/v1
  - opencode-go → https://opencode.ai/zen/go/v1
  - opencode-zen→ https://opencode.ai/zen/v1
  - generic     → whatever base_url the user configured

Cache hit/miss tokens are normalised across providers via
`tigercli.common.cache.extract_cache_usage`.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from openai import AsyncOpenAI

from tigercli.common.cache import (
    annotate_cache_breakpoints,
    extract_cache_usage,
    record_prefix,
    supports_cache_control,
)
from tigercli.config import settings, provider_api_key_from_env


# ── Provider client factory ───────────────────────────────────────────


def _get_opencode_api_key(provider: str = "opencode") -> str:
    from tigercli.config import load_auth

    auth = load_auth()
    # Use the provider's own slot, then the legacy "opencode" slot only.
    # Do NOT fall through to the *other* opencode tier (go<->zen): their key
    # namespaces differ and cross-using a key yields confusing 401s.
    cfg = auth.get(provider, {}) or auth.get("opencode", {})
    if not isinstance(cfg, dict):
        cfg = {}
    return (
        cfg.get("api_key")
        or os.environ.get("OPENCODE_API_KEY", "")
        or os.environ.get("TIGERCLI_OPENCODE_API_KEY", "")
        or settings.opencode_api_key
        or ""
    )


def _generic_provider_config(provider: str) -> tuple[str, str]:
    """Return (api_key, base_url) for a user-defined OpenAI-compatible provider."""
    from tigercli.config import load_auth

    auth = load_auth()
    cfg = auth.get(provider, {}) if isinstance(auth, dict) else {}
    if not isinstance(cfg, dict):
        cfg = {}
    api_key = (
        cfg.get("api_key")
        or provider_api_key_from_env(provider)
        or ""
    )
    base_url = cfg.get("base_url") or ""
    return api_key, base_url


_clients: dict[str, AsyncOpenAI] = {}

def get_client(provider: str) -> AsyncOpenAI:
    if provider in _clients:
        return _clients[provider]

    if provider == "deepseek":
        api_key = settings.deepseek_api_key or provider_api_key_from_env("deepseek")
        client = AsyncOpenAI(api_key=api_key, base_url=settings.deepseek_base_url)
    elif provider in {"opencode", "opencode-zen"}:
        client = AsyncOpenAI(
            api_key=_get_opencode_api_key(provider),
            base_url=settings.opencode_zen_base_url,
        )
    elif provider == "opencode-go":
        client = AsyncOpenAI(
            api_key=_get_opencode_api_key(provider),
            base_url=settings.opencode_go_base_url,
        )
    elif provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "")
        client = AsyncOpenAI(api_key=api_key, base_url="https://api.openai.com/v1")
    else:
        # Generic OpenAI-compatible provider
        api_key, base_url = _generic_provider_config(provider)
        if not base_url:
            raise ValueError(
                f"Unknown provider '{provider}' and no base_url configured. "
                "Add it via /connect in the TUI or write base_url into auth.json."
            )
        client = AsyncOpenAI(api_key=api_key or "anonymous", base_url=base_url)

    _clients[provider] = client
    return client


def evict_client(provider: str | None = None) -> None:
    """Drop cached client(s) so the next call rebuilds with fresh credentials.

    Call this after an API key / base_url change so revoked or rotated keys
    are not reused from the module-level cache.
    """
    if provider is None:
        _clients.clear()
    else:
        _clients.pop(provider, None)


async def close_clients() -> None:
    """Close all cached OpenAI clients to release connection pools."""
    for client in list(_clients.values()):
        try:
            # AsyncOpenAI.aclose() is a coroutine and must be awaited;
            # calling .close() would create an un-awaited coroutine.
            await client.close()
        except Exception:
            pass
    _clients.clear()


# ── Main entry point ──────────────────────────────────────────────────


async def call(
    provider: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    thinking: bool = False,
    reasoning_effort: str = "high",
    stream: bool = False,
    max_tokens: int | None = None,
    on_token: Callable[[str], Awaitable[None]] | None = None,
    on_reasoning: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    """Call the LLM. Returns a parsed response dict."""

    # Normalise opencode/* prefixes that some UIs emit.
    if provider == "opencode" and model.startswith("opencode-go/"):
        provider = "opencode-go"
        model = model.removeprefix("opencode-go/")
    elif provider == "opencode" and model.startswith("opencode-zen/"):
        provider = "opencode-zen"
        model = model.removeprefix("opencode-zen/")

    # Track local prefix warmth (best-effort; never blocks).
    try:
        prefix_info = record_prefix(provider, model, messages)
    except Exception:
        prefix_info = {"warm": False, "prefix_hash": ""}

    # Annotate cache_control markers for providers that support passthrough.
    request_messages = (
        annotate_cache_breakpoints(messages, provider, model)
        if supports_cache_control(provider, model)
        else messages
    )

    client = get_client(provider)
    kwargs: dict = {
        "model": model,
        "messages": request_messages,
        "stream": stream,
    }
    if stream:
        # Ask the server to include usage stats on the *final* chunk so we
        # can record cache hit/miss after streaming completes. OpenAI &
        # DeepSeek both honour this opt-in.
        kwargs["stream_options"] = {"include_usage": True}
    if tools:
        kwargs["tools"] = tools
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if thinking and provider == "deepseek":
        kwargs["reasoning_effort"] = reasoning_effort
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

    response = await client.chat.completions.create(**kwargs)

    if stream:
        result = await _parse_stream(response, provider, on_token, on_reasoning)
    else:
        result = _parse_response(response, provider)

    # Attach prefix-warmth metadata (UI uses it to render a 'warm prefix' hint).
    result.setdefault("metadata", {})["prefix"] = prefix_info
    return result


# ── Stream parser ─────────────────────────────────────────────────────


async def _parse_stream(response, provider: str, on_token=None, on_reasoning=None) -> dict:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    finish_reason: str | None = None
    final_usage: object | None = None

    async for chunk in response:
        if getattr(chunk, "usage", None):
            # OpenAI/DeepSeek emit usage on the last chunk when
            # stream_options.include_usage=true. Capture it; don't let the
            # absence of choices stop us.
            final_usage = chunk.usage

        if not chunk.choices:
            continue

        choice = chunk.choices[0]
        finish_reason = choice.finish_reason or finish_reason
        delta = choice.delta

        token = getattr(delta, "content", None) or ""
        if token:
            content_parts.append(token)
            if on_token:
                await on_token(token)

        reasoning = getattr(delta, "reasoning_content", None) or ""
        if reasoning:
            reasoning_parts.append(reasoning)
            if on_reasoning:
                await on_reasoning(reasoning)

        for tc in getattr(delta, "tool_calls", None) or []:
            idx = tc.index
            entry = tool_calls.setdefault(idx, {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if tc.id:
                entry["id"] = tc.id
            if tc.type:
                entry["type"] = tc.type
            if tc.function:
                if tc.function.name:
                    entry["function"]["name"] += tc.function.name
                if tc.function.arguments:
                    entry["function"]["arguments"] += tc.function.arguments

    cache_usage = extract_cache_usage(final_usage, provider)
    usage = {
        "input_tokens": int(getattr(final_usage, "prompt_tokens", 0) or 0)
        if final_usage is not None
        else cache_usage.total_input,
        "output_tokens": int(getattr(final_usage, "completion_tokens", 0) or 0)
        if final_usage is not None
        else 0,
        "cache_hit_tokens": cache_usage.hit_tokens,
        "cache_miss_tokens": cache_usage.miss_tokens,
        "cache_creation_tokens": cache_usage.creation_tokens,
    }

    result: dict = {
        "role": "assistant",
        "content": "".join(content_parts),
        "finish_reason": finish_reason,
        "usage": usage,
    }
    if reasoning_parts:
        result["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        result["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return result


# ── Non-streaming parser ──────────────────────────────────────────────


def _parse_response(response, provider: str) -> dict:
    choice = response.choices[0]
    msg = choice.message

    cache_usage = extract_cache_usage(getattr(response, "usage", None), provider)
    usage = {
        "input_tokens": int(getattr(response.usage, "prompt_tokens", 0) or 0)
        if getattr(response, "usage", None) is not None
        else cache_usage.total_input,
        "output_tokens": int(getattr(response.usage, "completion_tokens", 0) or 0)
        if getattr(response, "usage", None) is not None
        else 0,
        "cache_hit_tokens": cache_usage.hit_tokens,
        "cache_miss_tokens": cache_usage.miss_tokens,
        "cache_creation_tokens": cache_usage.creation_tokens,
    }

    result: dict = {
        "role": "assistant",
        "content": msg.content,
        "finish_reason": choice.finish_reason,
        "usage": usage,
    }

    if hasattr(msg, "reasoning_content") and msg.reasoning_content:
        result["reasoning_content"] = msg.reasoning_content

    if msg.tool_calls:
        result["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]

    return result
