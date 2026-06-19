# Token Saving — the "Lite" in TigerLiteCode

This is the feature that gives the project its name. Tiger Lite is engineered to
get the most out of every token, mostly by being disciplined about *how* it
talks to the model rather than by clever tricks. Here's the whole story.

## 1. Cache-first message ordering

Modern providers cache the **prefix** of a request. If two requests start with
the exact same tokens, the provider can skip recomputing that shared part and
charge you a small fraction of the price for it (often ~90–99% less for the
cached portion).

The catch: the prefix has to match *byte for byte*. Reorder a single message,
inject a timestamp, or shuffle your tool list and the cache misses.

TigerLiteCode lays out every request in a stable, deterministic order:

```
[ system prompt ] [ AGENTS.md / TIGER.md ] [ file context ] [ conversation history ] [ new message ]
```

Because the early, expensive parts (system prompt, project instructions, file
context) don't move between turns, the provider recognizes the prefix and
replays it from cache:

```
Turn 1:  [system][AGENTS.md][file:auth.ts]               ["fix the bug"]
Turn 2:  [system][AGENTS.md][file:auth.ts][history]       ["also add logging"]
         └──────────── identical, served from cache ─────┘
```

There's no cache layer to configure, no TTLs, and no invalidation logic. The
saving is a direct consequence of stable ordering.

## 2. Auto-compaction

A conversation that runs all afternoon would normally grow without bound — and
you'd pay to resend the whole thing every turn. Tiger Lite watches the token
count and, when the history crosses the **`compact_size`** threshold (500,000
tokens by default), it summarizes the older messages into a compact recap and
continues from there.

You stay in context; you stop paying for the full transcript.

Control it:

```text
/context                 # see how full the conversation is
/compact                 # compact right now
/compact "the auth work" # compact now, focused on a topic
/compact_size 300k       # lower the auto-compaction threshold
/compact_size 1m         # raise it
```

Or set it permanently in [configuration](configuration.md):

```bash
TIGERCLI_COMPACT_SIZE=300000
```

## 3. Focused subagents & skills

When a task needs a lot of exploration ("read these 40 files and find the bug"),
doing it in your main thread would stuff all that noise into your context — and
keep paying for it on every later turn.

Instead, hand it to a **subagent** via the `task` tool, or run a **skill** with
`context: fork`. The heavy work happens in a separate, throwaway context and only
a short summary comes back. Your main conversation stays small and cheap.

See [Subagents & Skills](subagents-and-skills.md).

## 4. You can always see the bill

The status bar shows a live **cache-hit rate** and a running **cost** estimate,
and `tigerlitecode stats` gives you the totals across all sessions:

```bash
tigerlitecode stats
```

If your cache rate is low, something is breaking the prefix — usually a model
switch mid-session or unstable file context. Keeping the model fixed within a
session keeps the cache warm.

## Practical tips

- **Stick with one model per session.** Switching providers/models invalidates
  the cache prefix.
- **Put stable instructions in `AGENTS.md`/`TIGER.md`.** They sit early in the
  prefix and get cached across every turn.
- **Use `plan` mode for exploration.** It proposes instead of acting, which
  avoids large unintended tool outputs landing in your context.
- **Lower `compact_size` on small-context models** so compaction kicks in before
  you hit the wall.
