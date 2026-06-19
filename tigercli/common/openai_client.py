from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from openai import AsyncOpenAI

from tigercli.common.settings import resolve_current_settings


_client_cache: AsyncOpenAI | None = None
_client_cache_key: str = ""


def _get_machine_id() -> Optional[str]:
    try:
        id_path = Path.home() / ".tigercli" / "machine-id"
        if id_path.exists():
            raw = id_path.read_text(encoding="utf-8").strip()
            if raw:
                return raw
        generated = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}-{int(time.time() * 1000)}"
        id_path.parent.mkdir(parents=True, exist_ok=True)
        id_path.write_text(generated, encoding="utf-8")
        return generated
    except Exception:
        return None


def create_openai_client(project_root: str = "") -> dict[str, Any]:
    global _client_cache, _client_cache_key

    settings = resolve_current_settings(project_root or os.getcwd())
    api_key = settings.get("apiKey")

    if not api_key:
        return {
            "client": None,
            "model": settings["model"],
            "baseURL": settings["baseURL"],
            "temperature": settings.get("temperature"),
            "thinkingEnabled": settings["thinkingEnabled"],
            "reasoningEffort": settings["reasoningEffort"],
            "debugLogEnabled": settings["debugLogEnabled"],
            "telemetryEnabled": settings["telemetryEnabled"],
            "notify": settings.get("notify"),
            "webSearchTool": settings.get("webSearchTool"),
            "env": settings["env"],
            "machineId": _get_machine_id(),
        }

    cache_key = f"{api_key}::{settings['baseURL']}"
    if _client_cache is not None and _client_cache_key == cache_key:
        return {
            "client": _client_cache,
            "model": settings["model"],
            "baseURL": settings["baseURL"],
            "temperature": settings.get("temperature"),
            "thinkingEnabled": settings["thinkingEnabled"],
            "reasoningEffort": settings["reasoningEffort"],
            "debugLogEnabled": settings["debugLogEnabled"],
            "telemetryEnabled": settings["telemetryEnabled"],
            "notify": settings.get("notify"),
            "webSearchTool": settings.get("webSearchTool"),
            "env": settings["env"],
            "machineId": _get_machine_id(),
        }

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=300.0, write=120.0, pool=180.0),
    )

    _client_cache = AsyncOpenAI(
        api_key=api_key,
        base_url=settings["baseURL"] or None,
        http_client=http_client,
    )
    _client_cache_key = cache_key

    async def _warmup() -> None:
        try:
            async with asyncio.timeout(3):
                await _client_cache.models.list()
        except Exception:
            pass

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_warmup())
    except RuntimeError:
        pass  # no running event loop, skip warmup

    return {
        "client": _client_cache,
        "model": settings["model"],
        "baseURL": settings["baseURL"],
        "temperature": settings.get("temperature"),
        "thinkingEnabled": settings["thinkingEnabled"],
        "reasoningEffort": settings["reasoningEffort"],
        "debugLogEnabled": settings["debugLogEnabled"],
        "telemetryEnabled": settings["telemetryEnabled"],
        "notify": settings.get("notify"),
        "webSearchTool": settings.get("webSearchTool"),
        "env": settings["env"],
        "machineId": _get_machine_id(),
    }
