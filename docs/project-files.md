# Project Files

TigerLiteCode looks for a few special files to learn about your project and to
keep its own state. Knowing them helps you steer the agent and keep your repo
clean.

## Instruction files: `AGENTS.md` and `TIGER.md`

These are plain-Markdown files that get injected near the **top** of every
request, so the agent always knows your conventions — and because they sit early
in the prefix, they're cached on every turn (see [Token Saving](token-saving.md)).

Put project rules here: build commands, code style, architecture notes, "always
run the tests with X", "never touch `generated/`", and so on.

**Load order (all that exist are combined):**

1. `./AGENTS.md` — project instructions (the common one).
2. `./TIGER.md` — TigerLiteCode-specific overrides for this project.
3. `~/.config/tigercli/AGENTS.md` — your global instructions.
4. `~/.tigercli/AGENTS.md` — legacy global location.

Create a starter file with:

```bash
tigerlitecode init      # writes ./AGENTS.md
# or inside the TUI:
/init
```

Example `AGENTS.md`:

```markdown
# Project: Acme API

## Commands
- Test: `pytest -q`
- Lint: `ruff check .`

## Conventions
- Type hints on all public functions.
- Never edit files under `migrations/` by hand.

## Architecture
- `app/api/` — HTTP routes
- `app/core/` — business logic
- `app/db/` — models and queries
```

## State directory: `.tigercli/`

When you set a project-local provider, model, or key, TigerLiteCode writes them
to `./.tigercli/`:

- `./.tigercli/config.json` — project defaults (provider, model, recent models).
- `./.tigercli/auth.json` — project-scoped API keys.

> These contain secrets and machine-specific state. They're already in
> `.gitignore`. **Do not commit them.** Global equivalents live under
> `~/.config/tigercli/` — see [Configuration](configuration.md).

## Extension directories

- `.agents/` — project subagents (`<name>.md`).
- `.skills/` — project skills (`<name>/SKILL.md`).

These *are* meant to be committed — they're part of your project's shared
tooling. See [Subagents & Skills](subagents-and-skills.md).

## What to commit vs. ignore

| Path | Commit? |
|------|---------|
| `AGENTS.md`, `TIGER.md` | ✅ Yes — shared project knowledge |
| `.agents/`, `.skills/` | ✅ Yes — shared tooling |
| `.tigercli/` | ❌ No — secrets & local state |
| `auth.json`, `config.json` (anywhere) | ❌ No — secrets |
| `*.db` | ❌ No — your session history |
