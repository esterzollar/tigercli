from __future__ import annotations

import json
import os
import platform
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict

FileLineEnding = Literal["LF", "CRLF"]


class FileState(TypedDict, total=False):
    filePath: str
    content: str
    timestamp: float
    version: int
    offset: int
    limit: int
    isPartialView: bool
    encoding: str
    lineEndings: FileLineEnding


class FileSnippet(TypedDict):
    id: str
    filePath: str
    startLine: int
    endLine: int
    preview: str
    fileVersion: int
    scopeType: Literal["snippet", "full"]


class SessionStateHistoryMessage(TypedDict, total=False):
    role: Any
    content: Any


file_states_by_session: Dict[str, Dict[str, FileState]] = {}
snippets_by_session: Dict[str, Dict[str, FileSnippet]] = {}
snippet_counters_by_session: Dict[str, int] = {}
full_file_snippet_counters_by_session: Dict[str, int] = {}
file_versions_by_session: Dict[str, Dict[str, int]] = {}

_SYSTEM_PLATFORM: str = platform.system().lower()


def _is_windows() -> bool:
    return _SYSTEM_PLATFORM == "windows"


def _posix_path_to_windows_path(posix_path: str) -> str:
    if posix_path.startswith("//"):
        return posix_path.replace("/", "\\")
    cygdrive_match = re.match(r"^/cygdrive/([A-Za-z])(/|$)", posix_path)
    if cygdrive_match:
        drive = cygdrive_match.group(1).upper()
        rest = posix_path[len(f"/cygdrive/{cygdrive_match.group(1)}"):]
        return f"{drive}:{(rest or '\\').replace('/', '\\')}"
    drive_match = re.match(r"^/([A-Za-z])(/|$)", posix_path)
    if drive_match:
        drive = drive_match.group(1).upper()
        rest = posix_path[2:]
        return f"{drive}:{(rest or '\\').replace('/', '\\')}"
    return posix_path.replace("/", "\\")


def _is_git_bash_absolute_path(file_path: str) -> bool:
    return bool(re.match(r"^/[A-Za-z](/|$)", file_path) or re.match(r"^/cygdrive/[A-Za-z](/|$)", file_path))


def _normalize_native_file_path(file_path: str) -> str:
    if not _is_windows():
        return file_path
    if _is_git_bash_absolute_path(file_path):
        return _posix_path_to_windows_path(file_path)
    return file_path


def normalize_file_path(file_path: str) -> str:
    native = _normalize_native_file_path(file_path)
    if _is_windows():
        return os.path.normpath(native).replace("/", "\\")
    return os.path.normpath(native)


def is_absolute_file_path(file_path: str) -> bool:
    native = _normalize_native_file_path(file_path)
    if not _is_windows():
        return os.path.isabs(native)
    normalized = os.path.normpath(native)
    if not os.path.isabs(normalized):
        return False
    return bool(re.match(r"^[A-Za-z]:[\\/]", normalized) or normalized.startswith("\\\\"))


# ---------------------------------------------------------------------------
# File read metadata helpers (inlined from file-utils)
# ---------------------------------------------------------------------------


def _detect_line_endings(value: str) -> FileLineEnding:
    return "CRLF" if "\r\n" in value else "LF"


def _detect_encoding(buffer: bytes) -> str:
    if len(buffer) >= 2 and buffer[0] == 0xFF and buffer[1] == 0xFE:
        return "utf16le"
    return "utf8"


def _normalize_content(value: str) -> str:
    return value.replace("\r\n", "\n")


def read_text_file_with_metadata(file_path: str) -> Dict[str, Any]:
    p = Path(file_path)
    buffer = p.read_bytes()
    stat_result = p.stat()
    encoding = _detect_encoding(buffer)
    raw = buffer.decode(encoding)
    return {
        "content": _normalize_content(raw),
        "encoding": encoding,
        "lineEndings": _detect_line_endings(raw),
        "timestamp": int(stat_result.st_mtime * 1000),
    }


# ---------------------------------------------------------------------------
# Session state management
# ---------------------------------------------------------------------------


def clear_session_state(session_id: str) -> None:
    if not session_id:
        return
    file_states_by_session.pop(session_id, None)
    snippets_by_session.pop(session_id, None)
    snippet_counters_by_session.pop(session_id, None)
    full_file_snippet_counters_by_session.pop(session_id, None)
    file_versions_by_session.pop(session_id, None)


def has_session_state(session_id: str) -> bool:
    if not session_id:
        return False
    return bool(
        len(file_states_by_session.get(session_id) or {}) > 0
        or len(snippets_by_session.get(session_id) or {}) > 0
        or session_id in snippet_counters_by_session
        or session_id in full_file_snippet_counters_by_session
        or len(file_versions_by_session.get(session_id) or {}) > 0
    )


def _get_file_version(session_id: str, file_path: str) -> int:
    if not session_id or not file_path:
        return 0
    return (file_versions_by_session.get(session_id) or {}).get(normalize_file_path(file_path), 0)


def _set_file_version(session_id: str, file_path: str, version: int) -> None:
    normalized = normalize_file_path(file_path)
    if session_id not in file_versions_by_session:
        file_versions_by_session[session_id] = {}
    file_versions_by_session[session_id][normalized] = version


def record_file_state(
    session_id: str,
    state: FileState,
    options: Optional[Dict[str, bool]] = None,
) -> None:
    if options is None:
        options = {}
    if not session_id or not state.get("filePath"):
        return

    if session_id not in file_states_by_session:
        file_states_by_session[session_id] = {}

    normalized_path = normalize_file_path(state["filePath"])
    current_version = _get_file_version(session_id, normalized_path)
    next_version = current_version + 1 if options.get("incrementVersion") else current_version
    _set_file_version(session_id, normalized_path, next_version)

    entry: FileState = dict(state)
    entry["filePath"] = normalized_path
    entry["version"] = next_version
    file_states_by_session[session_id][normalized_path] = entry


def mark_file_read(
    session_id: str,
    file_path: str,
    state: Optional[Dict[str, Any]] = None,
) -> None:
    if not session_id or not file_path:
        return
    s = state or {}
    record_file_state(session_id, {
        "filePath": file_path,
        "content": s.get("content") or "",
        "timestamp": s.get("timestamp") or 0,
        "offset": s.get("offset"),
        "limit": s.get("limit"),
        "isPartialView": s.get("isPartialView"),
        "encoding": s.get("encoding"),
        "lineEndings": s.get("lineEndings"),
    })


def get_file_state(session_id: str, file_path: str) -> Optional[FileState]:
    if not session_id or not file_path:
        return None
    return (file_states_by_session.get(session_id) or {}).get(normalize_file_path(file_path))


def was_file_read(session_id: str, file_path: str) -> bool:
    return get_file_state(session_id, file_path) is not None


def get_file_version(session_id: str, file_path: str) -> int:
    return _get_file_version(session_id, file_path)


def is_full_file_view(state: Optional[FileState]) -> bool:
    return bool(
        state
        and not state.get("isPartialView")
        and state.get("offset") is None
        and state.get("limit") is None
    )


def _create_snippet_with_id(
    session_id: str,
    file_path: str,
    start_line: int,
    end_line: int,
    preview: str,
    id: str,
    scope_type: Literal["snippet", "full"],
) -> Optional[FileSnippet]:
    if not session_id or not file_path or start_line < 1 or end_line < start_line:
        return None

    snippet: FileSnippet = {
        "id": id,
        "filePath": normalize_file_path(file_path),
        "startLine": start_line,
        "endLine": end_line,
        "preview": preview,
        "fileVersion": get_file_version(session_id, file_path),
        "scopeType": scope_type,
    }

    if session_id not in snippets_by_session:
        snippets_by_session[session_id] = {}
    snippets_by_session[session_id][snippet["id"]] = snippet
    return snippet


def create_snippet(
    session_id: str,
    file_path: str,
    start_line: int,
    end_line: int,
    preview: str,
) -> Optional[FileSnippet]:
    next_counter = (snippet_counters_by_session.get(session_id) or 0) + 1
    snippet_counters_by_session[session_id] = next_counter
    return _create_snippet_with_id(session_id, file_path, start_line, end_line, preview, f"snippet_{next_counter}", "snippet")


def create_full_file_snippet(
    session_id: str,
    file_path: str,
    start_line: int,
    end_line: int,
    preview: str,
) -> Optional[FileSnippet]:
    next_counter = full_file_snippet_counters_by_session.get(session_id) or 0
    full_file_snippet_counters_by_session[session_id] = next_counter + 1
    return _create_snippet_with_id(session_id, file_path, start_line, end_line, preview, f"full_file_{next_counter}", "full")


def _infer_snippet_scope_type(id: str) -> Literal["snippet", "full"]:
    return "full" if id.startswith("full_file_") else "snippet"


def _update_snippet_counters(session_id: str, id: str) -> None:
    full_file_match = re.match(r"^full_file_(\d+)$", id)
    if full_file_match:
        next_counter = int(full_file_match.group(1)) + 1
        current = full_file_snippet_counters_by_session.get(session_id) or 0
        full_file_snippet_counters_by_session[session_id] = max(current, next_counter)
        return
    snippet_match = re.match(r"^snippet_(\d+)$", id)
    if snippet_match:
        current_counter = int(snippet_match.group(1))
        current = snippet_counters_by_session.get(session_id) or 0
        snippet_counters_by_session[session_id] = max(current, current_counter)


def restore_snippet(
    session_id: str,
    snippet: Dict[str, Any],
) -> Optional[FileSnippet]:
    restored = _create_snippet_with_id(
        session_id,
        snippet.get("filePath") or "",
        snippet.get("startLine") or 0,
        snippet.get("endLine") or 0,
        snippet.get("preview") or "",
        snippet.get("id") or "",
        snippet.get("scopeType") or _infer_snippet_scope_type(snippet.get("id") or ""),
    )
    if restored:
        _update_snippet_counters(session_id, snippet.get("id") or "")
    return restored


def get_snippet(session_id: str, snippet_id: str) -> Optional[FileSnippet]:
    if not session_id or not snippet_id:
        return None
    return (snippets_by_session.get(session_id) or {}).get(snippet_id)


def has_snippet_outdated_file_version(session_id: str, snippet: FileSnippet) -> bool:
    return get_file_version(session_id, snippet["filePath"]) > snippet["fileVersion"]


def _as_record(value: Any) -> Optional[Dict[str, Any]]:
    if value is None or not isinstance(value, dict):
        return None
    return value


def _to_positive_integer(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        number_value = value
    elif isinstance(value, str):
        try:
            number_value = int(value)
        except (ValueError, TypeError):
            return None
    else:
        return None
    if number_value < 1:
        return None
    return number_value


def _parse_persisted_tool_result(content: str) -> Optional[Dict[str, Any]]:
    try:
        result = json.loads(content)
        return _as_record(result)
    except (json.JSONDecodeError, TypeError):
        return None


def _restore_snippet_from_record(
    session_id: str,
    record: Dict[str, Any],
    options: Dict[str, Any],
) -> Optional[FileSnippet]:
    raw_id = record.get(options["idKey"])
    raw_file_path = record.get(options["filePathKey"])
    id = raw_id.strip() if isinstance(raw_id, str) else ""
    file_path = normalize_file_path(raw_file_path) if isinstance(raw_file_path, str) else ""
    start_line = _to_positive_integer(record.get(options["startLineKey"]))
    end_line = _to_positive_integer(record.get(options["endLineKey"]))
    if not id or not file_path or start_line is None or end_line is None:
        return None

    return restore_snippet(session_id, {
        "id": id,
        "filePath": file_path,
        "startLine": start_line,
        "endLine": end_line,
        "preview": options.get("preview"),
        "scopeType": options.get("scopeType"),
    })


def _refresh_rebuilt_file_state(
    session_id: str,
    raw_file_path: str,
    options: Optional[Dict[str, Any]] = None,
) -> None:
    if options is None:
        options = {}
    file_path = normalize_file_path(raw_file_path)
    if not file_path:
        return

    p = Path(file_path)
    if not p.exists():
        return

    try:
        if p.is_dir():
            return

        metadata = read_text_file_with_metadata(file_path)
        is_partial_view = options.get("scopeType") == "snippet"
        if is_partial_view:
            lines = metadata["content"].split("\n")
            start = (options.get("startLine") or 1) - 1
            end = options.get("endLine") or len(lines)
            content = "\n".join(lines[start:end])
        else:
            content = metadata["content"]

        record_file_state(
            session_id,
            {
                "filePath": file_path,
                "content": content,
                "timestamp": metadata["timestamp"],
                "offset": options.get("startLine") if is_partial_view else None,
                "limit": max(1, options["endLine"] - options.get("startLine", 1) + 1)
                if is_partial_view and options.get("startLine") is not None and options.get("endLine") is not None
                else None,
                "isPartialView": is_partial_view,
                "encoding": metadata.get("encoding"),
                "lineEndings": metadata.get("lineEndings"),
            },
            {"incrementVersion": options.get("incrementVersion", False)},
        )
    except Exception:
        pass


def _rebuild_read_result(
    session_id: str,
    result: Dict[str, Any],
    metadata: Dict[str, Any],
) -> None:
    snippet = _as_record(metadata.get("snippet"))
    if not snippet:
        return

    restored = _restore_snippet_from_record(
        session_id,
        snippet,
        {
            "idKey": "id",
            "filePathKey": "filePath",
            "startLineKey": "startLine",
            "endLineKey": "endLine",
            "preview": result.get("output") if isinstance(result.get("output"), str) else "",
        },
    )
    if not restored:
        return

    _refresh_rebuilt_file_state(session_id, restored["filePath"], {
        "scopeType": restored["scopeType"],
        "startLine": restored["startLine"],
        "endLine": restored["endLine"],
        "incrementVersion": False,
    })


def _rebuild_candidate_snippets(
    session_id: str,
    metadata: Dict[str, Any],
    file_path: Optional[str],
) -> None:
    if not file_path:
        return

    candidates = metadata.get("candidates") if isinstance(metadata.get("candidates"), list) else []
    for candidate in candidates:
        record = _as_record(candidate)
        if not record:
            continue
        merged = dict(record)
        merged["file_path"] = file_path
        _restore_snippet_from_record(
            session_id,
            merged,
            {
                "idKey": "snippet_id",
                "filePathKey": "file_path",
                "startLineKey": "start_line",
                "endLineKey": "end_line",
                "scopeType": "snippet",
                "preview": record.get("preview") if isinstance(record.get("preview"), str) else "",
            },
        )

    closest_match = _as_record(metadata.get("closest_match"))
    if closest_match:
        merged = dict(closest_match)
        merged["file_path"] = file_path
        _restore_snippet_from_record(
            session_id,
            merged,
            {
                "idKey": "snippet_id",
                "filePathKey": "file_path",
                "startLineKey": "start_line",
                "endLineKey": "end_line",
                "scopeType": "snippet",
                "preview": closest_match.get("preview") if isinstance(closest_match.get("preview"), str) else "",
            },
        )


def _rebuild_edit_result(session_id: str, metadata: Dict[str, Any]) -> None:
    scope = _as_record(metadata.get("scope"))
    if scope:
        _restore_snippet_from_record(
            session_id,
            scope,
            {
                "idKey": "snippet_id",
                "filePathKey": "file_path",
                "startLineKey": "start_line",
                "endLineKey": "end_line",
                "scopeType": "full" if metadata.get("read_scope_type") == "full" else None,
            },
        )

    scope_file_path = scope.get("file_path") if isinstance(scope.get("file_path"), str) else None
    _rebuild_candidate_snippets(session_id, metadata, scope_file_path)

    file_path = metadata.get("file_path") if isinstance(metadata.get("file_path"), str) else scope_file_path
    if file_path and metadata.get("cache_refreshed") is True:
        _refresh_rebuilt_file_state(session_id, file_path, {"incrementVersion": True})


def _rebuild_write_result(session_id: str, metadata: Dict[str, Any]) -> None:
    if metadata.get("cache_refreshed") is not True or not isinstance(metadata.get("file_path"), str):
        return
    _refresh_rebuilt_file_state(session_id, metadata["file_path"], {"incrementVersion": True})


def rebuild_session_state_from_history(
    session_id: str,
    messages: List[SessionStateHistoryMessage],
) -> None:
    if not session_id or has_session_state(session_id):
        return

    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role != "tool" or not isinstance(content, str):
            continue

        result = _parse_persisted_tool_result(content)
        if not result or result.get("ok") is not True:
            continue

        metadata = _as_record(result.get("metadata"))
        if not metadata:
            continue

        name = result.get("name")
        if name == "read":
            _rebuild_read_result(session_id, result, metadata)
        elif name == "edit":
            _rebuild_edit_result(session_id, metadata)
        elif name == "write":
            _rebuild_write_result(session_id, metadata)
