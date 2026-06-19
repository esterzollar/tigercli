# Configuration

TigerLiteCode reads configuration from three layers, in increasing priority:

1. **Built-in defaults** (below).
2. **Config files** — global, then project-level.
3. **Environment variables** — prefixed with `TIGERCLI_`, plus an optional
   `.env` file in the working directory.

Project values override global values; environment variables override both.

## Where files live

Paths follow the XDG Base Directory spec:

| File | Default location | Purpose |
|------|------------------|---------|
| Config | `~/.config/tigercli/config.json` | Default provider/model, recent models, compaction size |
| Auth | `~/.config/tigercli/auth.json` | Provider keys & base URLs (when set via `/model`) |
| Database | `~/.local/share/tigercli/tigercli.db` | Session history (SQLite) |
| Cache | `~/.cache/tigercli/` | Logs and transient data |
| Project config | `./.tigercli/config.json` | Per-project overrides |
| Project auth | `./.tigercli/auth.json` | Per-project keys |

> ⚠️ `auth.json`, `config.json`, the `.db`, and the `.tigercli/` directory hold
> secrets or machine-specific state. They're excluded by `.gitignore` — never
> commit them.

### Overriding locations

| Variable | Overrides |
|----------|-----------|
| `TIGERCLI_CONFIG_HOME` | Config directory |
| `TIGERCLI_DATA_HOME` | Data directory (database) |
| `TIGERCLI_CACHE_HOME` | Cache directory |
| `XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_CACHE_HOME` | The standard XDG roots |

## Environment variables

Every setting can be set via an environment variable with the `TIGERCLI_`
prefix. Common ones:

```bash
TIGERCLI_DEFAULT_PROVIDER=deepseek
TIGERCLI_DEFAULT_MODEL=deepseek-v4-pro
TIGERCLI_THINKING_ENABLED=false
```

API keys are read from provider-specific variables — see [Providers](providers.md).

## Settings reference

| Setting | Default | Meaning |
|---------|---------|---------|
| `default_provider` | `deepseek` | Provider used for new sessions |
| `default_model` | `deepseek-v4-pro` | Model used for new sessions |
| `default_mode` | `build` | `build` (acts) or `plan` (proposes) |
| `thinking_enabled` | `false` | Reasoning/CoT mode |
| `reasoning_effort` | `high` | `low` / `medium` / `high` / `max` |
| `max_turns` | `30` | Max tool-call iterations per request |
| `context_window` | `1000000` | Assumed model context window (tokens) |
| `compact_size` | `500000` | Auto-compaction threshold (tokens) |
| `compact_threshold` | `0.8` | Fraction of the window that triggers warnings |
| `server_host` | `127.0.0.1` | HTTP server bind address |
| `server_port` | `8787` | HTTP server port |
| `auto_approve` | `false` | Skip permission prompts |

The `context_window` and `compact_size` values are also adjustable at runtime
with `/compact_size` inside the TUI. See [Token Saving](token-saving.md).
