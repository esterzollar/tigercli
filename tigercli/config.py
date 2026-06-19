import os
import json
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Per-provider override for the API-key environment variable name. By default a
# provider "foo-bar" reads FOO_BAR_API_KEY / TIGERCLI_FOO_BAR_API_KEY. Deepseek
# intentionally uses DEEPSEEK_API_TIGER_KEY so it does not collide with an
# unrelated DEEPSEEK_API_KEY in the user's shell.
_PROVIDER_ENV_KEY_OVERRIDES: dict[str, str] = {
    "deepseek": "DEEPSEEK_API_TIGER_KEY",
}


def provider_env_key_names(provider_id: str) -> list[str]:
    """Env var names to check for a provider's API key, in priority order."""
    override = _PROVIDER_ENV_KEY_OVERRIDES.get(provider_id)
    if override:
        return [override, f"TIGERCLI_{override}"]
    base = f"{provider_id.upper().replace('-', '_')}_API_KEY"
    return [base, f"TIGERCLI_{base}"]


def provider_api_key_from_env(provider_id: str) -> str:
    for name in provider_env_key_names(provider_id):
        val = os.environ.get(name)
        if val:
            return val
    return ""


def _xdg_config_home() -> Path:
    if os.environ.get("TIGERCLI_CONFIG_HOME"):
        return Path(os.environ["TIGERCLI_CONFIG_HOME"])
    xdg = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    return Path(xdg) / "tigercli"


def _xdg_data_home() -> Path:
    if os.environ.get("TIGERCLI_DATA_HOME"):
        return Path(os.environ["TIGERCLI_DATA_HOME"])
    xdg = os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    return Path(xdg) / "tigercli"


def _xdg_cache_home() -> Path:
    if os.environ.get("TIGERCLI_CACHE_HOME"):
        return Path(os.environ["TIGERCLI_CACHE_HOME"])
    xdg = os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
    return Path(xdg) / "tigercli"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TIGERCLI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    config_home: Path = Field(default_factory=_xdg_config_home)
    data_home: Path = Field(default_factory=_xdg_data_home)
    cache_home: Path = Field(default_factory=_xdg_cache_home)

    @property
    def db_path(self) -> Path:
        return self.data_home / "tigercli.db"

    @property
    def config_file(self) -> Path:
        return self.config_home / "config.json"

    @property
    def auth_file(self) -> Path:
        return self.config_home / "auth.json"

    @property
    def project_config_dir(self) -> Path:
        return Path.cwd() / ".tigercli"

    @property
    def project_config_file(self) -> Path:
        return self.project_config_dir / "config.json"

    @property
    def project_auth_file(self) -> Path:
        return self.project_config_dir / "auth.json"

    @property
    def plugins_dir(self) -> Path:
        return self.config_home / "plugins"

    default_provider: str = "deepseek"
    default_model: str = "deepseek-v4-pro"
    default_mode: str = "build"
    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_TIGER_KEY")
    opencode_api_key: str | None = Field(default=None, alias="OPENCODE_API_KEY")
    deepseek_base_url: str = "https://api.deepseek.com"
    opencode_zen_base_url: str = "https://opencode.ai/zen/v1"
    opencode_go_base_url: str = "https://opencode.ai/zen/go/v1"

    thinking_enabled: bool = False
    reasoning_effort: str = "high"

    max_turns: int = 30
    compact_threshold: float = 0.8
    # Total context window assumed for the active model and the absolute token
    # threshold at which the conversation auto-compacts itself mid-turn. Default
    # is a 1M-token window auto-compacting at half (500K). Both are adjustable
    # at runtime via /compact_size.
    context_window: int = 1_000_000
    compact_size: int = 500_000

    server_host: str = "127.0.0.1"
    server_port: int = 8787

    auto_approve: bool = False
    auto_retry: bool = False
    fallback_enabled: bool = False


settings = Settings()


KNOWN_PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "models": [],
        "website": "https://platform.deepseek.com",
    },
    "opencode-zen": {
        "name": "OpenCode Zen",
        "base_url": "https://opencode.ai/zen/v1",
        "models": [],
        "website": "https://opencode.ai/zen",
    },
    "opencode-go": {
        "name": "OpenCode Go",
        "base_url": "https://opencode.ai/zen/go/v1",
        "models": [],
        "website": "https://opencode.ai/go",
    },
    "openai": {
        "name": "OpenAI Compatible",
        "base_url": "https://api.openai.com/v1",
        "models": [],
        "website": "https://platform.openai.com",
    },
}


def _read_json_file(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _atomic_write(path: Path, serialized: str) -> None:
    """Write atomically so a crash mid-write cannot truncate an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(serialized)
    os.replace(tmp, path)


def _write_json_with_fallback(primary: Path, fallback: Path, payload: dict) -> Path:
    serialized = json.dumps(payload, indent=2)
    for path in (primary, fallback):
        try:
            _atomic_write(path, serialized)
            return path
        except OSError:
            continue
    raise OSError(f"Could not write config to {primary} or fallback {fallback}")


def _write_json_best_effort(paths: list[Path], payload: dict) -> Path | None:
    serialized = json.dumps(payload, indent=2)
    first_success: Path | None = None
    for path in paths:
        try:
            _atomic_write(path, serialized)
            if first_success is None:
                first_success = path
        except OSError:
            continue
    return first_success


def load_auth() -> dict:
    # Merge global + project so a project file that configures only one
    # provider does not hide globally-configured providers. Project values
    # win per top-level key.
    global_auth = _read_json_file(settings.auth_file)
    project_auth = _read_json_file(settings.project_auth_file)
    return {**global_auth, **project_auth}


def is_provider_configured(provider_id: str, auth: dict | None = None) -> bool:
    auth = auth or load_auth()
    cfg = auth.get(provider_id, {})
    # Legacy key fallback
    if not cfg and provider_id == "opencode-go":
        cfg = auth.get("opencode", {})
    if provider_id in ("opencode-zen", "opencode-go"):
        has_key = bool(
            cfg.get("api_key")
            or os.environ.get("OPENCODE_API_KEY")
            or os.environ.get("TIGERCLI_OPENCODE_API_KEY")
        )
        # opencode-go has a usable free tier, so a stored model list alone is
        # enough. opencode-zen requires a real key — a leftover model list
        # from a previous live-fetch must NOT keep it "configured".
        if provider_id == "opencode-go":
            return has_key or bool(cfg.get("models"))
        return has_key
    return bool(
        cfg.get("api_key")
        or provider_api_key_from_env(provider_id)
    )


def is_legacy_opencode_zen_auth(auth: dict | None = None) -> bool:
    return False


def save_auth(auth: dict) -> None:
    written = _write_json_best_effort([settings.project_auth_file, settings.auth_file], auth)
    if written is None:
        raise OSError(f"Could not write auth to {settings.project_auth_file} or {settings.auth_file}")


def load_config() -> dict:
    # Merge global + project; project values override per key.
    global_config = _read_json_file(settings.config_file)
    project_config = _read_json_file(settings.project_config_file)
    return {**global_config, **project_config}


def save_config(config: dict) -> None:
    written = _write_json_best_effort([settings.project_config_file, settings.config_file], config)
    if written is None:
        raise OSError(f"Could not write config to {settings.project_config_file} or {settings.config_file}")


def save_compact_size(compact_size: int, context_window: int | None = None) -> None:
    """Persist the auto-compact token threshold (and optionally the context
    window) and update the in-memory settings so the change takes effect for
    the current run as well as future runs."""
    config = load_config()
    compact_size = max(1000, int(compact_size))
    config["compact_size"] = compact_size
    settings.compact_size = compact_size
    if context_window is not None:
        context_window = max(compact_size, int(context_window))
        config["context_window"] = context_window
        settings.context_window = context_window
    save_config(config)


def save_default_selection(provider: str, model: str) -> None:
    config = load_config()
    config["default_provider"] = provider
    config["default_model"] = model
    recent_models = config.get("recent_models")
    if not isinstance(recent_models, list):
        recent_models = []
    next_recent = [{"provider": provider, "model": model}]
    for entry in recent_models:
        if not isinstance(entry, dict):
            continue
        entry_provider = entry.get("provider")
        entry_model = entry.get("model")
        if not isinstance(entry_provider, str) or not isinstance(entry_model, str):
            continue
        if entry_provider == provider and entry_model == model:
            continue
        next_recent.append({"provider": entry_provider, "model": entry_model})
    config["recent_models"] = next_recent[:8]
    # Update in-memory settings before the disk write so a concurrent reader
    # never observes the new on-disk default with the old in-memory default.
    settings.default_provider = provider
    settings.default_model = model
    save_config(config)


def get_active_provider_model(auth: dict | None = None) -> tuple[str, str]:
    auth = auth or load_auth()
    default_provider = settings.default_provider
    default_model = settings.default_model

    def _cfg_for(provider_id: str) -> dict:
        cfg = auth.get(provider_id, {})
        # Legacy key fallback: stored models may live under "opencode".
        if not cfg and provider_id == "opencode-go":
            cfg = auth.get("opencode", {})
        return cfg if isinstance(cfg, dict) else {}

    def _first_model(provider_id: str) -> str | None:
        cfg = _cfg_for(provider_id)
        models = cfg.get("models", [])
        for model in models:
            if isinstance(model, dict) and isinstance(model.get("id"), str):
                return model["id"]
            if isinstance(model, str):
                return model
        return None

    def _available_models(provider_id: str) -> list[str]:
        cfg = _cfg_for(provider_id)
        models = cfg.get("models", [])
        available: list[str] = []
        for model in models:
            if isinstance(model, dict) and isinstance(model.get("id"), str):
                available.append(model["id"])
            elif isinstance(model, str):
                available.append(model)
        return available

    def _provider_from_model(model: str, auth: dict | None = None) -> str | None:
        """Detect provider prefix in model name, or scan all model lists."""
        # Check known provider prefixes first
        for known_id, info in KNOWN_PROVIDERS.items():
            prefix = known_id + "/"
            if model.lower().startswith(prefix.lower()):
                return known_id
        # Legacy fallback: "opencode/..." → "opencode-go"
        if model.lower().startswith("opencode/"):
            return "opencode-go"
        # No prefix — scan each provider's model list for the name
        auth_data = load_auth() if auth is None else auth
        for known_id in KNOWN_PROVIDERS:
            cfg = auth_data.get(known_id, {})
            if not cfg and known_id == "opencode-go":
                cfg = auth_data.get("opencode", {})
            raw_models = cfg.get("models", [])
            for raw in raw_models:
                mid = raw["id"] if isinstance(raw, dict) else raw
                # Strip provider prefix for comparison
                for chk_id in (known_id, "opencode"):
                    if mid.startswith(chk_id + "/"):
                        mid = mid.removeprefix(chk_id + "/")
                        break
                if mid == model:
                    return known_id
        return None

    # If model name has a provider prefix, use that provider instead
    model_provider = _provider_from_model(default_model, auth)
    if model_provider and model_provider != default_provider:
        # The model belongs to a different provider — correct it
        if is_provider_configured(model_provider, auth):
            # Clean legacy prefix too ("opencode/" → "opencode-go/")
            model_clean = default_model
            for chk_id in (model_provider, "opencode"):
                if model_clean.startswith(chk_id + "/"):
                    model_clean = model_clean.removeprefix(chk_id + "/")
                    break
            return model_provider, model_clean

    if default_provider:
        if is_provider_configured(default_provider, auth):
            available = _available_models(default_provider)
            first_model = _first_model(default_provider)
            if first_model and available and default_model not in available:
                return default_provider, first_model
            if available:
                return default_provider, default_model
            # The default provider is "configured" (e.g. only via a stray env
            # var) but exposes no usable models. Prefer another configured
            # provider that actually has models before falling back to it.
            for provider_id in KNOWN_PROVIDERS:
                if provider_id == default_provider:
                    continue
                if is_provider_configured(provider_id, auth):
                    fm = _first_model(provider_id)
                    if fm:
                        return provider_id, fm
            return default_provider, default_model

    for provider_id in KNOWN_PROVIDERS:
        if is_provider_configured(provider_id, auth):
            first_model = _first_model(provider_id)
            if first_model:
                return provider_id, first_model
            return provider_id, default_model if provider_id == default_provider else settings.default_model

    return default_provider, default_model


def _apply_persisted_config() -> None:
    config = load_config()
    default_provider = config.get("default_provider")
    default_model = config.get("default_model")
    if isinstance(default_provider, str) and default_provider:
        settings.default_provider = default_provider
    if isinstance(default_model, str) and default_model:
        settings.default_model = default_model


_apply_persisted_config()
