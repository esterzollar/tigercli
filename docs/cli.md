# CLI Reference

The command is `tigerlitecode` (the `tigercli` alias is identical). With no
subcommand it launches the interactive TUI in the current directory.

```bash
tigerlitecode                 # launch the TUI here
tigerlitecode -p "<prompt>"   # launch with a prompt pre-filled
tigerlitecode --version       # print the version
tigerlitecode --help          # show top-level help
```

## `run` — one-shot, non-interactive

Send a single prompt and print the response to stdout.

```bash
tigerlitecode run "<prompt>"
```

| Flag | Short | Description |
|------|-------|-------------|
| `--session <id>` | `-s` | Use a specific session |
| `--continue` | | Continue the most recent session |
| `--provider <name>` | `-p` | Provider: `deepseek`, `opencode-zen`, `opencode-go`, `openai` |
| `--model <id>` | `-m` | Model ID |
| `--mode <mode>` | | `build` (default) or `plan` |
| `--thinking` | | Enable reasoning/thinking mode |
| `--effort <level>` | | `low`, `medium`, `high` (default), `max` |
| `--files <paths...>` | `-f` | Files to include as context |
| `--project <dir>` | `-d` | Project directory |
| `--title <text>` | | Title for a new session |
| `--yes` | `-y` | Auto-approve all permission prompts |
| `--json` | | Emit `{"session_id", "response"}` as JSON |

Examples:

```bash
tigerlitecode run --files src/auth.ts src/middleware.ts "fix the token refresh bug"
tigerlitecode run --continue "now add tests for that"
tigerlitecode run --thinking --effort max "design a caching strategy"
tigerlitecode run --provider opencode-go --model deepseek-v4-pro --json "refactor this"
```

## `session` — manage history

```bash
tigerlitecode session <subcommand> [...]
```

| Subcommand | Description | Notable flags |
|------------|-------------|---------------|
| `list` | List sessions | `--limit/-n`, `--archived` |
| `resume <id>` | Show full session info | |
| `delete <id>` | Delete a session | |
| `search [query]` | Full-text search titles & messages | `--project/-d`, `--limit/-n` |
| `fork <id>` | Branch a session | `--at <message_id>`, `--title` |
| `archive <id>` | Move to archive | |
| `unarchive <id>` | Restore from archive | |
| `pin <id>` | Pin to top of the list | `--unpin` |
| `export <id>` | Export the transcript | `--format/-f markdown\|json`, `--output/-o <file>` |

Examples:

```bash
tigerlitecode session list -n 50
tigerlitecode session search "auth refactor"
tigerlitecode session export ses_abc123 --format markdown -o session.md
tigerlitecode session fork ses_abc123 --title "experiment"
```

## `stats` — usage & cost

```bash
tigerlitecode stats
```

Prints total requests, tokens in/out, cache-hit rate, and estimated cost,
followed by a per-session breakdown.

## `serve` — optional HTTP API

```bash
tigerlitecode serve --port 8787
```

Requires `pip install fastapi uvicorn`. See [HTTP Server](server.md).

| Flag | Description |
|------|-------------|
| `--host <host>` | Bind address (default `127.0.0.1`) |
| `--port <port>` | Port (default `8787`) |

## `init` — scaffold project instructions

```bash
tigerlitecode init
```

Creates an `AGENTS.md` in the current directory (or `--project/-d <dir>`) if one
doesn't already exist. See [Project Files](project-files.md).
