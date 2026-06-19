# Troubleshooting

Quick fixes for the things most likely to trip you up.

## "No API key found" / authentication errors

The provider key isn't set, or it's the wrong variable name. Remember DeepSeek
uses `DEEPSEEK_API_TIGER_KEY` (with `TIGER`), not `DEEPSEEK_API_KEY`:

```bash
export DEEPSEEK_API_TIGER_KEY="sk-..."
```

Or set it interactively with `/model` in the TUI. Keys live in
`~/.config/tigercli/auth.json` — confirm they're there. See
[Providers](providers.md).

## The TUI won't start / "Connection to engine lost"

The terminal UI hasn't been built, or the build is stale:

```bash
cd tui-ts
npm install
npm run build
```

Then relaunch `tigerlitecode`. The UI (Node) and the engine (Python) talk over a
local socket; if the engine crashes the UI reports the lost connection — scroll
up for the underlying Python error.

## `tigerlitecode: command not found`

The package isn't installed (or not on your `PATH`):

```bash
pip install -e .
```

If you installed into a virtualenv, make sure it's activated. The `tigercli`
alias should also be available.

## Costs look high / cache rate is low

A low cache-hit rate in the status bar means the request prefix keeps changing.
Common causes:

- **Switching models mid-session** — pick one model per session.
- **Volatile file context** — keep stable instructions in `AGENTS.md` instead of
  re-pasting changing content.

See [Token Saving](token-saving.md) for how the cache works.

## The conversation feels like it "forgot" earlier context

It was auto-compacted. When history crosses `compact_size` (default 500k
tokens), older messages are summarized. Check usage with `/context`, and raise
the threshold if you have room:

```text
/compact_size 1m
```

## Images aren't being understood

The current model is text-only. TigerLiteCode strips images it can't send and
tells you so. Switch to a vision-capable model with `/model`, then paste again
with `Ctrl+V`.

## A shell command hangs or waits for input

Tools run non-interactively. If a command expects input it can stall — press
`Esc` to interrupt the turn, then rephrase so the command runs unattended (add
`--yes`/`-y` style flags, pipe input, etc.).

## Windows: "requires Git Bash"

The agent runs shell commands through `bash`. On Windows, install Git for
Windows (which provides Git Bash) so a `bash` executable is on your `PATH`.

## The server returns 500 on an MCP route

MCP endpoints are present but the MCP manager isn't bundled in this release. The
MCP refresh route will error; the rest of the API and the agent are unaffected.
See [HTTP Server](server.md).

## Resetting state

- **Start fresh in the UI:** `/new`.
- **Wipe a session:** `tigerlitecode session delete <id>`.
- **Full reset:** remove `~/.config/tigercli/` (config + keys) and
  `~/.local/share/tigercli/tigercli.db` (history). This deletes your keys and all
  sessions — back them up first if you need them.

Still stuck? Run with the engine output visible (launch from a terminal so
Python tracebacks print) and open an issue with the error and the command you
ran.
