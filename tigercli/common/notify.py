from __future__ import annotations

import errno
import os
import subprocess
from typing import Callable


def format_duration_seconds(duration_ms: float) -> str:
    safe_ms = 0
    if isinstance(duration_ms, (int, float)):
        import math
        if not (math.isnan(duration_ms) or math.isinf(duration_ms)):
            safe_ms = max(0, duration_ms)
    return str(int(safe_ms // 1000))


def build_notify_env(
    duration_ms: float,
    base_env: dict[str, str] | None = None,
    context: dict[str, str | None] | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env["DURATION"] = format_duration_seconds(duration_ms)
    env.pop("STATUS", None)
    env.pop("FAIL_REASON", None)
    env.pop("BODY", None)
    env.pop("TITLE", None)

    if context:
        if context.get("status"):
            env["STATUS"] = context["status"]
        if context.get("failReason"):
            env["FAIL_REASON"] = context["failReason"]
        if context.get("body"):
            env["BODY"] = context["body"]
        if context.get("title"):
            env["TITLE"] = context["title"]
    return env


def launch_notify_script(
    notify_path: str | None,
    duration_ms: float,
    working_directory: str | None = None,
    spawn_process: Callable[..., subprocess.Popen[bytes]] | None = None,
    configured_env: dict[str, str] | None = None,
    context: dict[str, str | None] | None = None,
) -> None:
    command_path = notify_path.strip() if notify_path else None
    if not command_path:
        return

    if spawn_process is None:
        spawn_process = subprocess.Popen

    merged_env = dict(os.environ)
    if configured_env:
        merged_env.update(configured_env)
    env = build_notify_env(duration_ms, merged_env, context)

    popen_kwargs = {
        "cwd": working_directory,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    }

    try:
        child = spawn_process([command_path], **popen_kwargs)
    except FileNotFoundError:
        return
    except PermissionError:
        if os.name == "nt":
            return
        try:
            spawn_process(["/bin/sh", command_path], **popen_kwargs)
        except Exception:
            pass
    except OSError as e:
        if os.name == "nt":
            return
        if e.errno not in (errno.EACCES, errno.ENOEXEC):
            return
        try:
            spawn_process(["/bin/sh", command_path], **popen_kwargs)
        except Exception:
            pass
    except Exception:
        pass
