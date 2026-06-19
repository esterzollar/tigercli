from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .state import FileLineEnding, FileState

FileReadMetadata = Dict[str, Any]


def normalize_content(value: str) -> str:
    return value.replace("\r\n", "\n")


def detect_line_endings(value: str) -> FileLineEnding:
    return "CRLF" if "\r\n" in value else "LF"


def detect_encoding(buffer: bytes) -> str:
    try:
        import chardet

        result = chardet.detect(buffer)
        encoding = result.get("encoding") or "utf-8"
        return encoding.lower()
    except ImportError:
        pass

    if len(buffer) >= 2 and buffer[0] == 0xFF and buffer[1] == 0xFE:
        return "utf-16-le"
    return "utf-8"


def read_text_file_with_metadata(file_path: str) -> FileReadMetadata:
    p = Path(file_path)
    buffer = p.read_bytes()
    stat_result = p.stat()
    encoding = detect_encoding(buffer)
    raw = buffer.decode(encoding)
    return {
        "content": normalize_content(raw),
        "encoding": encoding,
        "lineEndings": detect_line_endings(raw),
        "timestamp": int(stat_result.st_mtime * 1000),
    }


def write_text_file(
    file_path: str,
    content: str,
    encoding: str,
    line_endings: FileLineEnding,
) -> int:
    normalized = normalize_content(content)
    to_write = normalized.replace("\n", "\r\n") if line_endings == "CRLF" else normalized
    Path(file_path).write_text(to_write, encoding=encoding)
    enc = "utf-16-le" if encoding in ("utf-16", "utf-16le", "utf-16-le") else "utf-8"
    return len(to_write.encode(enc))


def ensure_parent_directory(file_path: str) -> None:
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)


def has_file_changed_since_state(file_path: str, state: FileState) -> bool:
    current = read_text_file_with_metadata(file_path)
    if current["timestamp"] <= (state.get("timestamp") or 0):
        return False
    is_full_read = (
        not state.get("isPartialView")
        and state.get("offset") is None
        and state.get("limit") is None
    )
    return not (is_full_read and current["content"] == state.get("content"))


def build_diff_preview(
    file_path: str,
    original_content: Optional[str],
    updated_content: str,
    max_lines: int = 40,
) -> Optional[str]:
    original = normalize_content(original_content) if original_content is not None else None
    updated = normalize_content(updated_content)

    if original is not None and original == updated:
        return None

    old_lines = _to_diff_lines(original)
    new_lines = _to_diff_lines(updated)

    prefix = 0
    while (
        prefix < len(old_lines)
        and prefix < len(new_lines)
        and old_lines[prefix] == new_lines[prefix]
    ):
        prefix += 1

    suffix = 0
    while (
        suffix < len(old_lines) - prefix
        and suffix < len(new_lines) - prefix
        and old_lines[len(old_lines) - 1 - suffix]
        == new_lines[len(new_lines) - 1 - suffix]
    ):
        suffix += 1

    old_changed = old_lines[prefix : len(old_lines) - suffix]
    new_changed = new_lines[prefix : len(new_lines) - suffix]

    old_start = 0 if original is None else prefix + 1
    new_start = prefix + 1

    preview_lines: List[str] = [
        f"--- {'/dev/null' if original is None else f'a/{file_path}'}",
        f"+++ b/{file_path}",
        f"@@ -{old_start},{len(old_changed)} +{new_start},{len(new_changed)} @@",
    ]

    if prefix > 0:
        preview_lines.append(f" {old_lines[prefix - 1]}")

    for line in old_changed:
        preview_lines.append(f"-{line}")
    for line in new_changed:
        preview_lines.append(f"+{line}")

    if suffix > 0:
        preview_lines.append(f" {old_lines[len(old_lines) - suffix]}")

    if len(preview_lines) > max_lines:
        return "\n".join(preview_lines[:max_lines]) + "\n..."

    return "\n".join(preview_lines)


def _to_diff_lines(content: Optional[str]) -> List[str]:
    if not content:
        return []
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines
