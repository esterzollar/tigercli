"""TigerLiteCode — TUI-first CLI coding agent.

Port of deepcode-cli/src/cli.tsx to Python.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from tigercli import __version__

PACKAGE_NAME = "tigercli"


def _print_version() -> None:
    sys.stdout.write(f"{__version__}\n")
    sys.exit(0)


def _print_help() -> None:
    sys.stdout.write(
        "\n".join([
            f"{PACKAGE_NAME} - TigerLiteCode CLI",
            "",
            "Usage:",
            f"  {PACKAGE_NAME}                              Launch the interactive TUI in the current directory",
            f"  {PACKAGE_NAME} -p <prompt>                  Launch with a pre-filled prompt",
            f"  {PACKAGE_NAME} --prompt <prompt>            Same as -p",
            f"  {PACKAGE_NAME} run <prompt>                 One-shot non-interactive",
            f"  {PACKAGE_NAME} serve                        Start API server",
            f"  {PACKAGE_NAME} session list                 List sessions",
            f"  {PACKAGE_NAME} session resume <id>          Show session info",
            f"  {PACKAGE_NAME} session delete <id>          Delete a session",
            f"  {PACKAGE_NAME} stats                        Usage statistics",
            f"  {PACKAGE_NAME} init                         Create AGENTS.md",
            f"  {PACKAGE_NAME} --version                    Print the version",
            f"  {PACKAGE_NAME} --help                       Show this help",
            "",
            "Configuration:",
            "  ~/.tigercli/settings.json    User-level API key, model, base URL",
            "  ./.tigercli/settings.json    Project-level settings",
            "  ./.tigercli/skills/*/SKILL.md Project-level native skills",
            "",
            "Inside the TUI:",
            "  enter            Send the prompt",
            "  shift+enter      Insert a newline",
            "  home/end         Move within the current line",
            "  alt+left/right   Move by word",
            "  ctrl+w           Delete the previous word",
            "  ctrl+v           Paste an image from the clipboard",
            "  ctrl+x           Clear pasted images",
            "  esc              Interrupt the current model turn",
            "  /                Open the skills/commands menu",
            "  /skills          List available skills",
            "  /model           Select model, thinking mode and effort control",
            "  /new             Start a fresh conversation",
            "  /init            Initialize an AGENTS.md file with instructions for LLM",
            "  /resume          Pick a previous conversation to continue",
            "  /continue        Continue the active conversation, or resume one if empty",
            "  /undo            Restore code and/or conversation to a previous point",
            "  /mcp             Show MCP server status and available tools",
            "  /raw             Toggle display mode for viewing or collapsing reasoning content",
            "  /exit            Quit",
            "  ctrl+d twice     Quit",
        ]) + "\n"
    )
    sys.exit(0)


def _extract_initial_prompt(argv: list[str]) -> str | None:
    for i, arg in enumerate(argv):
        if arg in ("-p", "--prompt") and i + 1 < len(argv):
            return argv[i + 1]
    return None


def cmd_tui(args: argparse.Namespace) -> None:
    """Launch the TypeScript TUI backed by the Python engine."""
    if not sys.stdin.isatty():
        sys.stderr.write(
            f"{PACKAGE_NAME} requires an interactive terminal (TTY). "
            "Re-run from a real terminal session.\n"
        )
        sys.exit(1)

    try:
        from tigercli.tui.bridge import run_tui

        asyncio.run(run_tui())
    except KeyboardInterrupt:
        pass  # Ctrl+C — exit silently
    except ImportError as e:
        print(f"TUI bridge error: {e}")
        print("Install/build the TypeScript TUI with: cd tui-ts && npm install && npm run build")
        sys.exit(1)
    except FileNotFoundError:
        print("TypeScript TUI package not found.")
        print("Expected ./tui-ts/package.json")
        sys.exit(1)


def cmd_run(args: argparse.Namespace) -> None:
    """Non-interactive: send prompt, get response."""
    from tigercli.config import settings
    from tigercli.session.store import SessionStore
    from tigercli.agent.loop import AgentLoop

    asyncio.run(_run(args, settings, SessionStore, AgentLoop))


async def _run(
    args: argparse.Namespace,
    settings: Any,
    SessionStore: Any,
    AgentLoop: Any,
) -> None:
    store = SessionStore()

    if args.session:
        session = await store.get_session(args.session)
    elif args.continue_last:
        sessions = await store.list_sessions(limit=1)
        session = sessions[0] if sessions else None
    else:
        session = None

    if not session:
        project_path = args.project or str(Path.cwd())
        session = await store.create_session(
            project_path=project_path,
            title=args.title,
            provider=args.provider or settings.default_provider,
            model=args.model or settings.default_model,
            thinking_enabled=args.thinking,
            reasoning_effort=args.effort,
            mode=args.mode or settings.default_mode,
        )

    files: list[tuple[str, str]] = []
    if args.files:
        for fp in args.files:
            p = Path(fp).expanduser().resolve()
            if p.exists():
                files.append((str(p), p.read_text()))
            else:
                print(f"Warning: file not found: {fp}", file=sys.stderr)

    loop = AgentLoop(store)
    response = await loop.run(
        session=session,
        user_message=" ".join(args.prompt),
        files=files or None,
        auto_approve=args.yes,
    )

    if args.json:
        import json
        print(json.dumps({"session_id": session.id, "response": response}))
    else:
        print(response)

    await store.close()


def cmd_serve(args: argparse.Namespace) -> None:
    """Start HTTP API server (optional web interface)."""
    from tigercli.config import settings
    try:
        import uvicorn
        from tigercli.api.routes import create_app

        host = args.host or settings.server_host
        port = args.port or settings.server_port
        app = create_app()
        print(f"TigerLiteCode server \u2192 http://{host}:{port}")
        uvicorn.run(app, host=host, port=port, log_level="info")
    except ImportError:
        print("Web server requires: pip install fastapi uvicorn")
        sys.exit(1)


def cmd_session(args: argparse.Namespace) -> None:
    """Session management."""
    asyncio.run(_session(args))


async def _session(args: argparse.Namespace) -> None:
    from tigercli.session.store import SessionStore
    store = SessionStore()

    cmd = args.session_command

    try:
        if cmd == "list":
            status = "archived" if getattr(args, "archived", False) else "active"
            sessions = await store.list_sessions(limit=args.limit or 20, status=status)
            if not sessions:
                print("No sessions.")
            else:
                print(f"{'ID':<24} {'Title':<30} {'Model':<24} {'Provider':<12} {'Updated'}")
                print("-" * 100)
                for s in sessions:
                    pin = "📌" if s.pinned else " "
                    print(f"{pin} {s.id:<22} {(s.title or '')[:28]:<30} {s.model:<24} {s.provider:<12} {s.updated_at[:19]}")

        elif cmd == "resume":
            session = await store.get_session(args.session_id)
            if not session:
                print(f"Session not found: {args.session_id}")
            else:
                print(f"Session: {session.id}")
                print(f"  Title: {session.title}")
                print(f"  Project: {session.project_path}")
                print(f"  Provider: {session.provider} / {session.model}")
                print(f"  Mode: {session.mode}")
                print(f"  Cache rate: {session.cache_hit_rate:.0%}")
                print(f"  Tokens: {session.total_tokens_in:,} in / {session.total_tokens_out:,} out")
                print(f"  Cache (hit/miss/create): {session.total_cache_hit_tokens:,} / {session.total_cache_miss_tokens:,} / {session.total_cache_creation_tokens:,}")
                print(f"  Cost: ${session.total_cost_usd:.4f}")

        elif cmd == "delete":
            await store.delete_session(args.session_id)
            print(f"Session {args.session_id} deleted.")

        elif cmd == "search":
            sessions = await store.search_sessions(
                query=args.query or "",
                project_path=args.project,
                limit=args.limit or 20,
            )
            if not sessions:
                print("No matches.")
            else:
                for s in sessions:
                    print(f"{s.id}  {(s.title or '')[:40]}  ({s.project_path})  ${s.total_cost_usd:.4f}")

        elif cmd == "fork":
            new = await store.fork_session(args.session_id, at_message_id=args.at, title=args.title)
            if new is None:
                print(f"Source session not found: {args.session_id}")
            else:
                print(f"Forked → {new.id}  ({new.title})")

        elif cmd == "archive":
            ok = await store.archive_session(args.session_id)
            print(f"Archived {args.session_id}" if ok else "Session not found")

        elif cmd == "unarchive":
            ok = await store.unarchive_session(args.session_id)
            print(f"Unarchived {args.session_id}" if ok else "Session not found")

        elif cmd == "pin":
            ok = await store.pin_session(args.session_id, pinned=not getattr(args, "unpin", False))
            print(f"{'Pinned' if not getattr(args, 'unpin', False) else 'Unpinned'} {args.session_id}" if ok else "Session not found")

        elif cmd == "export":
            try:
                content = await store.export_session(args.session_id, fmt=args.format or "markdown")
            except ValueError as e:
                print(f"Error: {e}")
                return
            if args.output:
                Path(args.output).write_text(content)
                print(f"Wrote {args.output}")
            else:
                print(content)
    finally:
        await store.close()


def cmd_stats(args: argparse.Namespace) -> None:
    """Show usage statistics."""
    asyncio.run(_stats(args))


async def _stats(args: argparse.Namespace) -> None:
    from tigercli.session.store import SessionStore
    store = SessionStore()
    try:
        stats = await store.get_total_stats()
        sessions = await store.list_sessions(limit=50)

        print()
        print(f"{'='*60}")
        print(f"  TigerLiteCode Usage Statistics")
        print(f"{'='*60}")
        print(f"  Total requests:   {stats.get('requests', 0):,}")
        print(f"  Total tokens in:  {stats.get('total_in', 0):,}")
        print(f"  Total tokens out: {stats.get('total_out', 0):,}")
        print(f"  Cache hit tokens: {stats.get('cache_hit', 0):,}")
        print(f"  Cache miss tokens:{stats.get('cache_miss', 0):,}")
        hit = stats.get("cache_hit", 0)
        miss = stats.get("cache_miss", 0)
        rate = hit / (hit + miss) * 100 if (hit + miss) > 0 else 0
        print(f"  Cache hit rate:   {rate:.1f}%")
        print(f"  Total cost (est): ${stats.get('cost', 0):.4f}")
        print(f"{'='*60}")

        if sessions:
            print(f"\n  Sessions ({len(sessions)}):")
            for s in sessions[:10]:
                print(f"    {s.id}  {s.title or '(untitled)':20s}  ${s.total_cost_usd:.4f}")
    finally:
        await store.close()


def cmd_init(args: argparse.Namespace) -> None:
    """Initialize a project \u2014 create AGENTS.md if missing."""
    project = Path(args.project or Path.cwd())
    agents_md = project / "AGENTS.md"
    if agents_md.exists():
        print(f"AGENTS.md already exists at {agents_md}")
        return

    content = f"""# {project.name}

## Build & Run
Describe how to build and run the project.

## Conventions
- Coding conventions used in this project.

## Notes for AI
- Be concise with code output
- Follow existing patterns
"""
    agents_md.write_text(content)
    print(f"Created {agents_md}")


def main() -> None:
    argv = sys.argv[1:]

    # Port of cli.tsx: --version / -v
    if "--version" in argv or "-v" in argv:
        _print_version()

    # Port of cli.tsx: --help / -h (only at top level, not for subcommands)
    _SUBCOMMANDS = {"run", "serve", "session", "stats", "init"}
    if ("--help" in argv or "-h" in argv) and not (argv and argv[0] in _SUBCOMMANDS):
        _print_help()

    # Port of cli.tsx: extract initial prompt from -p/--prompt
    initial_prompt = _extract_initial_prompt(argv)

    parser = argparse.ArgumentParser(
        prog=PACKAGE_NAME,
        description="TUI-first CLI coding agent with DeepSeek & OpenCode",
        add_help=False,
    )

    parser.set_defaults(func=cmd_tui)

    sub = parser.add_subparsers(dest="command")

    # \u2014\u2014 run \u2014\u2014
    run_p = sub.add_parser("run", help="Send a prompt (non-interactive)")
    run_p.add_argument("prompt", nargs="+", help="The prompt to send")
    run_p.add_argument("--session", "-s", help="Session ID to use")
    run_p.add_argument("--continue", dest="continue_last", action="store_true", help="Continue last session")
    run_p.add_argument("--provider", "-p", help="Provider: deepseek | opencode")
    run_p.add_argument("--model", "-m", help="Model ID")
    run_p.add_argument("--mode", help="Mode: build | plan")
    run_p.add_argument("--thinking", action="store_true", help="Enable thinking mode")
    run_p.add_argument("--effort", default="high", help="Reasoning effort: high | max")
    run_p.add_argument("--files", "-f", nargs="*", help="Files to include as context")
    run_p.add_argument("--project", "-d", help="Project directory")
    run_p.add_argument("--title", help="Session title")
    run_p.add_argument("--yes", "-y", action="store_true", help="Auto-approve all permissions")
    run_p.add_argument("--json", action="store_true", help="Output as JSON")
    run_p.set_defaults(func=cmd_run)

    # \u2014\u2014 serve \u2014\u2014
    serve_p = sub.add_parser("serve", help="Start HTTP API server (optional)")
    serve_p.add_argument("--host", help="Host to bind")
    serve_p.add_argument("--port", type=int, help="Port to listen on")
    serve_p.set_defaults(func=cmd_serve)

    # \u2014\u2014 session \u2014\u2014
    sess_p = sub.add_parser("session", help="Session management")
    sess_sub = sess_p.add_subparsers(dest="session_command")

    list_p = sess_sub.add_parser("list", help="List sessions")
    list_p.add_argument("--limit", "-n", type=int, help="Max sessions to show")
    list_p.add_argument("--archived", action="store_true", help="Show archived sessions")
    list_p.set_defaults(func=cmd_session)

    resume_p = sess_sub.add_parser("resume", help="Show session info")
    resume_p.add_argument("session_id", help="Session ID")
    resume_p.set_defaults(func=cmd_session)

    delete_p = sess_sub.add_parser("delete", help="Delete a session")
    delete_p.add_argument("session_id", help="Session ID")
    delete_p.set_defaults(func=cmd_session)

    search_p = sess_sub.add_parser("search", help="Search sessions by title or message text")
    search_p.add_argument("query", nargs="?", default="", help="Search query (FTS5)")
    search_p.add_argument("--project", "-d", help="Filter by project path")
    search_p.add_argument("--limit", "-n", type=int, help="Max results")
    search_p.set_defaults(func=cmd_session)

    fork_p = sess_sub.add_parser("fork", help="Branch a session at a specific message")
    fork_p.add_argument("session_id", help="Source session ID")
    fork_p.add_argument("--at", help="Message ID to branch at (default: tail)")
    fork_p.add_argument("--title", help="Title for the new fork")
    fork_p.set_defaults(func=cmd_session)

    archive_p = sess_sub.add_parser("archive", help="Move a session to archive")
    archive_p.add_argument("session_id", help="Session ID")
    archive_p.set_defaults(func=cmd_session)

    unarchive_p = sess_sub.add_parser("unarchive", help="Restore an archived session")
    unarchive_p.add_argument("session_id", help="Session ID")
    unarchive_p.set_defaults(func=cmd_session)

    pin_p = sess_sub.add_parser("pin", help="Pin (or unpin) a session to the top of the list")
    pin_p.add_argument("session_id", help="Session ID")
    pin_p.add_argument("--unpin", action="store_true", help="Remove the pin instead")
    pin_p.set_defaults(func=cmd_session)

    export_p = sess_sub.add_parser("export", help="Export a session to markdown or json")
    export_p.add_argument("session_id", help="Session ID")
    export_p.add_argument("--format", "-f", choices=["markdown", "json"], help="Output format")
    export_p.add_argument("--output", "-o", help="Write to file (default: stdout)")
    export_p.set_defaults(func=cmd_session)

    # \u2014\u2014 stats \u2014\u2014
    stats_p = sub.add_parser("stats", help="Usage statistics")
    stats_p.set_defaults(func=cmd_stats)

    # \u2014\u2014 init \u2014\u2014
    init_p = sub.add_parser("init", help="Initialize project (create AGENTS.md)")
    init_p.add_argument("--project", "-d", help="Project directory")
    init_p.set_defaults(func=cmd_init)

    # Port of cli.tsx: TTY check for TUI mode
    args = parser.parse_args(argv)

    # If no subcommand matched, func defaults to cmd_tui
    args.func(args)


if __name__ == "__main__":
    main()
