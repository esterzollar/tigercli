# HTTP Server

Beyond the terminal UI, TigerLiteCode can run as a local HTTP service with a
small JSON API and an optional web UI. Handy for integrations, a browser-based
chat, or wiring the agent into other tools.

## Requirements

The server needs FastAPI and Uvicorn, which aren't installed by default:

```bash
pip install fastapi uvicorn
```

## Start it

```bash
tigerlitecode serve                 # binds 127.0.0.1:8787
tigerlitecode serve --port 9000     # custom port
tigerlitecode serve --host 0.0.0.0 --port 8787
```

On startup it prints the address:

```text
TigerLiteCode server → http://127.0.0.1:8787
```

> By default it binds to `127.0.0.1` (localhost only). Only bind to `0.0.0.0` if
> you understand the exposure — the API can read and modify files and run
> commands in the served project.

## What it serves

- A **web UI** at `/` (when templates are present) — a browser chat with the
  same sessions and models as the TUI.
- A **JSON API** under `/api/...` for sessions, messages, models, and stats.

The exact routes are defined in `tigercli/api/routes.py`. Open
`http://localhost:8787/docs` for the interactive FastAPI documentation once the
server is running.

## Configuration

The default host and port come from your settings and can be overridden by flags
or environment variables:

```bash
TIGERCLI_SERVER_HOST=127.0.0.1
TIGERCLI_SERVER_PORT=8787
```

See [Configuration](configuration.md) for the full list.

## Notes

- The optional MCP (Model Context Protocol) endpoints are present but the MCP
  manager is not bundled in this release; the MCP refresh route will return an
  error if called. Everything else works normally.
