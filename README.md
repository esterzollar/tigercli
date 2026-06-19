# 🐅 TigerLiteCode

**A lightweight, token-saving coding agent that lives in your terminal.**

TigerLiteCode (or just *Tiger Lite*) is a CLI coding agent built around one
stubborn idea: a great coding assistant shouldn't burn a fortune in tokens to be
useful. It reads your code, edits files, runs commands, searches the web, and
remembers every conversation — while quietly structuring each request so your
provider's cache does most of the heavy lifting. Less spend, same sharp teeth.

```
                                                                                ..,co88oc.oo8888cc,..
                                                         o8o.               ..,o8889689ooo888o"88888888oooc..
                                                       .88888             .o888896888".88888888o'?888888888889ooo....
                                                       a888P          ..c6888969""..,"o888888888o.?8888888888"".ooo8888oo.
                                                       088P        ..atc88889"".,oo8o.86888888888o 88988889",o888888888888.
                                                       888t  ...coo688889"'.ooo88o88b.'86988988889 8688888'o8888896989^888o
                                                        888888888888"..ooo888968888888  "9o688888' "888988 8888868888'o88888
                                                         ""G8889""'ooo888888888888889 .d8o9889""'   "8688o."88888988"o888888o .
                                                                  o8888'""""""""""'   o8688"          88868. 888888.68988888"o8o.
                                                                  88888o.              "8888ooo.        '8888. 88888.8898888o"888o.
                                                                  "888888'               "888888'          '""8o"8888.8869888oo8888o .
                                                             . :.:::::::::::.: .     . :.::::::::.: .   . : ::.:."8888 "888888888888o
                                                                                                               :..8888,. "88888888888.
                                                                                                               .:o888.o8o.  "866o9888o
                                                                                                                :888.o8888.  "88."89".
                                                                                                               . 89  888888    "88":.
                                                                                                               :.     '8888o
                                                                                                                .       "8888..
                                                                                                                          888888o.
                                                                                                                           "888889,
                                                                                                                    . : :.:::::::.: :.

```

---

## Why "Lite"?

Most agents resend your whole conversation on every turn and pay full price for
it. Tiger Lite is built to be frugal:

- 🧠 **Cache-first message ordering** — requests are laid out so the provider's
  KV cache recognizes the prefix and replays it for ~99% less. No proxy, no
  local cache layer, just discipline.
- 🗜️ **Auto-compaction** — long chats summarize themselves before they bloat,
  so you keep context without paying for the whole transcript every turn.
- 🔍 **Focused subagents & skills** — heavy exploration runs in its own forked
  context and hands back a short answer, instead of dumping everything into your
  main thread.
- 📊 **It shows you the bill** — live cache-hit rate, token counts, and cost are
  always on screen. No surprises.

The result: a senior-engineer-grade assistant you can leave running all day
without watching the meter.

---

## Quick start

You'll need **Python 3.11+** and **Node.js 18+**.

```bash
# 1. Get the code
git clone https://github.com/esterzollar/tigercli.git
cd tigercli

# 2. One-command setup (installs the engine + builds the TUI)
#    Linux / macOS:
./build.sh
#    Windows (PowerShell):
#    ./build.ps1

# 3. Add a key. Each provider uses its own variable:
#      DeepSeek  ->  DEEPSEEK_API_TIGER_KEY   (the "TIGER" is DeepSeek-only,
#                    so it won't clash with a DEEPSEEK_API_KEY you already have)
#      OpenCode  ->  OPENCODE_API_KEY
#      OpenAI    ->  OPENAI_API_KEY
export OPENCODE_API_KEY="..."     # or whichever provider you use

# 4. Go
tigerlitecode
```

> Don't want to use environment variables? Just run `tigerlitecode`, open
> `/model`, pick your provider, and paste the key right there — it's saved for
> you. Each provider keeps its own key; they are **not** all DeepSeek keys.

Prefer to do it by hand?

```bash
pip install -e .                          # the Python engine
cd tui-ts && npm install && npm run build # the terminal UI
```

That `tigerlitecode` command drops you into the TUI in your current directory.
Ask it anything: *"Where does auth get validated?"*, *"Fix the flaky test in
`payments.py`"*, *"Add a `--dry-run` flag to the CLI."*

> Prefer the old name? `tigercli` is installed as an alias and works identically.

---

## A taste of it

**Interactive (the usual way):**

```bash
tigerlitecode                       # open the TUI here
tigerlitecode -p "explain this repo" # open with a prompt ready to send
```

**One-shot (great for scripts and CI):**

```bash
tigerlitecode run "summarize what changed in the last commit"
tigerlitecode run --files src/auth.ts "find the token-refresh bug"
tigerlitecode run --thinking --effort max "design a retry strategy"
```

**Pick up where you left off:**

```bash
tigerlitecode run --continue "now add tests for that"
tigerlitecode session list
tigerlitecode session resume ses_abc123
```

**See what you've spent:**

```bash
tigerlitecode stats
```

---

## The toolbox

Tiger Lite can actually *do* things, not just talk. Out of the box it has:

| Tool | What it does |
|------|--------------|
| `bash` | Run shell commands in your project |
| `read` | Read files (with line numbers) |
| `write` | Create or overwrite files |
| `edit` | Surgical find-and-replace edits |
| `glob` | Find files by pattern |
| `grep` | Search code with regex |
| `websearch` | Search the web (DuckDuckGo) |
| `webfetch` | Fetch and read a page |
| `task` | Hand work to a focused subagent |
| `skill` | Run a saved, reusable workflow |

Risky actions (shell commands, deletes) always ask before they run.

---

## Supported providers

Tiger Lite supports **3 providers**, each spoken to directly over its
OpenAI-compatible API:

1. **DeepSeek** — `DEEPSEEK_API_TIGER_KEY`
2. **OpenCode** — `OPENCODE_API_KEY` (both the **Zen** and **Go** endpoints)
3. **OpenAI** — `OPENAI_API_KEY` (and any OpenAI-compatible endpoint)

Switch any time with `/model` in the TUI or `--provider`/`--model` on the CLI.
Full details in [docs/providers.md](docs/providers.md).

---

## Build from source (autobuild)

The repo ships smart, cross-platform setup scripts that check your toolchain,
install the Python engine, and build the terminal UI in one go.

| OS | Command |
|----|---------|
| **Linux** | `./build.sh` |
| **macOS** | `./build.sh` |
| **Windows** (PowerShell) | `./build.ps1` |
| **Windows** (cmd.exe) | `build.bat` |

Each script:

- verifies that **Python 3.11+** and **Node.js 18+** are present (and stops with
  a clear message if not),
- runs `pip install -e .` to install the engine,
- runs `npm install && npm run build` in `tui-ts/` to build the UI,
- and tells you exactly what to do next.

```bash
# Linux / macOS
git clone https://github.com/esterzollar/tigercli.git
cd tigercli
./build.sh
```

```powershell
# Windows
git clone https://github.com/esterzollar/tigercli.git
cd tigercli
./build.ps1
```

---

## Documentation

The full guides live in [`docs/`](docs/):

| Guide | What's inside |
|-------|---------------|
| [Getting Started](docs/getting-started.md) | Install, first run, first edit |
| [CLI Reference](docs/cli.md) | Every command and flag |
| [The TUI](docs/tui.md) | Keys, slash commands, the screen |
| [Configuration](docs/configuration.md) | Env vars, files, where things live |
| [Providers & Models](docs/providers.md) | The 3 providers: DeepSeek, OpenCode, OpenAI |
| [Sessions](docs/sessions.md) | History, fork, search, export |
| [Token Saving](docs/token-saving.md) | How the cache & compaction really work |
| [Subagents & Skills](docs/subagents-and-skills.md) | Extend the agent for your project |
| [Project Files](docs/project-files.md) | `AGENTS.md`, `TIGER.md`, `.tigercli/` |
| [HTTP Server](docs/server.md) | The optional web API |
| [Troubleshooting](docs/troubleshooting.md) | When something goes sideways |

---

## How it stays cheap (the 30-second version)

```
Turn 1:  [system] [AGENTS.md] [file:auth.ts]  ["fix the bug"]
Turn 2:  [system] [AGENTS.md] [file:auth.ts]  [history]  ["also add logging"]
         └────────────── identical prefix ──────────────┘
                         the provider replays this from cache, ~99% cheaper
```

Stable ordering means the expensive part of every request is usually already
paid for. See [docs/token-saving.md](docs/token-saving.md) for the whole story.

---

## Contributing

```bash
ruff check tigercli/          # lint the Python engine
cd tui-ts && npm run check    # type-check the TUI
```

PRs welcome. Keep changes focused and idiomatic.

---

## License

See [LICENSE.md](LICENSE.md).

---

<p align="center">Made by <a href="https://github.com/esterzollar">@esterzollar</a> 🐅</p>
