from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

DEBUG_LOG_DIR = Path.home() / ".tigercli" / "debug"
DEBUG_LOG_FILE = DEBUG_LOG_DIR / "debug.log"


def get_debug_log_path() -> str:
    return str(DEBUG_LOG_FILE)


def normalize_debug_error(error: Any) -> dict[str, str]:
    if isinstance(error, Exception):
        return {
            "name": type(error).__name__,
            "message": str(error),
            "stack": getattr(error, "__traceback__", None) and str(error.__traceback__) or "",
        }
    return {
        "name": "UnknownError",
        "message": str(error),
    }


def _to_serializable(value: Any, _seen: set[int] | None = None) -> Any:
    if _seen is None:
        _seen = set()

    if isinstance(value, (int, float)):
        import math
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return str(value)
        return value

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    if isinstance(value, Exception):
        return normalize_debug_error(value)

    if value is None:
        return None

    if id(value) in _seen:
        return "[Circular]"

    _seen.add(id(value))

    if isinstance(value, list):
        return [_to_serializable(item, _seen) for item in value]

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, val in value.items():
            result[str(key)] = _to_serializable(val, _seen)
        return result

    if hasattr(value, "__dict__"):
        return _to_serializable(vars(value), _seen)

    try:
        return str(value)
    except Exception:
        return repr(value)


def log_openai_chat_completion_debug(entry: dict[str, Any]) -> None:
    try:
        DEBUG_LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(_to_serializable(entry), ensure_ascii=False, default=str)
        with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def sanitize_request_body(body: dict[str, Any] | str) -> dict[str, Any] | str:
    if isinstance(body, str):
        return _mask_sensitive(body)
    return _sanitize_request_payload(body)


_TRUNCATE_PREVIEW = 100


def _truncate_content(value: str) -> str:
    if len(value) <= _TRUNCATE_PREVIEW:
        return value
    return f"{value[:_TRUNCATE_PREVIEW]}...(total {len(value)} chars)"


def _sanitize_request_payload(request: dict[str, Any]) -> dict[str, Any]:
    def walk(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        result: dict[str, Any] = {}
        for key, val in value.items():
            if key == "content" and isinstance(val, str):
                result[key] = _truncate_content(val)
            else:
                result[key] = walk(val) if isinstance(val, dict) else val
        return result

    return walk(request)


def _mask_sensitive(text: str) -> str:
    text = re.sub(
        r"(Authorization:\s*Bearer\s+)[^\s\r\n]+",
        r"\1***MASKED***",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r'((?:api[Kk]ey|api_key|secret)\s*[:=]\s*"?)[^",}\s]+',
        r"\1***MASKED***",
        text,
        flags=re.IGNORECASE,
    )
    return text
