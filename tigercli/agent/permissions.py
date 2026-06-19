from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

PermissionScope = Literal[
    "read-in-cwd",
    "read-out-cwd",
    "write-in-cwd",
    "write-out-cwd",
    "delete-in-cwd",
    "delete-out-cwd",
    "query-git-log",
    "mutate-git-log",
    "network",
    "mcp",
]

BashPermissionScope = PermissionScope | Literal["unknown"]

PermissionDecision = Literal["allow", "deny", "ask"]

AskPermissionScope = PermissionScope | Literal["unknown"]

PermissionDefaultMode = Literal["allowAll", "askAll"]

_VALID_SCOPES: frozenset[AskPermissionScope] = frozenset({
    "read-in-cwd",
    "read-out-cwd",
    "write-in-cwd",
    "write-out-cwd",
    "delete-in-cwd",
    "delete-out-cwd",
    "query-git-log",
    "mutate-git-log",
    "network",
    "mcp",
    "unknown",
})

_PERMISSION_SCOPES: frozenset[PermissionScope] = frozenset({
    "read-in-cwd",
    "read-out-cwd",
    "write-in-cwd",
    "write-out-cwd",
    "delete-in-cwd",
    "delete-out-cwd",
    "query-git-log",
    "mutate-git-log",
    "network",
    "mcp",
})


@dataclass
class UserToolPermission:
    toolCallId: str
    permission: Literal["allow", "deny"]


@dataclass
class MessageToolPermission:
    toolCallId: str
    permission: PermissionDecision


@dataclass
class AskPermissionRequest:
    toolCallId: str
    scopes: list[AskPermissionScope]
    name: str
    command: str
    description: str | None = None


@dataclass
class PermissionToolCall:
    id: str
    type: str = "function"
    function: dict[str, str] = field(default_factory=lambda: {"name": "", "arguments": ""})


@dataclass
class PermissionToolExecution:
    toolCallId: str
    content: str
    result: dict[str, Any]


@dataclass
class PermissionPlan:
    permissions: list[MessageToolPermission]
    askPermissions: list[AskPermissionRequest]


@dataclass
class PermissionSettings:
    allow: list[PermissionScope] = field(default_factory=list)
    deny: list[PermissionScope] = field(default_factory=list)
    ask: list[PermissionScope] = field(default_factory=list)
    defaultMode: PermissionDefaultMode = "allowAll"


@dataclass
class ComputeToolCallPermissionsOptions:
    sessionId: str
    projectRoot: str
    toolCalls: list[Any]
    settings: PermissionSettings | None = None
    readPermissionExemptPaths: list[str] | None = None
    resolveSnippetPath: Any = None


def parse_tool_call_for_permissions(tool_call: Any) -> PermissionToolCall | None:
    if not isinstance(tool_call, dict):
        return None
    record_id = tool_call.get("id")
    if not isinstance(record_id, str):
        return None
    func = tool_call.get("function")
    if not isinstance(func, dict):
        return None
    func_name = func.get("name")
    if not isinstance(func_name, str):
        return None
    func_args = func.get("arguments")
    return PermissionToolCall(
        id=record_id,
        type="function",
        function={
            "name": func_name,
            "arguments": func_args if isinstance(func_args, str) else "",
        },
    )


def resolve_tool_call_permission(
    tool_call_id: str,
    permission_overrides: list[UserToolPermission] | None = None,
    message_permissions: list[MessageToolPermission] | None = None,
) -> PermissionDecision:
    if permission_overrides:
        for item in permission_overrides:
            if item.toolCallId == tool_call_id and item.permission in ("allow", "deny"):
                return item.permission
    if message_permissions:
        for item in message_permissions:
            if item.toolCallId == tool_call_id and item.permission in ("allow", "deny", "ask"):
                return item.permission
    return "allow"


def build_synthetic_tool_execution(
    tool_call: PermissionToolCall, error: str
) -> PermissionToolExecution:
    result: dict[str, Any] = {
        "ok": False,
        "name": tool_call.function["name"],
        "error": error,
    }
    return PermissionToolExecution(
        toolCallId=tool_call.id,
        content=json.dumps(result, indent=2),
        result=result,
    )


def build_permission_tool_execution(
    tool_call: PermissionToolCall,
    permission_overrides: list[UserToolPermission] | None = None,
    message_permissions: list[MessageToolPermission] | None = None,
) -> PermissionToolExecution | None:
    permission = resolve_tool_call_permission(
        tool_call.id,
        permission_overrides=permission_overrides,
        message_permissions=message_permissions,
    )
    if permission == "allow":
        return None
    if permission == "deny":
        return build_synthetic_tool_execution(
            tool_call,
            "User denied the required permission for this tool call. Do not try to bypass this decision.",
        )
    return build_synthetic_tool_execution(
        tool_call,
        "The user has not authorized this tool call yet. Retry only if the permission is still necessary.",
    )


def is_ask_permission_scope(value: Any) -> bool:
    return value in _VALID_SCOPES


def normalize_ask_permissions(value: Any) -> list[AskPermissionRequest] | None:
    if not isinstance(value, list):
        return None
    result: list[AskPermissionRequest] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        tool_call_id = item.get("toolCallId")
        name = item.get("name")
        if not isinstance(tool_call_id, str) or not isinstance(name, str):
            continue
        scopes_raw = item.get("scopes")
        scopes: list[AskPermissionScope] = (
            [s for s in scopes_raw if is_ask_permission_scope(s)]
            if isinstance(scopes_raw, list)
            else []
        )
        command = item.get("command")
        description = item.get("description")
        result.append(AskPermissionRequest(
            toolCallId=tool_call_id,
            scopes=scopes,
            name=name,
            command=command if isinstance(command, str) else name,
            description=description if isinstance(description, str) else None,
        ))
    return result if result else None


def has_user_permission_replies(value: dict[str, Any]) -> bool:
    permissions = value.get("permissions")
    always_allows = value.get("alwaysAllows")
    return bool(
        (isinstance(permissions, list) and len(permissions) > 0)
        or (isinstance(always_allows, list) and len(always_allows) > 0)
    )


def _normalize_file_path(file_path: str) -> str:
    return os.path.normpath(file_path)


def _is_absolute_file_path(file_path: str) -> bool:
    return os.path.isabs(file_path)


def is_path_in_project(project_root: str, file_path: str) -> bool:
    normalized = _normalize_file_path(file_path)
    absolute_path = normalized if _is_absolute_file_path(normalized) else os.path.join(project_root, normalized)
    absolute_path = os.path.realpath(absolute_path)
    project_root_real = os.path.realpath(project_root)
    try:
        relative = os.path.relpath(absolute_path, project_root_real)
    except ValueError:
        return False
    return relative == "" or (not relative.startswith("..") and not os.path.isabs(relative))


def is_path_in_any_directory(
    project_root: str, file_path: str, directories: list[str] | None
) -> bool:
    if not directories:
        return False
    normalized = _normalize_file_path(file_path)
    absolute_path = normalized if _is_absolute_file_path(normalized) else os.path.join(project_root, normalized)
    absolute_path = os.path.realpath(absolute_path)
    for directory in directories:
        norm_dir = _normalize_file_path(directory)
        abs_dir = norm_dir if _is_absolute_file_path(norm_dir) else os.path.join(project_root, norm_dir)
        abs_dir = os.path.realpath(abs_dir)
        try:
            relative = os.path.relpath(absolute_path, abs_dir)
        except ValueError:
            continue
        if relative == "" or (not relative.startswith("..") and not os.path.isabs(relative)):
            return True
    return False


def format_tool_path_command(tool_name: str, file_path: str) -> str:
    return f"{tool_name} {file_path}" if file_path else tool_name


def parse_tool_arguments_for_permissions(raw_arguments: str) -> dict[str, Any]:
    if not raw_arguments:
        return {}
    try:
        parsed = json.loads(raw_arguments)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except (json.JSONDecodeError, ValueError):
        return {}


def resolve_edit_permission_path(
    session_id: str,
    args: dict[str, Any],
    resolve_snippet_path: Any = None,
) -> str:
    file_path = args.get("file_path", "")
    if isinstance(file_path, str) and file_path:
        return file_path
    snippet_id = args.get("snippet_id", "")
    if isinstance(snippet_id, str) and snippet_id and callable(resolve_snippet_path):
        result = resolve_snippet_path(session_id, snippet_id)
        if result:
            return result
    return ""


def parse_bash_side_effects(value: Any) -> list[AskPermissionScope]:
    if not isinstance(value, list):
        return ["unknown"]
    scopes: list[AskPermissionScope] = []
    for item in value:
        if not isinstance(item, str) or item not in _VALID_SCOPES:
            return ["unknown"]
        if item not in scopes:
            scopes.append(item)
    if "unknown" in scopes:
        return ["unknown"]
    return scopes


def evaluate_permission_scopes(
    scopes: list[AskPermissionScope],
    settings: PermissionSettings | None = None,
) -> PermissionDecision:
    if settings is None:
        settings = PermissionSettings()
    if "unknown" in scopes:
        return "ask"
    if len(scopes) == 0:
        return "allow"
    permission_scopes: list[PermissionScope] = [s for s in scopes if s != "unknown"]
    if any(s in settings.deny for s in permission_scopes):
        return "deny"
    if any(s in settings.ask for s in permission_scopes):
        return "ask"
    if all(s in settings.allow for s in permission_scopes):
        return "allow"
    return "ask" if settings.defaultMode == "askAll" else "allow"


def get_permission_scopes_requiring_ask(
    scopes: list[AskPermissionScope],
    settings: PermissionSettings | None = None,
) -> list[AskPermissionScope]:
    if settings is None:
        settings = PermissionSettings()
    result: list[AskPermissionScope] = []
    for scope in scopes:
        if scope == "unknown":
            result.append(scope)
            continue
        if scope in settings.deny:
            continue
        if scope in settings.ask:
            result.append(scope)
            continue
        if scope in settings.allow:
            continue
        if settings.defaultMode == "askAll":
            result.append(scope)
    return result


def describe_tool_permission_request(
    session_id: str,
    project_root: str,
    tool_call: PermissionToolCall,
    read_permission_exempt_paths: list[str] | None = None,
    resolve_snippet_path: Any = None,
) -> AskPermissionRequest:
    name = tool_call.function["name"]
    args = parse_tool_arguments_for_permissions(tool_call.function["arguments"])

    if name in ("read", "Read"):
        file_path = args.get("file_path", "")
        if isinstance(file_path, str) and file_path:
            exempt = read_permission_exempt_paths
            scopes: list[AskPermissionScope] = (
                []
                if is_path_in_any_directory(project_root, file_path, exempt)
                else (
                    ["read-in-cwd"]
                    if is_path_in_project(project_root, file_path)
                    else ["read-out-cwd"]
                )
            )
        else:
            scopes = []
        return AskPermissionRequest(
            toolCallId=tool_call.id,
            name=name,
            command=format_tool_path_command("read", file_path if isinstance(file_path, str) else ""),
            scopes=scopes,
        )

    if name in ("write", "Write"):
        file_path = args.get("file_path", "")
        if isinstance(file_path, str) and file_path:
            scopes = (
                ["write-in-cwd"]
                if is_path_in_project(project_root, file_path)
                else ["write-out-cwd"]
            )
        else:
            scopes = []
        return AskPermissionRequest(
            toolCallId=tool_call.id,
            name=name,
            command=format_tool_path_command("write", file_path),
            scopes=scopes,
        )

    if name in ("edit", "Edit"):
        file_path = resolve_edit_permission_path(session_id, args, resolve_snippet_path)
        if file_path:
            scopes = (
                ["write-in-cwd"]
                if is_path_in_project(project_root, file_path)
                else ["write-out-cwd"]
            )
        else:
            scopes = ["write-out-cwd"]
        return AskPermissionRequest(
            toolCallId=tool_call.id,
            name=name,
            command=format_tool_path_command("edit", file_path),
            scopes=scopes,
        )

    if name in ("bash", "Bash"):
        command = args.get("command", "bash")
        description = args.get("description")
        return AskPermissionRequest(
            toolCallId=tool_call.id,
            name="bash",
            command=command if isinstance(command, str) else "bash",
            description=description if isinstance(description, str) else None,
            scopes=parse_bash_side_effects(args.get("sideEffects")),
        )

    if name == "WebSearch":
        query = args.get("query", "WebSearch")
        return AskPermissionRequest(
            toolCallId=tool_call.id,
            name=name,
            command=query if isinstance(query, str) else "WebSearch",
            scopes=["network"],
        )

    if name.startswith("mcp__"):
        return AskPermissionRequest(
            toolCallId=tool_call.id,
            name=name,
            command=name,
            scopes=["mcp"],
        )

    return AskPermissionRequest(
        toolCallId=tool_call.id,
        name=name,
        command=name,
        scopes=[],
    )


def compute_tool_call_permissions(options: ComputeToolCallPermissionsOptions) -> PermissionPlan:
    permissions: list[MessageToolPermission] = []
    ask_permissions: list[AskPermissionRequest] = []

    for raw_tool_call in options.toolCalls:
        tool_call = parse_tool_call_for_permissions(raw_tool_call)
        if tool_call is None:
            continue
        request = describe_tool_permission_request(
            session_id=options.sessionId,
            project_root=options.projectRoot,
            tool_call=tool_call,
            read_permission_exempt_paths=options.readPermissionExemptPaths,
            resolve_snippet_path=options.resolveSnippetPath,
        )
        permission = evaluate_permission_scopes(request.scopes, options.settings)
        permissions.append(MessageToolPermission(toolCallId=tool_call.id, permission=permission))
        if permission == "ask":
            ask_scopes = get_permission_scopes_requiring_ask(request.scopes, options.settings)
            ask_permissions.append(AskPermissionRequest(
                toolCallId=tool_call.id,
                scopes=ask_scopes if ask_scopes else request.scopes,
                name=request.name,
                command=request.command,
                description=request.description,
            ))

    return PermissionPlan(permissions=permissions, askPermissions=ask_permissions)


def append_project_permission_allows(
    project_root: str,
    scopes: list[PermissionScope] | None,
    inherited_permissions: PermissionSettings | None = None,
) -> None:
    if not isinstance(scopes, list) or len(scopes) == 0:
        return
    next_scopes = [s for s in scopes if s in _PERMISSION_SCOPES]
    if len(next_scopes) == 0:
        return

    settings_path = os.path.join(project_root, ".tigercli", "settings.json")
    settings: dict[str, Any] = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                parsed = json.load(f)
                if isinstance(parsed, dict):
                    settings = parsed
        except (json.JSONDecodeError, OSError):
            settings = {}

    existing_permissions = settings.get("permissions")
    if existing_permissions is not None and isinstance(existing_permissions, dict):
        permissions: dict[str, Any] = dict(existing_permissions)
    elif inherited_permissions is not None:
        permissions = {
            "allow": list(inherited_permissions.allow),
            "deny": list(inherited_permissions.deny),
            "ask": list(inherited_permissions.ask),
            "defaultMode": inherited_permissions.defaultMode,
        }
    else:
        permissions = {}

    current_allow = permissions.get("allow")
    current_allow_list: list[str] = (
        current_allow if isinstance(current_allow, list) else []
    )
    allow_list: list[str] = list(current_allow_list)
    for scope in next_scopes:
        if scope not in allow_list:
            allow_list.append(scope)

    current_deny = permissions.get("deny")
    current_deny_list: list[str] = (
        current_deny if isinstance(current_deny, list) else []
    )
    deny_list: list[str] = [s for s in current_deny_list if s not in next_scopes]

    current_ask = permissions.get("ask")
    current_ask_list: list[str] = (
        current_ask if isinstance(current_ask, list) else []
    )
    ask_list: list[str] = [s for s in current_ask_list if s not in next_scopes]

    changed = (
        len(allow_list) != len(current_allow_list)
        or len(deny_list) != len(current_deny_list)
        or len(ask_list) != len(current_ask_list)
    )
    if existing_permissions is not None and not changed:
        return

    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    new_permissions: dict[str, Any] = dict(permissions)
    new_permissions["allow"] = allow_list
    new_permissions["deny"] = deny_list
    new_permissions["ask"] = ask_list
    settings["permissions"] = new_permissions
    with open(settings_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(settings, indent=2) + "\n")


# ── Backward compatibility aliases ─────────────────────────────

@dataclass
class PermissionResult:
    allowed: bool
    level: str
    reason: str = ""

BASH_DENY_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"rm\s+-rf\s+~",
    r"sudo\s+rm",
    r"dd\s+if=",
    r"mkfs\.",
    r">\s*/dev/sd",
]
BASH_ASK_PATTERNS = [
    r"git\s+push\s+.*--force",
    r"git\s+push\s+.*-f",
    r"npm\s+publish",
    r"chmod\s+777",
    r"sudo\s+",
]
PLAN_MODE_ALLOWED = {"read", "glob", "grep", "websearch", "webfetch", "update_plan", "todowrite"}
DEFAULT_PERMISSIONS: dict[str, str] = {
    "bash": "ask", "write": "ask", "edit": "ask",
    "read": "allow", "glob": "allow", "grep": "allow",
    "websearch": "allow", "webfetch": "allow", "task": "allow", "todowrite": "allow",
    "update_plan": "allow",
}


def check_permission(tool_name: str, arguments: dict | None = None,
                     session_mode: str = "build") -> PermissionResult:
    """Legacy permission check — wraps deepcode port for compatibility."""
    if session_mode == "plan" and tool_name not in PLAN_MODE_ALLOWED:
        return PermissionResult(
            allowed=False, level="deny",
            reason=f"Tool '{tool_name}' not allowed in plan mode",
        )
    level = DEFAULT_PERMISSIONS.get(tool_name, "allow")
    if level == "allow":
        return PermissionResult(allowed=True, level="allow")
    if level == "deny":
        return PermissionResult(allowed=False, level="deny", reason=f"Denied: {tool_name}")
    if tool_name == "bash" and arguments:
        import re
        cmd = arguments.get("command", "")
        for pattern in BASH_DENY_PATTERNS:
            if re.search(pattern, cmd):
                return PermissionResult(allowed=False, level="deny", reason=f"Deny pattern: {pattern}")
        for pattern in BASH_ASK_PATTERNS:
            if re.search(pattern, cmd):
                return PermissionResult(allowed=False, level="ask", reason=f"Ask pattern: {pattern}")
    return PermissionResult(allowed=False, level="ask", reason=f"Tool '{tool_name}' requires approval")


def format_permission_prompt(tool_name: str, arguments: dict) -> str:
    import json
    if tool_name == "bash":
        return f"Allow shell command?\n  {arguments.get('command', '')}"
    elif tool_name == "write":
        return f"Allow write to {arguments.get('filePath', '')}?"
    elif tool_name == "edit":
        return f"Allow edit of {arguments.get('filePath', '')}?"
    return f"Allow {tool_name}?\n{json.dumps(arguments, indent=2)}"
