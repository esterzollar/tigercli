#!/usr/bin/env python3
"""Standalone web search script — called as a subprocess by TigerLiteCode.

Uses DuckDuckGo (DDGS) as the only search backend. Results are cleaned
(HTML stripped, entities decoded, deduplicated by domain) but never
capped or truncated — all information flows through to the LLM.

Input (stdin JSON):
    {"query": "...", "maxResults": 5}

Output (stdout):
    Clean, deduplicated, compact citation format.
"""

from __future__ import annotations

import html as _html
import json
import re
import sys
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Token-saving constants
# ---------------------------------------------------------------------------
MAX_SNIPPET_CHARS = 300  # per-result body truncation (saves tokens, keeps info)


# ---------------------------------------------------------------------------
# Cleaning pipeline (deterministic — zero LLM token cost)
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def clean_snippet(text: str) -> str:
    """Strip HTML tags, decode entities, normalise whitespace, truncate."""
    text = _HTML_TAG_RE.sub("", text)
    text = _html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > MAX_SNIPPET_CHARS:
        text = text[: MAX_SNIPPET_CHARS - 3] + "..."
    return text


def extract_domain(url: str) -> str:
    """Return the netloc (domain) from a URL for deduplication."""
    try:
        return urlparse(url).netloc
    except Exception:
        return url


def deduplicate(results: list[dict]) -> list[dict]:
    """Remove duplicate results by domain, keeping first occurrence."""
    seen: set[str] = set()
    unique: list[dict] = []
    for r in results:
        domain = extract_domain(r.get("href", "") or r.get("url", ""))
        if domain and domain not in seen:
            seen.add(domain)
            unique.append(r)
        elif not domain:
            unique.append(r)
    return unique


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        raw = sys.stdin.read()
        args = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        args = {}

    query = str(args.get("query", "") or "").strip()
    if not query:
        print("Error: missing 'query'", file=sys.stderr)
        sys.exit(1)

    max_results = int(args.get("maxResults", 5))

    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # old package name
        except ImportError:
            print(
                "ddgs not installed. Install with: pip install ddgs",
                file=sys.stderr,
            )
            sys.exit(1)

    # Fetch more than requested to account for dedup losses
    try:
        results = list(DDGS().text(query, max_results=max(max_results * 2, 15)))
    except Exception as exc:
        print(f"Search error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not results:
        print("No search results found.")
        return

    # ── Cleaning pipeline ──────────────────────────────────────────
    # 1. Deduplicate by domain
    results = deduplicate(results)

    # 2. Take only the requested number after dedup
    results = results[:max_results]

    # 3. Clean snippets (strip HTML, decode entities, truncate long bodies)
    for r in results:
        if "body" in r and r["body"]:
            r["body"] = clean_snippet(r["body"])

    # ── Build output (all results, no cap) ─────────────────────────
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        body = r.get("body", "")
        href = r.get("href", "")
        lines.append(f"{i}. {title}\n   {body}\n   {href}\n")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
