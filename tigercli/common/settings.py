from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict, Union

DEEPSEEK_V4_MODELS: set[str] = {"deepseek-v4-flash", "deepseek-v4-pro"}


def _is_deepseek_v4(model: str) -> bool:
    return "deepseek-v4" in model.lower()


# Models known to lack vision/image input. DeepSeek V4 models DO support
# vision via the API, so only older (non-V4) deepseek models are excluded.
def _is_non_multimodal(model: str) -> bool:
    m = model.lower()
    if "deepseek" in m:
        return not _is_deepseek_v4(model)
    return False


def defaults_to_thinking_mode(model: str) -> bool:
    return _is_deepseek_v4(model)


# Providers whose chat route does NOT accept OpenAI-style image_url content
# parts. The opencode-go gateway rejects them with a 400 deserialize error
# ("unknown variant image_url, expected text"), so image input must be gated
# out for every model routed through it regardless of the underlying model's
# own vision capability.
_NON_MULTIMODAL_PROVIDERS: set[str] = {"opencode-go"}


def supports_multimodal(model: str, provider: str | None = None) -> bool:
    if provider and provider.lower() in _NON_MULTIMODAL_PROVIDERS:
        return False
    return not _is_non_multimodal(model)

ReasoningEffort = Literal["high", "max"]

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

PermissionDefaultMode = Literal["allowAll", "askAll"]


class McpServerConfig(TypedDict, total=False):
    command: str
    args: List[str]
    env: Dict[str, str]


class PermissionSettings(TypedDict, total=False):
    allow: List[PermissionScope]
    deny: List[PermissionScope]
    ask: List[PermissionScope]
    defaultMode: PermissionDefaultMode


EnabledSkillsSettings = Dict[str, bool]


class DeepcodingEnv(TypedDict, total=False):
    MODEL: str
    BASE_URL: str
    API_KEY: str
    TEMPERATURE: str
    THINKING_ENABLED: str
    REASONING_EFFORT: str
    DEBUG_LOG_ENABLED: str
    TELEMETRY_ENABLED: str


class TigerCLISettings(TypedDict, total=False):
    env: DeepcodingEnv
    model: str
    temperature: float
    thinkingEnabled: bool
    reasoningEffort: ReasoningEffort
    debugLogEnabled: bool
    telemetryEnabled: bool
    notify: str
    webSearchTool: str
    mcpServers: Dict[str, McpServerConfig]
    permissions: PermissionSettings
    enabledSkills: EnabledSkillsSettings


class ResolvedTigerCLISettings(TypedDict, total=False):
    env: Dict[str, str]
    apiKey: Optional[str]
    baseURL: str
    model: str
    temperature: Optional[float]
    thinkingEnabled: bool
    reasoningEffort: ReasoningEffort
    debugLogEnabled: bool
    telemetryEnabled: bool
    notify: Optional[str]
    webSearchTool: Optional[str]
    mcpServers: Optional[Dict[str, McpServerConfig]]
    permissions: PermissionSettings
    enabledSkills: EnabledSkillsSettings


class ModelConfigSelection(TypedDict):
    model: str
    thinkingEnabled: bool
    reasoningEffort: ReasoningEffort


SettingsProcessEnv = Dict[str, Optional[str]]


def _first_defined(*values: object) -> object:
    for v in values:
        if v is not None:
            return v
    return None


def _resolve_reasoning_effort(value: object) -> Optional[ReasoningEffort]:
    if value in ("high", "max"):
        return value  # type: ignore[return-value]
    return None


def _parse_boolean(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in ("1", "true", "enabled", "yes", "on"):
        return True
    if normalized in ("0", "false", "disabled", "no", "off"):
        return False
    return None


def _parse_temperature(value: object) -> Optional[float]:
    raw: Optional[float] = None
    if isinstance(value, (int, float)):
        raw = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            raw = float(value)
        except ValueError:
            return None
    if raw is None or raw < 0 or raw > 2:
        return None
    return raw


def _trim_string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


VALID_PERMISSION_SCOPES: set[str] = {
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
}


def _normalize_permission_list(value: object) -> List[PermissionScope]:
    if not isinstance(value, list):
        return []
    result: List[PermissionScope] = []
    for item in value:
        if isinstance(item, str) and item in VALID_PERMISSION_SCOPES:
            if item not in result:
                result.append(item)  # type: ignore[arg-type]
    return result


def _merge_permission_lists(*lists: Optional[List[PermissionScope]]) -> List[PermissionScope]:
    result: List[PermissionScope] = []
    for lst in lists:
        for scope in lst or []:
            if scope not in result:
                result.append(scope)
    return result


def _normalize_permission_default_mode(value: object) -> Optional[PermissionDefaultMode]:
    if value in ("allowAll", "askAll"):
        return value  # type: ignore[return-value]
    return None


def _normalize_permissions(settings: Optional[PermissionSettings]) -> PermissionSettings:
    s = settings or {}
    return {
        "allow": _normalize_permission_list(s.get("allow")),
        "deny": _normalize_permission_list(s.get("deny")),
        "ask": _normalize_permission_list(s.get("ask")),
        "defaultMode": _normalize_permission_default_mode(s.get("defaultMode")) or "allowAll",
    }


def _merge_permissions(
    user_settings: Optional[TigerCLISettings],
    project_settings: Optional[TigerCLISettings],
) -> PermissionSettings:
    user_perms = _normalize_permissions((user_settings or {}).get("permissions"))
    project_perms = _normalize_permissions((project_settings or {}).get("permissions"))
    return {
        "allow": _merge_permission_lists(user_perms.get("allow"), project_perms.get("allow")),
        "deny": _merge_permission_lists(user_perms.get("deny"), project_perms.get("deny")),
        "ask": _merge_permission_lists(user_perms.get("ask"), project_perms.get("ask")),
        "defaultMode": (
            project_perms["defaultMode"]
            if project_settings and "permissions" in project_settings
            else user_perms["defaultMode"]
            if user_settings and "permissions" in user_settings
            else "allowAll"
        ),
    }


def _normalize_enabled_skills(value: object) -> EnabledSkillsSettings:
    if not isinstance(value, dict):
        return {}
    result: EnabledSkillsSettings = {}
    for name, enabled in value.items():
        if isinstance(name, str) and name and isinstance(enabled, bool):
            result[name] = enabled
    return result


def _merge_enabled_skills(
    user_settings: Optional[TigerCLISettings],
    project_settings: Optional[TigerCLISettings],
) -> EnabledSkillsSettings:
    result = _normalize_enabled_skills((user_settings or {}).get("enabledSkills"))
    result.update(_normalize_enabled_skills((project_settings or {}).get("enabledSkills")))
    return result


def _normalize_env(env: object) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not isinstance(env, dict):
        return result
    for key, value in env.items():
        if isinstance(value, str):
            result[str(key)] = value
    return result


def collect_tigercli_env(process_env: Optional[SettingsProcessEnv] = None) -> Dict[str, str]:
    if process_env is None:
        process_env = dict(os.environ)
    result: Dict[str, str] = {}
    prefix = "TIGERCLI_"
    for key, value in process_env.items():
        if not key.startswith(prefix) or not isinstance(value, str):
            continue
        stripped_key = key[len(prefix):]
        if stripped_key:
            result[stripped_key] = value
    return result


def _extract_mcp_env(env: Dict[str, str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for key, value in env.items():
        if not key.startswith("MCP_"):
            continue
        stripped_key = key[len("MCP_"):]
        if stripped_key:
            result[stripped_key] = value
    return result


def _merge_mcp_servers(
    user_settings: Optional[TigerCLISettings],
    project_settings: Optional[TigerCLISettings],
    user_env: Dict[str, str],
    project_env: Dict[str, str],
    system_env: Dict[str, str],
) -> Optional[Dict[str, McpServerConfig]]:
    user_servers = (user_settings or {}).get("mcpServers") or {}
    project_servers = (project_settings or {}).get("mcpServers") or {}
    server_names = set(user_servers.keys()) | set(project_servers.keys())
    if not server_names:
        return None

    user_mcp_env = _extract_mcp_env(user_env)
    project_mcp_env = _extract_mcp_env(project_env)
    system_mcp_env = _extract_mcp_env(system_env)
    merged: Dict[str, McpServerConfig] = {}

    for name in server_names:
        user_config = user_servers.get(name) or {}
        project_config = project_servers.get(name) or {}
        command = project_config.get("command") or user_config.get("command")
        if not command:
            continue

        env: Dict[str, str] = {}
        env.update(user_env)
        env.update(user_config.get("env") or {})
        env.update(user_mcp_env)
        env.update(project_env)
        env.update(project_config.get("env") or {})
        env.update(project_mcp_env)
        env.update(system_env)
        env.update(system_mcp_env)

        config: McpServerConfig = {"command": command}
        args = project_config.get("args") if project_config.get("args") is not None else user_config.get("args")
        if args is not None:
            config["args"] = args
        if env:
            config["env"] = env
        merged[name] = config

    return merged if merged else None


def resolve_settings_sources(
    user_settings: Optional[TigerCLISettings],
    project_settings: Optional[TigerCLISettings],
    defaults: Dict[str, str],
    process_env: Optional[SettingsProcessEnv] = None,
) -> ResolvedTigerCLISettings:
    if process_env is None:
        process_env = dict(os.environ)

    user_env = _normalize_env((user_settings or {}).get("env"))
    project_env = _normalize_env((project_settings or {}).get("env"))
    system_env = collect_tigercli_env(process_env)

    env: Dict[str, str] = {}
    env.update(user_env)
    env.update(project_env)
    env.update(system_env)

    model = (
        _trim_string(system_env.get("MODEL"))
        or _trim_string((project_settings or {}).get("model"))
        or _trim_string(project_env.get("MODEL"))
        or _trim_string((user_settings or {}).get("model"))
        or _trim_string(user_env.get("MODEL"))
        or defaults["model"]
    )

    thinking_enabled = _first_defined(
        _parse_boolean(system_env.get("THINKING_ENABLED")),
        _parse_boolean((project_settings or {}).get("thinkingEnabled")),
        _parse_boolean(project_env.get("THINKING_ENABLED")),
        _parse_boolean((user_settings or {}).get("thinkingEnabled")),
        _parse_boolean(user_env.get("THINKING_ENABLED")),
        defaults_to_thinking_mode(model),
    )
    assert isinstance(thinking_enabled, bool)

    reasoning_effort = _first_defined(
        _resolve_reasoning_effort(system_env.get("REASONING_EFFORT")),
        _resolve_reasoning_effort((project_settings or {}).get("reasoningEffort")),
        _resolve_reasoning_effort(project_env.get("REASONING_EFFORT")),
        _resolve_reasoning_effort((user_settings or {}).get("reasoningEffort")),
        _resolve_reasoning_effort(user_env.get("REASONING_EFFORT")),
        "max",
    )
    assert isinstance(reasoning_effort, str)

    temperature = _first_defined(
        _parse_temperature(system_env.get("TEMPERATURE")),
        _parse_temperature((project_settings or {}).get("temperature")),
        _parse_temperature(project_env.get("TEMPERATURE")),
        _parse_temperature((user_settings or {}).get("temperature")),
        _parse_temperature(user_env.get("TEMPERATURE")),
    )

    debug_log_enabled = _first_defined(
        _parse_boolean(system_env.get("DEBUG_LOG_ENABLED")),
        _parse_boolean((project_settings or {}).get("debugLogEnabled")),
        _parse_boolean(project_env.get("DEBUG_LOG_ENABLED")),
        _parse_boolean((user_settings or {}).get("debugLogEnabled")),
        _parse_boolean(user_env.get("DEBUG_LOG_ENABLED")),
        False,
    )
    assert isinstance(debug_log_enabled, bool)

    telemetry_enabled = _first_defined(
        _parse_boolean(system_env.get("TELEMETRY_ENABLED")),
        _parse_boolean((project_settings or {}).get("telemetryEnabled")),
        _parse_boolean(project_env.get("TELEMETRY_ENABLED")),
        _parse_boolean((user_settings or {}).get("telemetryEnabled")),
        _parse_boolean(user_env.get("TELEMETRY_ENABLED")),
        True,
    )
    assert isinstance(telemetry_enabled, bool)

    notify = (
        _trim_string(system_env.get("NOTIFY"))
        or _trim_string((project_settings or {}).get("notify"))
        or _trim_string((user_settings or {}).get("notify"))
        or ""
    )
    web_search_tool = (
        _trim_string(system_env.get("WEB_SEARCH_TOOL"))
        or _trim_string((project_settings or {}).get("webSearchTool"))
        or _trim_string((user_settings or {}).get("webSearchTool"))
        or ""
    )

    api_key = _trim_string(env.get("API_KEY")) or None
    base_url = _trim_string(env.get("BASE_URL")) or defaults["baseURL"]

    return {
        "env": env,
        "apiKey": api_key if api_key else None,
        "baseURL": base_url,
        "model": model,
        "temperature": temperature,
        "thinkingEnabled": thinking_enabled,
        "reasoningEffort": reasoning_effort,
        "debugLogEnabled": debug_log_enabled,
        "telemetryEnabled": telemetry_enabled,
        "notify": notify or None,
        "webSearchTool": web_search_tool or None,
        "mcpServers": _merge_mcp_servers(user_settings, project_settings, user_env, project_env, system_env),
        "permissions": _merge_permissions(user_settings, project_settings),
        "enabledSkills": _merge_enabled_skills(user_settings, project_settings),
    }


def resolve_settings(
    settings: Optional[TigerCLISettings],
    defaults: Dict[str, str],
    process_env: Optional[SettingsProcessEnv] = None,
) -> ResolvedTigerCLISettings:
    return resolve_settings_sources(settings, None, defaults, process_env)


def model_config_key(config: ModelConfigSelection) -> str:
    return f"thinking:{config['reasoningEffort']}" if config["thinkingEnabled"] else "thinking:none"


def apply_model_config_selection(
    settings: Optional[TigerCLISettings],
    current: ModelConfigSelection,
    selected: ModelConfigSelection,
) -> Dict[str, Any]:
    changed = selected["model"] != current["model"] or model_config_key(selected) != model_config_key(current)
    next_settings: TigerCLISettings = dict(settings or {})
    if not changed:
        return {"settings": next_settings, "changed": False}

    if selected["model"] != current["model"] or "model" in next_settings:
        next_settings["model"] = selected["model"]
    else:
        next_settings.pop("model", None)

    next_settings["thinkingEnabled"] = selected["thinkingEnabled"]
    if selected["thinkingEnabled"]:
        next_settings["reasoningEffort"] = selected["reasoningEffort"]

    return {"settings": next_settings, "changed": True}


# ---------------------------------------------------------------------------
# Default constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "opencode-go/deepseek-v4-pro"
DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"

# ---------------------------------------------------------------------------
# Settings file I/O
# ---------------------------------------------------------------------------


def get_user_settings_path() -> Path:
    return Path.home() / ".tigercli" / "settings.json"


def get_project_settings_path(project_root: str) -> Path:
    return Path(project_root) / ".tigercli" / "settings.json"


def read_settings_file(settings_path: Path) -> Optional[TigerCLISettings]:
    try:
        if not settings_path.exists():
            return None
        raw = settings_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def read_settings() -> Optional[TigerCLISettings]:
    return read_settings_file(get_user_settings_path())


def read_project_settings(project_root: str = "") -> Optional[TigerCLISettings]:
    if not project_root:
        project_root = os.getcwd()
    return read_settings_file(get_project_settings_path(project_root))


def write_settings_file(settings_path: Path, settings: TigerCLISettings) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(f"{json.dumps(settings, indent=2)}\n", encoding="utf-8")


def write_settings(settings: TigerCLISettings) -> None:
    write_settings_file(get_user_settings_path(), settings)


def write_project_settings(settings: TigerCLISettings, project_root: str = "") -> None:
    if not project_root:
        project_root = os.getcwd()
    write_settings_file(get_project_settings_path(project_root), settings)


def write_model_config_selection(
    selection: ModelConfigSelection,
    current: Optional[ModelConfigSelection] = None,
    project_root: str = "",
) -> Dict[str, Any]:
    if not project_root:
        project_root = os.getcwd()
    if current is None:
        current = resolve_current_settings(project_root)
    project_settings_path = get_project_settings_path(project_root)
    should_write_project_settings = project_settings_path.exists()
    raw_settings = read_project_settings(project_root) if should_write_project_settings else read_settings()
    result = apply_model_config_selection(raw_settings, current, selection)
    if result["changed"]:
        if should_write_project_settings:
            write_project_settings(result["settings"], project_root)
        else:
            write_settings(result["settings"])
    return result


def resolve_current_settings(project_root: str = "") -> ResolvedTigerCLISettings:
    if not project_root:
        project_root = os.getcwd()
    return resolve_settings_sources(
        read_settings(),
        read_project_settings(project_root),
        {"model": DEFAULT_MODEL, "baseURL": DEFAULT_BASE_URL},
        dict(os.environ),
    )


# ---------------------------------------------------------------------------
# Merged from tigercli/common/telemetry.py
# ---------------------------------------------------------------------------

_MACHINE_ID_DIR = Path.home() / ".tigercli"
_MACHINE_ID_FILE = _MACHINE_ID_DIR / "machine-id"


def get_telemetry_enabled() -> bool:
    settings = read_settings()
    if settings is None:
        return False
    enabled = settings.get("telemetryEnabled")
    if isinstance(enabled, bool):
        return enabled
    return False


def generate_machine_id() -> str:
    if _MACHINE_ID_FILE.exists():
        mid = _MACHINE_ID_FILE.read_text(encoding="utf-8").strip()
        if mid:
            return mid
    _MACHINE_ID_DIR.mkdir(parents=True, exist_ok=True)
    mid = str(uuid.uuid4())
    _MACHINE_ID_FILE.write_text(mid, encoding="utf-8")
    return mid


def report_new_prompt(
    enabled: bool = False,
    machine_id: str | None = None,
) -> None:
    if not enabled or not machine_id:
        return
