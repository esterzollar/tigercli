from __future__ import annotations

import os
import platform
import re
import subprocess
import tempfile
from pathlib import PurePosixPath, PureWindowsPath
from typing import Callable, Dict, List, Optional, Union

from tigercli.common.state import Literal

import math

DEFAULT_BASH_TIMEOUT_MS = 10 * 60 * 1000
MIN_BASH_TIMEOUT_MS = 60 * 1000
BASH_TIMEOUT_INCREMENT_MS = 5 * 60 * 1000
BASH_TIMEOUT_DECREMENT_MS = 60 * 1000


def clamp_bash_timeout_ms(
    timeout_ms: float,
    min_timeout_ms: float | None = None,
) -> int:
    if not math.isfinite(timeout_ms):
        return DEFAULT_BASH_TIMEOUT_MS
    if min_timeout_ms is None:
        min_timeout_ms = MIN_BASH_TIMEOUT_MS
    minimum = (
        max(1, round(min_timeout_ms))
        if math.isfinite(min_timeout_ms)
        else MIN_BASH_TIMEOUT_MS
    )
    return max(minimum, round(timeout_ms))


ShellKind = Literal["bash", "zsh", "unknown"]

_WINDOWS_GIT_LOCATIONS = [
    "C:\\Program Files\\Git\\cmd\\git.exe",
    "C:\\Program Files (x86)\\Git\\cmd\\git.exe",
]
_WINDOWS_BASH_LOCATIONS = [
    "C:\\Program Files\\Git\\bin\\bash.exe",
    "C:\\Program Files (x86)\\Git\\bin\\bash.exe",
]

_NUL_REDIRECT_REGEX = re.compile(r"(\d?&?>+\s*)[Nn][Uu][Ll](?=\s|$|[|&;)\n])")

_cached_git_bash_path: Optional[str] = None

_SYSTEM: str = platform.system().lower()


def _is_windows() -> bool:
    return _SYSTEM == "windows"


def set_shell_if_windows() -> None:
    if not _is_windows():
        return
    os.environ["SHELL"] = find_git_bash_path()


def find_git_bash_path() -> str:
    global _cached_git_bash_path
    if _cached_git_bash_path:
        return _cached_git_bash_path

    bash_path = resolve_windows_git_bash_path(
        find_executable_candidates=_find_all_windows_executable_candidates,
        find_git_exec_path=_find_git_exec_path,
        exists_fn=os.path.exists,
    )
    if bash_path:
        _cached_git_bash_path = bash_path
        return bash_path

    raise RuntimeError(
        "TigerLiteCode on Windows requires Git Bash. "
        "Install Git for Windows, or ensure Git's bash.exe is available in PATH."
    )


def resolve_windows_git_bash_path(
    find_executable_candidates: Callable[[str], List[str]],
    find_git_exec_path: Callable[[], Optional[str]],
    exists_fn: Callable[[str], bool],
) -> Optional[str]:
    return _first_existing_windows_path(
        [
            *find_executable_candidates("bash"),
            *_WINDOWS_BASH_LOCATIONS,
            *_git_exec_path_to_bash_candidates(find_git_exec_path()),
            *[
                b
                for g in find_executable_candidates("git")
                for b in _git_executable_to_bash_candidates(g)
            ],
        ],
        exists_fn,
    )


def resolve_shell_path() -> str:
    if _is_windows():
        return find_git_bash_path()
    env_shell = os.environ.get("SHELL")
    if env_shell and get_shell_kind(env_shell) != "unknown":
        return env_shell
    return "/bin/bash"


def get_shell_kind(shell_path: str) -> ShellKind:
    executable = (
        shell_path.replace("\\", "/").split("/")[-1].lower()
        if shell_path
        else ""
    )
    if executable in ("bash", "bash.exe"):
        return "bash"
    if executable in ("zsh", "zsh.exe"):
        return "zsh"
    return "unknown"


def build_shell_init_command(shell_path: str) -> Optional[str]:
    kind = get_shell_kind(shell_path)
    if kind == "zsh":
        return (
            'ZSHRC="${ZDOTDIR:-$HOME}/.zshrc"; '
            'if [ -f "$ZSHRC" ]; then { . "$ZSHRC"; } >/dev/null 2>&1; fi'
        )
    if kind == "bash":
        return (
            'BASHRC="${BASH_ENV:-$HOME}/.bashrc"; '
            'if [ -f "$BASHRC" ]; then { . "$BASHRC"; } >/dev/null 2>&1; fi'
        )
    return None


def build_disable_extglob_command(shell_path: str) -> Optional[str]:
    kind = get_shell_kind(shell_path)
    if kind == "bash":
        return "shopt -u extglob 2>/dev/null || true"
    if kind == "zsh":
        return "setopt NO_EXTENDED_GLOB 2>/dev/null || true"
    return None


def rewrite_windows_null_redirect(command: str) -> str:
    return _NUL_REDIRECT_REGEX.sub(r"\1/dev/null", command)


def windows_path_to_posix_path(windows_path: str) -> str:
    if windows_path.startswith("\\\\"):
        return windows_path.replace("\\", "/")
    drive_match = re.match(r"^([A-Za-z]):[/\\]", windows_path)
    if drive_match:
        drive_letter = drive_match.group(1).lower()
        return f"/{drive_letter}{windows_path[2:].replace('\\', '/')}"
    return windows_path.replace("\\", "/")


def posix_path_to_windows_path(posix_path: str) -> str:
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


def to_native_cwd(shell_cwd: str) -> str:
    if not _is_windows():
        return shell_cwd
    return posix_path_to_windows_path(shell_cwd)


def build_shell_env(
    shell_path: str,
    extra_env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    env: Dict[str, str] = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    env["SHELL"] = shell_path
    env["GIT_EDITOR"] = "true"

    if _is_windows():
        tmpdir = windows_path_to_posix_path(tempfile.gettempdir())
        env["TMPDIR"] = tmpdir
        env["TMPPREFIX"] = str(PurePosixPath(tmpdir) / "zsh")

    return env


def _find_all_windows_executable_candidates(executable: str) -> List[str]:
    if executable == "git":
        extra_candidates = _WINDOWS_GIT_LOCATIONS
    elif executable == "bash":
        extra_candidates = _WINDOWS_BASH_LOCATIONS
    else:
        extra_candidates = []

    try:
        output = subprocess.check_output(
            ["where.exe", executable],
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        where_results = [
            line.strip() for line in output.splitlines() if line.strip()
        ]
        if executable == "bash":
            where_results = [
                c
                for c in where_results
                if not re.search(r"system32[\\/]bash\.exe", c, re.IGNORECASE)
            ]
        return _filter_windows_executable_candidates(
            [*where_results, *extra_candidates]
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return _filter_windows_executable_candidates(extra_candidates)


def _find_git_exec_path() -> Optional[str]:
    try:
        output = subprocess.check_output(
            ["git", "--exec-path"],
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
        ).strip()
        return output or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _git_exec_path_to_bash_candidates(
    exec_path: Optional[str],
) -> List[str]:
    if not exec_path:
        return []
    normalized = exec_path.replace("/", "\\")
    p = PureWindowsPath(normalized)
    return [
        str(p.parent.parent.parent / "bin" / "bash.exe"),
        str(p.parent.parent / "bin" / "bash.exe"),
    ]


def _git_executable_to_bash_candidates(git_path: str) -> List[str]:
    p = PureWindowsPath(git_path)
    return [
        str(p.parent.parent / "bin" / "bash.exe"),
        str(p.parent / "bin" / "bash.exe"),
    ]


def _first_existing_windows_path(
    candidates: List[str],
    exists_fn: Callable[[str], bool],
) -> Optional[str]:
    seen = set()
    for candidate in candidates:
        normalized = os.path.normpath(candidate)
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        if get_shell_kind(normalized) == "bash" and exists_fn(normalized):
            return normalized
    return None


def _filter_windows_executable_candidates(candidates: List[str]) -> List[str]:
    cwd = os.getcwd().lower()
    seen = set()
    results: List[str] = []
    for candidate in candidates:
        normalized = os.path.normpath(os.path.abspath(candidate)).lower()
        candidate_dir = os.path.dirname(normalized).lower()
        if candidate_dir == cwd or normalized.startswith(f"{cwd}{os.sep}"):
            continue
        if normalized not in seen and os.path.exists(candidate):
            seen.add(normalized)
            results.append(candidate)
    return results
