# Sessions

Every conversation is a **session**, stored locally in a SQLite database
(`~/.local/share/tigercli/tigercli.db`). Sessions remember the full transcript,
the model used, token counts, and cost — so you can stop, come back, branch, and
search later.

## Continuing where you left off

```bash
# Resume the very last session and add to it
tigerlitecode run --continue "now write tests for that change"

# Resume a specific session by ID
tigerlitecode run --session ses_abc123 "keep going"
```

Inside the TUI, use `/continue` for the active session or `/resume` to pick one
from a list.

## Listing & inspecting

```bash
tigerlitecode session list            # recent sessions
tigerlitecode session list -n 50      # show more
tigerlitecode session list --archived # show archived ones
tigerlitecode session resume ses_abc123  # print full details
```

## Searching

Full-text search across titles and message content:

```bash
tigerlitecode session search "token refresh"
tigerlitecode session search "auth" --project /path/to/repo -n 20
```

## Forking

Branch a session to explore an alternative without disturbing the original:

```bash
tigerlitecode session fork ses_abc123 --title "try a different approach"
# Fork from a specific point in the history:
tigerlitecode session fork ses_abc123 --at msg_xyz789
```

## Organizing

```bash
tigerlitecode session pin ses_abc123       # keep it at the top
tigerlitecode session pin ses_abc123 --unpin
tigerlitecode session archive ses_abc123   # hide from the default list
tigerlitecode session unarchive ses_abc123
tigerlitecode session delete ses_abc123    # remove permanently
```

## Exporting

Save a transcript to share or keep:

```bash
tigerlitecode session export ses_abc123 --format markdown -o session.md
tigerlitecode session export ses_abc123 --format json    -o session.json
```

## Titles

New sessions get an auto-generated title from your first prompt. Override it with
`--title` on `run`, or when forking.
