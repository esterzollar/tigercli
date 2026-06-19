import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell command in the project directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command to execute"},
                    "workdir": {"type": "string", "description": "Working directory (default: project root)"},
                    "timeout": {"type": "integer", "description": "Timeout in milliseconds"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file from the filesystem. Returns content with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "Absolute path to the file"},
                    "offset": {"type": "integer", "description": "Line number to start from (1-indexed)"},
                    "limit": {"type": "integer", "description": "Maximum lines to read"},
                },
                "required": ["filePath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write a file to disk. Creates or overwrites.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "Absolute path to write to"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["filePath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Perform exact string replacement in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "Absolute path to the file"},
                    "oldString": {"type": "string", "description": "Text to replace (must match exactly)"},
                    "newString": {"type": "string", "description": "Replacement text"},
                    "replaceAll": {"type": "boolean", "description": "Replace all occurrences (default: false)"},
                },
                "required": ["filePath", "oldString", "newString"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
                    "path": {"type": "string", "description": "Directory to search in (default: project root)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with a regular expression.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "include": {"type": "string", "description": "File pattern filter, e.g. '*.py'"},
                    "path": {"type": "string", "description": "Directory to search in (default: project root)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "websearch",
            "description": "Search the web for current information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "webfetch",
            "description": "Fetch a URL and return its content as text or markdown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "format": {"type": "string", "enum": ["text", "markdown"], "description": "Content format"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task",
            "description": "Launch a subagent for complex multi-step tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Short description of the task (3-5 words)"},
                    "prompt": {"type": "string", "description": "Detailed instructions for the subagent"},
                    "subagent_type": {"type": "string", "description": "Subagent name, e.g. 'explore', 'general', or a custom .agents/*.md agent"},
                    "provider": {"type": "string", "description": "Optional provider override for this subagent call"},
                    "model": {"type": "string", "description": "Optional model override for this subagent call"},
                },
                "required": ["description", "prompt", "subagent_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill",
            "description": "Load and run a reusable TigerLiteCode skill from .skills/<name>/SKILL.md or the user skills directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name"},
                    "arguments": {"type": "string", "description": "Arguments passed to the skill"},
                    "provider": {"type": "string", "description": "Optional provider override for this skill run"},
                    "model": {"type": "string", "description": "Optional model override for this skill run"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todowrite",
            "description": "Create a structured task list with status tracking for the current session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "Brief description of the task"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                            },
                            "required": ["content", "status", "priority"],
                        },
                    },
                },
                "required": ["todos"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": (
                "Update the current task plan. The plan argument must be the complete "
                "markdown task list to show as the latest progress state. Use [ ] for "
                "pending, [>] for in-progress, [x] for completed, [-] for cancelled. "
                "Keep exactly one [>] task. Update before starting a task and after "
                "completing each task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "string",
                        "description": "Complete markdown task list with status markers.",
                    },
                    "explanation": {
                        "type": "string",
                        "description": "Optional short reason for changing the plan.",
                    },
                },
                "required": ["plan"],
                "additionalProperties": False,
            },
        },
    },
]

TOOL_REGISTRY = {
    "bash": ("_exec_bash", True),
    "read": ("_exec_read", False),
    "write": ("_exec_write", True),
    "edit": ("_exec_edit", True),
    "glob": ("_exec_glob", False),
    "grep": ("_exec_grep", False),
    "websearch": ("_exec_websearch", False),
    "webfetch": ("_exec_webfetch", False),
    "task": ("_exec_task", False),
    "skill": ("_exec_skill", False),
    "todowrite": ("_exec_todowrite", False),
    "update_plan": ("_exec_update_plan", False),
}


async def execute_tool(name: str, arguments: dict, project_path: str) -> str:
    if name == "bash":
        return await _exec_bash(arguments, project_path)
    elif name == "read":
        return _exec_read(arguments, project_path)
    elif name == "write":
        return _exec_write(arguments, project_path)
    elif name == "edit":
        return _exec_edit(arguments, project_path)
    elif name == "glob":
        return _exec_glob(arguments, project_path)
    elif name == "grep":
        return _exec_grep(arguments, project_path)
    elif name == "websearch":
        return await _exec_websearch(arguments)
    elif name == "webfetch":
        return await _exec_webfetch(arguments)
    elif name == "task":
        return await _exec_task(arguments, project_path)
    elif name == "skill":
        return await _exec_skill(arguments, project_path)
    elif name == "todowrite":
        return _exec_todowrite(arguments)
    elif name == "update_plan":
        return _exec_update_plan(arguments)
    else:
        return f"Unknown tool: {name}"


def tool_needs_permission(name: str) -> bool:
    entry = TOOL_REGISTRY.get(name)
    return entry[1] if entry else False


def get_all_tool_schemas(extra_tools: list[dict] | None = None) -> list[dict]:
    if extra_tools:
        return TOOL_SCHEMAS + extra_tools
    return TOOL_SCHEMAS


def get_tool_schemas_by_names(names: set[str] | list[str] | tuple[str, ...]) -> list[dict]:
    wanted = set(names)
    return [
        schema for schema in TOOL_SCHEMAS
        if schema.get("function", {}).get("name") in wanted
    ]


# ── Tool executors ──────────────────────────────────────────────

async def _exec_bash(args: dict, project_path: str) -> str:
    command = args["command"]
    workdir = args.get("workdir", project_path)
    try:
        timeout_ms = int(args.get("timeout", 30000))
    except (TypeError, ValueError):
        timeout_ms = 30000
    timeout_ms = max(1000, min(timeout_ms, 120000))

    wd = Path(workdir)
    if not wd.is_absolute():
        wd = Path(project_path) / wd
    if not wd.exists():
        wd = Path(project_path)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(wd),
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_ms / 1000
            )
        except asyncio.TimeoutError:
            _kill_process_group(proc)
            return f"[timeout after {timeout_ms}ms]"
        except asyncio.CancelledError:
            _kill_process_group(proc)
            raise
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")

        result_parts = []
        if out.strip():
            result_parts.append(out.strip())
        if err.strip():
            result_parts.append(f"[stderr]\n{err.strip()}")
        result_parts.append(f"[exit code: {proc.returncode}]")

        return "\n".join(result_parts)
    except Exception as e:
        return f"[error: {e}]"


def _kill_process_group(proc) -> None:
    try:
        if proc.returncode is None and proc.pid:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _exec_read(args: dict, project_path: str) -> str:
    path = _resolve_path(args["filePath"], project_path)
    if not path.exists():
        return f"File not found: {path}"
    if path.is_dir():
        entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        lines = [f"  {e.name}/" if e.is_dir() else f"  {e.name}" for e in entries[:100]]
        return "Directory contents:\n" + "\n".join(lines)

    try:
        content = path.read_text()
    except Exception as e:
        return f"Error reading file: {e}"

    lines = content.split("\n")
    total = len(lines)
    offset = max(1, args.get("offset", 1))
    limit = args.get("limit", 2000)
    selected = lines[offset - 1:offset - 1 + limit]

    result = [f"File: {path} (lines {offset}-{min(offset+limit-1, total)} of {total})"]
    for i, line in enumerate(selected, start=offset):
        result.append(f"{i}: {line}")
    return "\n".join(result)


def _exec_write(args: dict, project_path: str) -> str:
    path = _resolve_path(args["filePath"], project_path)
    content = args["content"]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


def _exec_edit(args: dict, project_path: str) -> str:
    path = _resolve_path(args["filePath"], project_path)
    old_str = args["oldString"]
    new_str = args["newString"]
    replace_all = args.get("replaceAll", False)

    if not path.exists():
        return f"File not found: {path}"

    try:
        content = path.read_text()
    except Exception as e:
        return f"Error reading file: {e}"

    if old_str not in content:
        return f"ERROR: oldString not found in {path}. The text must match exactly including whitespace."

    count = content.count(old_str)
    if count > 1 and not replace_all:
        return (
            f"Found {count} matches for oldString in {path}. "
            f"Provide more surrounding context to make it unique, or set replaceAll=true."
        )

    new_content = content.replace(old_str, new_str) if replace_all else content.replace(old_str, new_str, 1)
    path.write_text(new_content)
    return f"Edited {path} ({count} occurrence{'s' if count > 1 else ''} replaced)."


def _exec_glob(args: dict, project_path: str) -> str:
    pattern = args["pattern"]
    base = Path(args.get("path", project_path))
    if not base.is_absolute():
        base = Path(project_path) / base

    try:
        matches = sorted(base.glob(pattern))[:100]
    except Exception as e:
        return f"Glob error: {e}"

    if not matches:
        return f"No files matching '{pattern}'"
    return "\n".join(str(m) for m in matches)


def _exec_grep(args: dict, project_path: str) -> str:
    pattern = args["pattern"]
    include = args.get("include")
    base = Path(args.get("path", project_path))
    if not base.is_absolute():
        base = Path(project_path) / base

    if shutil.which("rg"):
        return _grep_ripgrep(pattern, base, include)
    return _grep_python(pattern, base, include)


def _grep_ripgrep(pattern: str, base: Path, include: str | None) -> str:
    cmd = ["rg", "--line-number", "--no-heading", "--color=never", "-M", "200", pattern, str(base)]
    if include:
        cmd.insert(-1, f"--glob={include}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = result.stdout.strip()
        lines = out.split("\n")
        if len(lines) > 500:
            out = "\n".join(lines[:500]) + f"\n... ({len(lines) - 500} more matches)"
        return out or "No matches found."
    except subprocess.TimeoutExpired:
        return "[grep timeout]"
    except Exception as e:
        return f"[grep error: {e}]"


def _grep_python(pattern: str, base: Path, include: str | None) -> str:
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Invalid regex: {e}"

    results = []
    for fpath in base.rglob("*"):
        if not fpath.is_file():
            continue
        if include and not fpath.match(include):
            continue
        try:
            for i, line in enumerate(fpath.read_text().split("\n"), 1):
                if regex.search(line):
                    results.append(f"{fpath}:{i}: {line.strip()[:200]}")
                    if len(results) >= 500:
                        break
        except Exception:
            continue
        if len(results) >= 500:
            break

    if not results:
        return "No matches found."
    return "\n".join(results)


async def _exec_websearch(args: dict) -> str:
    """Run the standalone websearch script as a subprocess (OpenCode plugin style)."""
    query = args["query"]
    max_results = int(args.get("maxResults", 5))
    payload = json.dumps({"query": query, "maxResults": max_results})

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tigercli.tools.websearch",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(payload.encode()),
            timeout=30,
        )
        if proc.returncode != 0:
            err = stderr.decode().strip() if stderr else "unknown error"
            return f"WebSearch failed: {err}"
        return stdout.decode().strip()
    except asyncio.TimeoutError:
        return "WebSearch timed out after 30s."
    except FileNotFoundError:
        return "WebSearch: python not found."
    except Exception as e:
        return f"WebSearch error: {e}"


async def _exec_webfetch(args: dict) -> str:
    url = args["url"]
    fetch_format = args.get("format", "markdown")
    max_chars = int(args.get("maxChars", 10000))
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; TigerLiteCode/1.0)"
            })
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            text = resp.text

            if fetch_format == "markdown" and "text/html" in content_type:
                try:
                    from markdownify import markdownify as md
                    text = md(text, heading_style="ATX", strip=["script", "style", "meta", "link"])
                except ImportError:
                    import re as _re
                    import html
                    text = _re.sub(
                        r'<script[^>]*>.*?</script>', '', text,
                        flags=_re.DOTALL | _re.IGNORECASE,
                    )
                    text = _re.sub(
                        r'<style[^>]*>.*?</style>', '', text,
                        flags=_re.DOTALL | _re.IGNORECASE,
                    )
                    text = _re.sub(r'<[^>]+>', '', text)
                    text = html.unescape(text)
                    text = _re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
                    text = text.strip()

            if len(text) > max_chars:
                text = text[:max_chars] + (
                    f"\n\n... (truncated, {len(text) - max_chars} more chars)"
                )

            return text
    except ImportError:
        return "httpx not installed. Install with: pip install httpx"
    except Exception as e:
        return f"Fetch error: {e}"


async def _exec_task(args: dict, project_path: str) -> str:
    from tigercli.agent.subagents import run_subagent

    return await run_subagent(args, project_path)


async def _exec_skill(args: dict, project_path: str) -> str:
    from tigercli.agent.skills import run_skill

    return await run_skill(args, project_path)


def _exec_todowrite(args: dict) -> str:
    todos = args.get("todos", [])
    lines = ["Task list updated:"]
    for t in todos:
        status_icon = {"pending": "○", "in_progress": "●", "completed": "✓", "cancelled": "✗"}
        icon = status_icon.get(t.get("status", "pending"), "○")
        priority = t.get("priority", "medium")
        lines.append(f"  {icon} [{priority}] {t.get('content', '')}")
    return "\n".join(lines)


def _exec_update_plan(args: dict) -> str:
    """Store the plan for TUI display. The plan is a markdown string."""
    plan = args.get("plan", "")
    explanation = args.get("explanation", "")
    if explanation:
        return f"Plan updated: {explanation}\n\n{plan}"
    return f"Plan updated:\n\n{plan}"


def _resolve_path(file_path: str, project_path: str) -> Path:
    p = Path(file_path)
    if p.is_absolute():
        return p
    return Path(project_path) / p
