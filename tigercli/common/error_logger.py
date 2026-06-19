from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ERROR_LOG_DIR = Path.home() / ".tigercli" / "errors"
ERROR_LOG_PATH = ERROR_LOG_DIR / "error.log"
MAX_ENTRIES = 20
_TRUNCATE_PREVIEW = 100


def _ensure_log_dir() -> None:
    ERROR_LOG_DIR.mkdir(parents=True, exist_ok=True)


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


def _truncate_content(value: str) -> str:
    if len(value) <= _TRUNCATE_PREVIEW:
        return value
    return f"{value[:_TRUNCATE_PREVIEW]}...(total {len(value)} chars)"


def _sanitize_request_payload(request: dict[str, Any]) -> dict[str, Any]:
    def walk(value: Any) -> Any:
        if not value or not isinstance(value, (dict, list)):
            return value
        if isinstance(value, list):
            return [walk(item) for item in value]
        result: dict[str, Any] = {}
        for key, val in value.items():
            if key == "content" and isinstance(val, str):
                result[key] = _truncate_content(val)
            else:
                result[key] = walk(val)
        return result

    return walk(request)


def log_api_error(entry: dict[str, Any]) -> None:
    try:
        _ensure_log_dir()

        error_info = entry.get("error", {})
        sanitized_error = {
            "name": error_info.get("name", "Error"),
            "message": _mask_sensitive(str(error_info.get("message", ""))),
        }
        stack = error_info.get("stack")
        if stack:
            sanitized_error["stack"] = _mask_sensitive(str(stack))

        log_line: dict[str, Any] = {
            "timestamp": entry.get("timestamp", ""),
            "location": entry.get("location", ""),
            "requestId": entry.get("requestId", ""),
            "sessionId": entry.get("sessionId"),
            "model": entry.get("model"),
            "baseURL": entry.get("baseURL"),
            "error": sanitized_error,
            "request": _sanitize_request_payload(entry.get("request", {})),
        }

        response_val = entry.get("response")
        if response_val is not None:
            log_line["response"] = (
                _mask_sensitive(response_val) if isinstance(response_val, str) else response_val
            )

        new_line = json.dumps(log_line, ensure_ascii=False) + "\n"
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(new_line)

        raw = ERROR_LOG_PATH.read_text(encoding="utf-8")
        lines = [line for line in raw.split("\n") if line.strip()]
        if len(lines) > MAX_ENTRIES:
            ERROR_LOG_PATH.write_text(
                "\n".join(lines[-MAX_ENTRIES:]) + "\n",
                encoding="utf-8",
            )
    except Exception:
        pass
