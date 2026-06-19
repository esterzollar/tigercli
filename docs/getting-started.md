# Getting Started

This guide takes you from zero to your first AI-assisted edit.

## Requirements

- **Python 3.11 or newer** — runs the agent engine.
- **Node.js 18 or newer** — runs the terminal UI (`tui-ts`).
- An API key for at least one provider (DeepSeek, OpenCode, or any
  OpenAI-compatible endpoint).

## Install

```bash
# 1. Clone the repository
git clone https://github.com/<your-org>/TigerLiteCode.git
cd TigerLiteCode

# 2. Install the Python engine (editable install for development)
pip install -e .

# 3. Build the terminal UI once
cd tui-ts
npm install
npm run build
cd ..
```

After this you'll have two commands on your `PATH`:

- `tigerlitecode` — the primary command.
- `tigercli` — an alias that behaves identically (kept for compatibility).

## Add an API key

Each provider uses its **own** environment variable — they are not all DeepSeek
keys. Only DeepSeek uses the `TIGER`-flavored name (deliberately, so it never
clashes with an unrelated `DEEPSEEK_API_KEY` already in your shell); OpenCode and
OpenAI use their standard names:

```bash
export DEEPSEEK_API_TIGER_KEY="sk-..."   # DeepSeek (note: ..._API_TIGER_KEY)
export OPENCODE_API_KEY="..."            # OpenCode
export OPENAI_API_KEY="sk-..."           # OpenAI / OpenAI-compatible
```

Set only the one(s) for the provider you actually use.

Prefer not to use environment variables at all? Launch the TUI and run
`/model` — you can pick a provider and **paste that provider's key right there**.
Keys entered this way are saved per-provider to `~/.config/tigercli/auth.json`,
so you never have to export anything.

See [Providers & Models](providers.md) for the full list.

## First run

From inside any project directory:

```bash
tigerlitecode
```

You'll land in the interactive TUI. Try something concrete:

> *"Give me a one-paragraph summary of what this repo does."*

Press **Enter** to send. Watch the response stream in. When the agent wants to
run a shell command or write a file, it asks for approval first.

## First edit

Ask for a real change:

> *"Add a `--version` flag to the CLI and print the package version."*

The agent will read the relevant files, propose an `edit` with a diff preview,
and ask you to approve it. Approve, and the change lands on disk.

## One-shot mode

Don't need the full UI? Run a single prompt and get the answer on stdout —
perfect for scripts and CI:

```bash
tigerlitecode run "list the public functions in src/api.py"
```

## Where to next

- [The TUI](tui.md) — keys and slash commands.
- [CLI Reference](cli.md) — every command and flag.
- [Token Saving](token-saving.md) — why this agent is cheap to run.
