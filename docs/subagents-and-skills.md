# Subagents & Skills

Two ways to teach TigerLiteCode new tricks for your project: **subagents**
(specialized helpers the agent can delegate to) and **skills** (reusable
workflows you trigger by name). Both keep your main conversation lean — see
[Token Saving](token-saving.md).

---

## Subagents

A subagent is a focused assistant with its own system prompt, tool set, and
optionally its own model. The main agent calls one through the `task` tool when
a job is better handled in isolation (deep exploration, code review, etc.). The
subagent works in a separate context and returns a short result.

### Built-in subagents

- **`explore`** — read-only investigation of a codebase.
- **`general`** — a general-purpose helper with the full tool set.

### Project subagents

Drop a Markdown file in `.agents/<name>.md` with YAML frontmatter:

```markdown
---
name: reviewer
description: Review code for correctness, bugs, and regressions.
tools: read, glob, grep
provider: opencode-go
model: google/gemini-2.5-flash
---
You are a strict code reviewer. Inspect the requested changes and report
findings first, each with a file path and line number. Be concise and specific.
```

| Field | Required | Meaning |
|-------|----------|---------|
| `name` | yes | The `subagent_type` used to invoke it |
| `description` | yes | When the main agent should use it |
| `tools` | no | Comma-separated allow-list (defaults to a safe set) |
| `provider` | no | Override the provider for this subagent |
| `model` | no | Override the model for this subagent |

The main agent can now delegate with the `task` tool using
`subagent_type: "reviewer"`. A `provider`/`model` can also be overridden per
call.

---

## Skills

A skill is a saved workflow — a prompt template plus metadata — that you trigger
directly. Great for repeatable chores: "review these changes", "write a
conventional commit", "add tests for this module".

### Where skills live

- Project: `.skills/<skill-name>/SKILL.md`
- User: `~/.config/tigercli/skills/<skill-name>/SKILL.md`

### Anatomy of a skill

```markdown
---
name: code-review
description: Review the current changes for bugs and regressions.
tools: read, glob, grep
context: fork
agent: reviewer
---
Review the following changes and report issues with file:line references.

Focus area: $ARGUMENTS
```

| Field | Meaning |
|-------|---------|
| `name` | Invocation name (`/code-review`) |
| `description` | Shown in `/skills` and used by the model |
| `tools` | Tool allow-list for the skill |
| `context` | `fork` runs it in a subagent (isolated, cheap); omit to run inline |
| `agent` | Which subagent to run it as when forked |
| `provider` / `model` | Optional model override |

`$ARGUMENTS` is replaced with whatever you pass when invoking the skill.

### Running a skill

```text
/code-review the changes in payments.py
```

Or list what's available with `/skills`. The model can also invoke a skill on
its own through the `skill` tool when it decides the workflow fits.

> Use `context: fork` for anything that reads a lot of files. The exploration
> stays out of your main conversation, so you only pay for the summary it
> returns.
