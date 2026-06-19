from __future__ import annotations

import os
import platform
import subprocess
from typing import Callable, Optional

_SYSTEM: str = platform.system().lower()

KillPidFn = Callable[[int, int], None]
TaskkillSpawnSync = Callable[
    [str, list[str], dict], "subprocess.CompletedProcess[str]"
]


def kill_process_tree(
    pid: int,
    signal: int = 9,
    *,
    platform_name: Optional[str] = None,
    kill_pid: Optional[KillPidFn] = None,
    run_taskkill: Optional[Callable[[int], bool]] = None,
    kill_group_on_non_windows: bool = True,
) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False

    pname = platform_name if platform_name is not None else _SYSTEM
    kfn = kill_pid if kill_pid is not None else _kill_pid_impl

    if pname == "windows":
        taskkill_fn = run_taskkill if run_taskkill is not None else _run_windows_taskkill
        if taskkill_fn(pid):
            return True
        return _kill_direct_process(pid, signal, kfn)

    if kill_group_on_non_windows and _kill_direct_process(-pid, signal, kfn):
        return True
    return _kill_direct_process(pid, signal, kfn)


def _run_windows_taskkill(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _kill_direct_process(
    pid: int,
    signal: int,
    kill_pid: KillPidFn,
) -> bool:
    try:
        kill_pid(pid, signal)
        return True
    except (OSError, ValueError):
        return False


def _kill_pid_impl(pid: int, signal: int) -> None:
    os.kill(pid, signal)
