# The Terminal UI

Running `tigerlitecode` with no subcommand opens the interactive TUI. It owns
the full terminal: a scrolling transcript of the conversation, a live status bar
(model, mode, cache rate, cost, context meter), and a prompt box at the bottom.

## Sending a prompt

Type your message and press **Enter**. Responses stream in token by token. When
the agent calls a tool you'll see a one-line summary (the file path, command, or
query) and, for `write`/`edit`, a colored diff preview.

## Editing keys

| Key | Action |
|-----|--------|
| `Enter` | Send the prompt |
| `Shift+Enter` | Insert a newline |
| `Home` / `End` | Jump to start/end of the current line |
| `Alt+Left` / `Alt+Right` | Move by word |
| `Ctrl+W` | Delete the previous word |
| `Ctrl+V` | Paste an image from the clipboard |
| `Ctrl+X` | Clear pasted images |
| `Esc` | Interrupt the current model turn |
| `Ctrl+Q` | Quit |
| `Ctrl+D` (twice) | Quit |

## Slash commands

Type `/` to open the commands-and-skills menu, or type a command directly.

| Command | What it does |
|---------|--------------|
| `/model` | Pick provider, model, thinking mode, and effort; enter API keys |
| `/new` | Start a fresh conversation |
| `/init` | Create an `AGENTS.md` for this project |
| `/resume` | Pick a previous conversation to continue |
| `/continue` | Continue the active conversation (or resume if empty) |
| `/compact [focus]` | Summarize the conversation now, optionally focused on a topic |
| `/compact_size <n>` | Set the auto-compaction threshold (e.g. `300k`, `1m`) |
| `/context` | Show token/context usage and the live meter |
| `/skills` | List available skills |
| `/mcp` | Show MCP server status and tools |
| `/raw` | Toggle showing/collapsing reasoning content |
| `/undo` | Restore code and/or conversation to an earlier point |
| `/exit` | Quit |
| `/help` | Show help |

Any custom skill is also available as `/<skill-name>` — see
[Subagents & Skills](subagents-and-skills.md).

## Images

If your model supports vision, paste an image with `Ctrl+V` and ask about it.
With a text-only model, TigerLiteCode strips the image before sending and tells
you it couldn't be seen — switch models with `/model` to share images.

## The status bar

Keep an eye on the bar while you work:

- **Model & mode** — what's answering, and whether you're in `build` or `plan`.
- **Cache rate** — the share of tokens served from the provider cache. Higher is
  cheaper.
- **Cost** — running estimate for the session.
- **Context meter** — how full the conversation is relative to the
  auto-compaction threshold. See [Token Saving](token-saving.md).
