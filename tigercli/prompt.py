"""System prompt generation and tool definitions.

Port of deepcode-cli/src/prompt.ts with embedded template strings.
Replaces deepcode/DEEPCODE with tigercli/TIGERCLI.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict


# =========================================================================
# Embedded tool template constants  (from templates/tools/*.md / *.md.ejs)
# =========================================================================

TOOL_ASK_USER_QUESTION = """\
## AskUserQuestion

Use this tool when you need to ask the user questions during execution. This allows you to:
1. Gather user preferences or requirements
2. Clarify ambiguous instructions
3. Get decisions on implementation choices as you work
4. Offer choices to the user about what direction to take.

Usage notes:
- Users will always be able to select "Other" to provide custom text input
- Use multiSelect: true to allow multiple answers to be selected for a question
- If you recommend a specific option, make that the first option in the list and add "(Recommended)" at the end of the label"""

TOOL_BASH = """\
## Bash

Executes a given bash command. Working directory persists between commands; shell state (everything else) does not. The shell environment is initialized from the user'\''s profile (bash or zsh).

On Windows, Bash runs through Git Bash. Use POSIX commands and quote Windows paths carefully.

IMPORTANT: This tool is for terminal operations like git, npm, docker, etc. DO NOT use it for file operations (reading, writing, editing, searching, finding files) - use the specialized tools for this instead.

IMPORTANT: Before reaching for generic shell pipelines, prefer purpose-built CLI tools when they make the task more accurate, safer, faster, or easier to understand:
- Use `ripgrep` (`rg`) when you need to search file contents by text or regex across the workspace; prefer it over slower tools like `grep`.
- Use `jq` when you need to inspect, filter, or transform JSON output; prefer it over ad-hoc parsing with `sed`, `awk`, or Python one-liners.

Before executing the command, please follow these steps:

1. Directory Verification:
   - If the command will create new directories or files, first use `ls` to verify the parent directory exists and is the correct location
   - For example, before running "mkdir foo/bar", first use `ls foo` to check that "foo" exists and is the intended parent directory

2. Command Execution:
   - Always quote file paths that contain spaces with double quotes (e.g., cd "path with spaces/file.txt")
   - Examples of proper quoting:
     - cd "/Users/name/My Documents" (correct)
     - cd /Users/name/My Documents (incorrect - will fail)
     - python "/path/with spaces/script.py" (correct)
     - python /path/with spaces/script.py (incorrect - will fail)
   - After ensuring proper quoting, execute the command.
   - Capture the output of the command.

Usage notes:
  - The command argument is required.
  - The sideEffects argument is required. Declare the minimum permission scopes the command may need.
  - You can use `run_in_background: true` to run a command in the background. Only use this if you need to perform a blocking task, like running a server for the upcoming test scripts.
  - When using `run_in_background`, do NOT add `&` to the command. Output is written to a log file.
  - Before your final response, stop background tasks that has not reported a completed state, unless the user explicitly asks to keep it running.
  - To stop a background command, use the `stopCommand` returned in the tool result metadata.
  - Use `sideEffects: []` only for commands that do not read, write, delete, query Git history, mutate Git history, or access the network, such as `date` or `node --version`.
  - Use `*-out-cwd` when the command accesses paths outside the current workspace. For example, `cat /etc/hosts` requires `["read-out-cwd"]`.
  - Use `query-git-log` for commands such as `git log`, `git show HEAD`, `git blame`, or history diffs. Use `mutate-git-log` for commands such as `git commit`, `git reset`, `git rebase`, `git merge`, `git cherry-pick`, or `git tag`.
  - Use `["unknown"]` when you cannot classify the command safely.
  - It is very helpful if you write a clear, concise description of what this command does. For simple commands, keep it brief (5-10 words). For complex commands (piped commands, obscure flags, or anything hard to understand at a glance), add enough context to clarify what it does.
  - If the output exceeds 30000 characters, output will be truncated before being returned to you.
  - Always prefer using the dedicated tools for these commands:
    - Read files: Use Read (NOT cat/head/tail)
    - Edit files: Use Edit (NOT sed/awk)
    - Write files: Use Write (NOT echo >/cat <<EOF)
    - Communication: Output text directly (NOT echo/printf)
  - When issuing multiple commands:
    - If the commands are independent and can run in parallel, make multiple Bash tool calls in a single message. For example, if you need to run "git status" and "git diff", send a single message with two Bash tool calls in parallel.
    - If the commands depend on each other and must run sequentially, use a single Bash call with '\''&&'\'' to chain them together (e.g., `git add . && git commit -m "message" && git push`). For instance, if one operation must complete before another starts (like mkdir before cp, Write before Bash for git operations, or git add before git commit), run these operations sequentially instead.
    - Use '\'';'\'' only when you need to run commands sequentially but don'\''t care if earlier commands fail
    - DO NOT use newlines to separate commands (newlines are ok in quoted strings)
  - Try to maintain your current working directory throughout the session by using absolute paths and avoiding usage of `cd`. You may use `cd` if the User explicitly requests it.
    <good-example>
    pytest /foo/bar/tests
    </good-example>
    <bad-example>
    cd /foo/bar && pytest tests
    </bad-example>

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "command": {
      "description": "The command to execute",
      "type": "string"
    },
    "description": {
      "description": "Clear, concise description of what this command does in active voice. Never use words like \"complex\" or \"risk\" in the description - just describe what it does.\n\nFor simple commands (git, npm, standard CLI tools), keep it brief (5-10 words):\n- ls \u2192 \"List files in current directory\"\n- git status \u2192 \"Show working tree status\"\n- npm install \u2192 \"Install package dependencies\"\n\nFor commands that are harder to parse at a glance (piped commands, obscure flags, etc.), add enough context to clarify what it does:\n- find . -name \"*.tmp\" -exec rm {} \\; \u2192 \"Find and delete all .tmp files recursively\"\n- git reset --hard origin/main \u2192 \"Discard all local changes and match remote main\"\n- curl -s url | jq '\''.data[]'\'' \u2192 \"Fetch JSON from URL and extract data array elements\"",
      "type": "string"
    },
    "sideEffects": {
      "description": "Permission scopes required by this bash command. Use [] only for commands that do not read, write, delete, or access the network. Use [\"unknown\"] when the effects cannot be classified safely.",
      "type": "array",
      "items": {
        "type": "string",
        "enum": [
          "read-in-cwd",
          "read-out-cwd",
          "write-in-cwd",
          "write-out-cwd",
          "delete-in-cwd",
          "delete-out-cwd",
          "query-git-log",
          "mutate-git-log",
          "network",
          "unknown"
        ]
      },
      "uniqueItems": true
    },
    "run_in_background": {
      "description": "Set to true to run the command in the background. Use this only when you do not need the result immediately and can wait for a completion notification.",
      "type": "boolean"
    }
  },
  "required": [
    "command",
    "sideEffects"
  ],
  "additionalProperties": false
}
```"""

TOOL_EDIT = """\
## Edit

Performs scoped string replacements in files.

Usage:
- You must use `Read` tool at least once in the conversation before editing to get the required `snippet_id`. This tool will error if you attempt an edit without reading the file.
- `snippet_id` defines the search scope. Provide `file_path` only as an optional guard that the snippet belongs to the expected file.
- When editing text from Read tool output, ensure you preserve the exact indentation (tabs/spaces) as it appears AFTER the line number prefix. The line number prefix format is: spaces + line number + tab. Everything after that tab is the actual file content to match. Never include any part of the line number prefix in the old_string or new_string.
- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.
- Only use emojis if the user explicitly requests it. Avoid adding emojis to files unless asked.
- If `old_string` is not unique, the tool returns candidate matches with line ranges, previews, and snippet ids that you can reuse in a follow-up edit.
- If `old_string` is not found, the tool returns the closest likely match in metadata, including a preview. If the only difference is escaping and there is a unique loose-escape match, the tool may use the configured model to correct `old_string` and `new_string` before retrying.
- `replace_all` has safety checks. For broad or short-fragment replacements, provide `expected_occurrences` so the tool can verify the exact number of matches before editing.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "snippet_id": {
      "description": "Required snippet_id returned by Read or a prior Edit error response.",
      "type": "string"
    },
    "file_path": {
      "description": "Optional absolute path guard. If provided, it must match the snippet'\''s file.",
      "type": "string"
    },
    "old_string": {
      "description": "The text to replace within the snippet_id scope",
      "type": "string"
    },
    "new_string": {
      "description": "The text to replace it with (must be different from old_string)",
      "type": "string"
    },
    "replace_all": {
      "description": "Replace all occurences of old_string (default false)",
      "default": false,
      "type": "boolean"
    },
    "expected_occurrences": {
      "description": "Expected number of matches. Useful as a guardrail for replace_all.",
      "type": "number"
    }
  },
  "required": [
    "snippet_id",
    "old_string",
    "new_string"
  ],
  "additionalProperties": false
}
```"""

TOOL_READ_MD = """\
## Read

Reads a file from the local filesystem. You can access any file directly by using this tool.
Assume this tool is able to read all files on the machine. If the User provides a path to a file assume that path is valid. It is okay to read a file that does not exist; an error will be returned.

Usage:
- The file_path parameter must be a UNIX-style file path.
- By default, it reads up to 2000 lines starting from the beginning of the file
- You can optionally specify a line offset and limit (especially handy for long files), but it'\''s recommended to read the whole file by not providing these parameters
- Any lines longer than 2000 characters will be truncated
- Results are returned using cat -n format, with line numbers starting at 1
- Text reads return a snippet id for Edit: full-file reads use ids like `full_file_0`; partial reads use ids like `snippet_1`.
__MULTIMODAL_LINE__
- This tool can read PDF files (.pdf). For large PDFs (more than 10 pages), you MUST provide the pages parameter to read specific page ranges (e.g., pages: "1-5"). Reading a large PDF without the pages parameter will fail. Maximum 20 pages per request.
- This tool can read Jupyter notebooks (.ipynb files) and returns all cells with their outputs, combining code, text, and visualizations.
- This tool can only read files, not directories. To read a directory, use an ls command via the Bash tool.
- You can call multiple tools in a single response. It is always better to speculatively read multiple potentially useful files in parallel.
- You will regularly be asked to read screenshots. If the user provides a path to a screenshot, ALWAYS use this tool to view the file at the path. This tool will work with all temporary file paths.
- If you read a file that exists but has empty contents you will receive a system reminder warning in place of file contents.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "file_path": {
      "description": "The absolute path to the file to read",
      "type": "string"
    },
    "offset": {
      "description": "The line number to start reading from. Only provide if the file is too large to read at once",
      "type": "number"
    },
    "limit": {
      "description": "The number of lines to read. Only provide if the file is too large to read at once.",
      "type": "number"
    },
    "pages": {
      "description": "Page range for PDF files (e.g., \\"1-5\\", \\"3\\", \\"10-20\\"). Only applicable to PDF files. Maximum 20 pages per request.",
      "type": "string"
    }
  },
  "required": [
    "file_path"
  ],
  "additionalProperties": false
}
```"""

TOOL_UPDATE_PLAN = """\
## UpdatePlan

Updates the current task plan and progress display.

Usage:
- Use this tool for non-trivial multi-step tasks when a task list helps track execution progress.
- Pass the complete current task list every time. The latest call replaces the previous visible plan.
- The `plan` argument is a markdown string, not an array of step objects. Match the user's language: if they wrote in English, use English; if they wrote in another language, use that language.
- Keep exactly one task marked `[>]` while work is in progress.
- Update the plan before starting a task, immediately after completing a task, and whenever tasks are split, merged, reordered, blocked, or changed.
- Before executing the first task and after completing each task, re-evaluate the latest conversation and project context, then revise the remaining plan if needed.
- Remove tasks that are no longer relevant, and add newly discovered follow-up tasks before working on them.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "plan": {
      "description": "The complete markdown task list to display as the latest plan state.",
      "type": "string"
    },
    "explanation": {
      "description": "Optional short reason for changing the plan.",
      "type": "string"
    }
  },
  "required": [
    "plan"
  ],
  "additionalProperties": false
}
```"""

TOOL_WEB_SEARCH = """\
## WebSearch

Use this tool when you need up-to-date web information before writing code, changing dependencies, or citing external guidance.

JSON schema:

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "description": "A search query phrased as a clear, specific natural language question or statement that includes key context."
    }
  },
  "required": ["query"],
  "additionalProperties": false
}
```

Usage:
- Do not reduce `query` to space-separated keywords.

Typical use cases:
- Confirm recent SDK, framework, or API changes
- Check current compatibility, deprecations, or migration notes
- Look up active issue tracker discussions or recent regressions
- Gather cited sources before producing technical guidance"""

TOOL_WRITE = """\
## Write

Writes a file to the local filesystem.

Usage:
- This tool will overwrite the existing file if there is one at the provided path.
- If this is an existing file, you MUST read the full file first. A partial read is not enough for overwriting an existing file.
- `content` must be a single string. If you are writing JSON, serialize the full document to text before calling this tool.
- Prefer `Edit` for updating existing files. Use `Write` for new files or intentional full-file rewrites.
- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested by the User.
- NEVER proactively create one-off test script. Only create one-off test script files if explicitly requested by the User.
- Only use emojis if the user explicitly requests it. Avoid writing emojis to files unless asked.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "file_path": {
      "description": "The absolute path to the file to write (must be absolute, not relative)",
      "type": "string"
    },
    "content": {
      "description": "The complete file content as a string",
      "type": "string"
    }
  },
  "required": [
    "file_path",
    "content"
  ],
  "additionalProperties": false
}
```"""

# =========================================================================
# Embedded skill template  (from templates/skills/karpathy-guidelines.md)
# =========================================================================

SKILL_KARPATHY_GUIDELINES = """\
---
name: karpathy-guidelines
description: Behavioral guidelines to reduce common LLM coding mistakes. Use when writing, reviewing, or refactoring code to avoid overcomplication, make surgical changes, surface assumptions, and define verifiable success criteria.
---

# Karpathy Guidelines

Behavioral guidelines to reduce common LLM coding mistakes.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don'\''t assume. Don'\''t hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don'\''t pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what'\''s confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn'\''t requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don'\''t "improve" adjacent code, comments, or formatting.
- Don'\''t refactor things that aren'\''t broken.
- Match existing style, even if you'\''d do it differently.
- If you notice unrelated dead code, mention it - don'\''t delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don'\''t remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user'\''s request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" \u2192 "Write tests for invalid inputs, then make them pass"
- "Fix the bug" \u2192 "Write a test that reproduces it, then make it pass"
- "Refactor X" \u2192 "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] \u2192 verify: [check]
2. [Step] \u2192 verify: [check]
3. [Step] \u2192 verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification."""

# =========================================================================
# Embedded prompt template  (from templates/prompts/init_command.md.ejs)
# =========================================================================

INIT_COMMAND_PROMPT = """\
__INIT_OR_UPDATE__
Your goal is to produce a clear, concise, and well-structured document with descriptive headings and actionable explanations for each section.
Follow the outline below, but adapt as needed \u2014 add sections if relevant, and omit those that do not apply to this project.

Document Requirements

- Title the document "Repository Guidelines".
- Use Markdown headings (#, ##, etc.) for structure.
- Keep the document concise. 200-400 words is optimal.
- Keep explanations short, direct, and specific to this repository.
- Provide examples where helpful (commands, directory paths, naming patterns).
- Maintain a professional, instructional tone.

Recommended Sections

Project Structure & Module Organization

- Outline the project structure, including where the source code, tests, and assets are located.

Build, Test, and Development Commands

- List key commands for building, testing, and running locally (e.g., npm test, make build).
- Briefly explain what each command does.

Coding Style & Naming Conventions

- Specify indentation rules, language-specific style preferences, and naming patterns.
- Include any formatting or linting tools used.

Testing Guidelines

- Identify testing frameworks and coverage requirements.
- State test naming conventions and how to run tests.

Commit & Pull Request Guidelines

- Summarize commit message conventions found in the project'\''s Git history.
- Outline pull request requirements (descriptions, linked issues, screenshots, etc.).

(Optional) Add other sections if relevant, such as Security & Configuration Tips, Architecture Overview, or Agent-Specific Instructions."""

# =========================================================================
# Other base string constants
# =========================================================================

COMPACT_PROMPT_BASE = """\
Your task is to create a detailed summary of the conversation so far, paying close attention to the user'\''s explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you'\''ve covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user'\''s explicit requests and intents
   - Your approach to addressing the user'\''s requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly.

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user'\''s explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users'\'' feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user'\''s most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first.
                       If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there'\''s no drift in task interpretation.

Here'\''s an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]
   - [...]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Summary of the changes made to this file, if any]
      - [Important Code Snippet]
   - [File Name 2]
      - [Important Code Snippet]
   - [...]

4. Errors and fixes:
    - [Detailed description of error 1]:
      - [How you fixed the error]
      - [User feedback on the error if any]
    - [...]

5. Problem Solving:
   [Description of solved problems and ongoing troubleshooting]

6. All user messages:
    - [Detailed non tool use user message]
    - [...]

7. Pending Tasks:
   - [Task 1]
   - [Task 2]
   - [...]

8. Current Work:
   [Precise description of current work]

9. Optional Next Step:
   [Optional Next step to take]

</summary>"""

SYSTEM_PROMPT_BASE = """\
You are TigerLiteCode, an interactive CLI coding agent. You help users with software engineering tasks. Use the instructions below and the tools available to you to assist the user.

You are powered by a state-of-the-art LLM. Match the quality bar of a senior engineer — clear thinking, surgical edits, no fluff.

# Tone and style
- Be concise, direct, and technical. Avoid filler ("Sure!", "Great question!", "Let me…").
- Output is shown in a terminal: prefer plain text over markdown for short replies, and use markdown only when it genuinely helps (lists, tables, code).
- Only use emojis if the user explicitly requests them.
- NEVER fabricate URLs. If you reference a URL it must come from the user's input, a file you've read, or a domain you are certain exists.
- When in doubt, investigate first (read files, run commands) before answering.

# Professional objectivity
Prioritize technical accuracy over agreement. Disagree with the user — politely — when the evidence says they're wrong. Don't validate ideas that are mistaken just to be friendly.

# Tool use
- Prefer the dedicated tools (Read, Write, Edit, Glob, Grep, WebSearch, WebFetch) over shelling out via Bash.
- Use Bash for actual system operations: git, build/test commands, package managers.
- Run independent tool calls in parallel; chain only when one depends on another.
- ALWAYS prefer editing existing files over creating new ones. NEVER write a file that wasn't asked for, especially documentation (*.md, README) or scratch test scripts.

# Doing tasks
The user will mostly ask you to perform software engineering tasks. For these:
1. If the task is non-trivial (3+ steps), use the UpdatePlan tool to lay out a plan and keep it current.
2. Investigate before editing — Read the relevant files, Grep for usages.
3. Make surgical changes that map directly to the request.
4. Verify your work: run the test suite or relevant build command if one is available.
5. Report back briefly: what you changed and why, with `path:line` references the user can click."""

# =========================================================================
# Constants
# =========================================================================

DEFAULT_SKILL_TEMPLATES = ["karpathy-guidelines"]
DEFAULT_SKILL_RESOURCE_FILE_LIMIT = 50
SKILL_RESOURCE_EXCLUDED_DIRS = frozenset({
    ".cache", ".git", ".next", ".turbo",
    "build", "coverage", "dist", "node_modules", "out",
})

_TOOL_ENTRIES: list[tuple[str, str, bool]] = [
    ("ask-user-question.md", "TOOL_ASK_USER_QUESTION", False),
    ("bash.md", "TOOL_BASH", False),
    ("edit.md", "TOOL_EDIT", False),
    ("read.md", "TOOL_READ_MD", True),
    ("update-plan.md", "TOOL_UPDATE_PLAN", False),
    ("web-search.md", "TOOL_WEB_SEARCH", False),
    ("write.md", "TOOL_WRITE", False),
]

_TOOL_CONTENT_MAP: dict[str, str] = {
    "TOOL_ASK_USER_QUESTION": TOOL_ASK_USER_QUESTION,
    "TOOL_BASH": TOOL_BASH,
    "TOOL_EDIT": TOOL_EDIT,
    "TOOL_READ_MD": TOOL_READ_MD,
    "TOOL_UPDATE_PLAN": TOOL_UPDATE_PLAN,
    "TOOL_WEB_SEARCH": TOOL_WEB_SEARCH,
    "TOOL_WRITE": TOOL_WRITE,
}


# =========================================================================
# Type definitions
# =========================================================================

class ToolDefinition(TypedDict):
    type: str
    function: dict[str, Any]


class SkillPromptDocument(TypedDict):
    name: str
    content: str
    path: str | None
    skillFilePath: str | None


class SkillResourceListing(TypedDict):
    files: list[str]
    truncated: bool

# =========================================================================
# Helper functions
# =========================================================================


def _escape_xml(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _to_posix_path(file_path: str) -> str:
    return file_path.replace(os.sep, "/")


def _supports_multimodal(model: str) -> bool:
    model_lower = model.lower()
    multimodal_keywords = [
        "gpt-4o", "gpt-4-turbo", "claude-3-5-sonnet", "claude-3-opus",
        "claude-3-haiku", "gemini-1.5", "gemini-2.0", "deepseek-vl",
        "gpt-4-vision",
    ]
    return any(kw in model_lower for kw in multimodal_keywords)


def _check_tool_installed(tool: str) -> bool:
    return shutil.which(tool) is not None


def _get_uname_info() -> str:
    try:
        result = subprocess.run(
            ["uname", "-a"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return f"{platform.system()} {platform.release()} {platform.machine()}"


def _get_shell_path_info() -> str:
    return os.environ.get("SHELL", "/bin/bash")


def _get_runtime_version_info() -> dict[str, str]:
    versions: dict[str, str] = {}
    py_version = sys.version.split()[0]
    if py_version:
        versions["python3 version"] = py_version
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            node_ver = result.stdout.strip()
            if node_ver.startswith("v"):
                versions["node version"] = node_ver
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return versions


def _get_current_date_and_model_prompt(model: str | None = None) -> str:
    now = datetime.now()
    prompt = f"Today is {now.strftime('%Y-%m-%d')}. Time advances as the conversation continues."
    if model:
        prompt += f"\nThe current model is {model}. The user can switch models at any time with the /model command."
    return prompt


def _render_multimodal_line(model: str) -> str:
    if _supports_multimodal(model):
        return "- This tool allows you to read images (eg PNG, JPG, etc). When reading an image file the contents are presented visually as TigerLiteCode is a multimodal LLM."
    return "- This tool can inspect image files, but the current model is not multimodal, so image reads are not presented visually to the model."


def _strip_skill_prompt_metadata(content: str) -> str:
    match = re.match(r"^---\s*\n(.*?\n)---\s*\n", content, re.DOTALL)
    if match:
        frontmatter_text = match.group(1)
        body = content[match.end():]
        lines = frontmatter_text.split("\n")
        filtered_lines = [
            line for line in lines
            if not line.strip().startswith("metadata:")
        ]
        new_frontmatter = "\n".join(filtered_lines)
        return f"---\n{new_frontmatter}\n---\n{body}"
    return content


def _list_skill_resource_files(skill_file_path: str, limit: int = DEFAULT_SKILL_RESOURCE_FILE_LIMIT) -> SkillResourceListing:
    skill_dir = Path(skill_file_path).parent
    files: list[str] = []
    truncated = False

    def visit(directory: Path, relative_dir: str = "") -> None:
        nonlocal truncated
        if len(files) > limit:
            truncated = True
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except PermissionError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            relative_path = str(Path(relative_dir) / entry.name) if relative_dir else entry.name
            full_path = directory / entry.name
            if entry.is_dir():
                if entry.name in SKILL_RESOURCE_EXCLUDED_DIRS:
                    continue
                visit(full_path, relative_path)
                if truncated:
                    return
                continue
            if not entry.is_file() or entry.name == "SKILL.md":
                continue
            files.append(_to_posix_path(relative_path))
            if len(files) > limit:
                truncated = True
                return

    visit(skill_dir)
    return SkillResourceListing(files=files[:limit], truncated=truncated)


def _render_skill_resources(skill_file_path: str | None = None) -> str:
    if not skill_file_path:
        return ""
    listing = _list_skill_resource_files(skill_file_path)
    if not listing["files"] and not listing["truncated"]:
        return ""
    file_lines = [f"  <file>{_escape_xml(f)}</file>" for f in listing["files"]]
    note_lines = (
        [f"  <note>Listing capped at {DEFAULT_SKILL_RESOURCE_FILE_LIMIT} files and may be incomplete.</note>"]
        if listing["truncated"] else []
    )
    return f"\n\n<skill_resources>\n" + "\n".join(file_lines + note_lines) + "\n</skill_resources>"


def _render_skill_document_block(skill: SkillPromptDocument) -> str:
    path_attr = f' path="{_escape_xml(skill["path"])}"' if skill.get("path") else ""
    resources = _render_skill_resources(skill.get("skillFilePath"))
    content = _strip_skill_prompt_metadata(skill["content"])
    return (
        f'<{skill["name"]}-skill{path_attr}>\n'
        f"{content}{resources}\n"
        f"</{skill['name']}-skill>"
    )


# =========================================================================
# Public API
# =========================================================================


def getExtensionRoot() -> Path:
    return Path(__file__).parent.parent


def readToolDocs(options: dict[str, Any] | None = None) -> str:
    docs: list[str] = []
    model = (options or {}).get("model", "")
    for _entry_name, content_key, is_ejs in _TOOL_ENTRIES:
        content = _TOOL_CONTENT_MAP[content_key]
        if is_ejs:
            multimodal_line = _render_multimodal_line(model)
            content = content.replace("__MULTIMODAL_LINE__", multimodal_line)
        trimmed = content.strip()
        if trimmed:
            docs.append(trimmed)
    return "\n\n".join(docs)


def getSystemPrompt(projectRoot: str, options: dict[str, Any] | None = None) -> str:
    opts = options or {}
    mode = opts.get("mode", "build")
    tool_docs = readToolDocs(opts)
    prompt = SYSTEM_PROMPT_BASE

    # Plan-mode specific instructions
    if mode == "plan":
        prompt += """

# PLAN MODE ACTIVE
You are currently in **plan mode**. In this mode you can ONLY use read-only tools:
- `read`, `glob`, `grep` — explore the codebase
- `websearch`, `webfetch` — research online
- `update_plan`, `todowrite` — propose and maintain plans

**You CANNOT use**: `bash`, `write`, `edit`, `task`, `skill`.

Your job is to:
1. Explore the codebase thoroughly using read-only tools
2. Propose a clear, step-by-step plan using the `update_plan` tool
3. When your plan is complete and the user is satisfied, tell them to switch to **build mode** to execute it

Be thorough — investigate all relevant files, understand the architecture, and produce a detailed plan before asking the user to switch modes."""

    if tool_docs:
        return f"{prompt}\n\n# Available Tools\n\n{tool_docs}"
    return prompt


def getTools(
    options: dict[str, Any] | None = None,
    externalTools: list[ToolDefinition] | None = None,
) -> list[ToolDefinition]:
    tools: list[ToolDefinition] = [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Execute shell commands in a persistent bash session.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command to execute",
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "Clear, concise description of what this command does in active voice. "
                                'Never use words like "complex" or "risk" in the description '
                                "- just describe what it does."
                            ),
                        },
                        "sideEffects": {
                            "description": (
                                "Permission scopes required by this bash command. Use [] only for commands "
                                'that do not read, write, delete, or access the network. Use ["unknown"] '
                                "when the effects cannot be classified safely."
                            ),
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [
                                    "read-in-cwd",
                                    "read-out-cwd",
                                    "write-in-cwd",
                                    "write-out-cwd",
                                    "delete-in-cwd",
                                    "delete-out-cwd",
                                    "query-git-log",
                                    "mutate-git-log",
                                    "network",
                                    "unknown",
                                ],
                            },
                            "uniqueItems": True,
                        },
                        "run_in_background": {
                            "type": "boolean",
                            "description": (
                                "Set to true to run the command in the background. Use this only "
                                "when you need to perform a blocking task and do not need the result immediately."
                            ),
                        },
                    },
                    "required": ["command", "sideEffects"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "AskUserQuestion",
                "description": (
                    "When the task has ambiguities or multiple implementation approaches, use this tool "
                    "to pause execution and ask the user a question to get clarification or make a decision."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "questions": {
                            "type": "array",
                            "description": "Questions to present to the user. Usually only one question is needed at a time.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "question": {
                                        "type": "string",
                                        "description": "The question to ask the user.",
                                    },
                                    "multiSelect": {
                                        "type": "boolean",
                                        "description": "Whether the user may choose multiple options.",
                                    },
                                    "options": {
                                        "type": "array",
                                        "description": "A list of predefined options for the user to choose from.",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "label": {
                                                    "type": "string",
                                                    "description": "The display text for the option.",
                                                },
                                                "description": {
                                                    "type": "string",
                                                    "description": (
                                                        "A detailed explanation or hint about this option "
                                                        "to help the user understand what happens if they choose it."
                                                    ),
                                                },
                                            },
                                            "required": ["label"],
                                        },
                                    },
                                },
                                "required": ["question", "options"],
                            },
                        },
                    },
                    "required": ["questions"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "UpdatePlan",
                "description": (
                    "Update the current task plan. The plan argument must be the complete markdown "
                    "task list to show as the latest progress state."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plan": {
                            "type": "string",
                            "description": (
                                "The complete markdown task list, including task status markers "
                                "such as [ ], [>], [x], and optional notes."
                            ),
                        },
                        "explanation": {
                            "type": "string",
                            "description": "Optional short reason for changing the plan.",
                        },
                    },
                    "required": ["plan"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read files from the filesystem (text, images, PDFs, notebooks).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "UNIX-style path to file",
                        },
                        "offset": {
                            "type": "number",
                            "description": "Line number to start reading from",
                        },
                        "limit": {
                            "type": "number",
                            "description": "Number of lines to read",
                        },
                        "pages": {
                            "type": "string",
                            "description": (
                                'Page range for PDF files (e.g., "1-5", "3", "10-20"). Only applicable to PDF files.'
                            ),
                        },
                    },
                    "required": ["file_path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write",
                "description": "Create files or overwrite them with a complete string payload. Prefer edit for existing files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Absolute path to file",
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "Complete file content as a single string. Serialize JSON documents before writing."
                            ),
                        },
                    },
                    "required": ["file_path", "content"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit",
                "description": "Perform scoped string replacements in files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "snippet_id": {
                            "type": "string",
                            "description": "Required Read/Edit snippet_id.",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Optional absolute path guard; must match snippet_id's file.",
                        },
                        "old_string": {
                            "type": "string",
                            "description": "Exact text to replace inside snippet_id's scope",
                        },
                        "new_string": {
                            "type": "string",
                            "description": "Replacement text (must differ from old_string)",
                        },
                        "replace_all": {
                            "type": "boolean",
                            "description": "Replace all occurences of old_string (default false)",
                        },
                        "expected_occurrences": {
                            "type": "number",
                            "description": (
                                "Expected number of matches, especially useful as a safety check with replace_all"
                            ),
                        },
                    },
                    "required": ["snippet_id", "old_string", "new_string"],
                    "additionalProperties": False,
                },
            },
        },
    ]

    tools.append({
        "type": "function",
        "function": {
            "name": "WebSearch",
            "description": "Perform web searching using a natural language query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "A search query phrased as a clear, specific natural language question "
                            "or statement that includes key context."
                        ),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    })

    if externalTools:
        tools.extend(externalTools)

    return tools


def getCompactPrompt(sessionMessages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for msg in sessionMessages:
        lines.append(json.dumps(
            {
                "id": msg.get("id"),
                "role": msg.get("role"),
                "content": msg.get("content"),
                "contentParams": msg.get("contentParams"),
                "messageParams": msg.get("messageParams"),
                "createTime": msg.get("createTime"),
            },
            ensure_ascii=False,
        ))
    jsonl = "\n".join(lines)
    return f"{COMPACT_PROMPT_BASE}\n\nconversation below:\n\n```jsonl\n{jsonl}\n```"


def getRuntimeContext(projectRoot: str, model: str | None = None) -> str:
    uname = _get_uname_info()
    shell_path = _get_shell_path_info()
    shell_mode_opts: dict[str, str] = {}
    runtime_versions = _get_runtime_version_info()

    env: dict[str, Any] = {
        "root path": projectRoot,
        "pwd": projectRoot,
        "homedir": str(Path.home()),
        "system info": uname,
        "shell path": shell_path,
        **shell_mode_opts,
        **runtime_versions,
        "command installed": {
            "ripgrep": _check_tool_installed("rg"),
            "jq": _check_tool_installed("jq"),
        },
    }

    return (
        f"{_get_current_date_and_model_prompt(model)}\n\n"
        f"# Local Workspace Environment\n\n"
        f"```json\n{json.dumps(env, indent=2)}\n```"
    )


def buildSkillDocumentsPrompt(skills: list[SkillPromptDocument]) -> str:
    blocks = [_render_skill_document_block(skill) for skill in skills]
    return f"Use the skill documents below to assist the user:\n" + "\n\n".join(blocks)


def readDefaultSkillDocs(
    enabledSkills: dict[str, bool] | None = None,
) -> list[SkillPromptDocument]:
    if enabledSkills is None:
        enabledSkills = {}

    skill_map: dict[str, str] = {
        "karpathy-guidelines": SKILL_KARPATHY_GUIDELINES,
    }

    result: list[SkillPromptDocument] = []
    for name in DEFAULT_SKILL_TEMPLATES:
        if enabledSkills.get(name) is False:
            continue
        content = skill_map.get(name)
        if content:
            trimmed = content.strip()
            if trimmed:
                result.append(SkillPromptDocument(
                    name=name,
                    content=trimmed,
                    path=None,
                    skillFilePath=None,
                ))
    return result


def getDefaultSkillPrompt(options: dict[str, Any] | None = None) -> str:
    if options is None:
        options = {}
    skill_docs = readDefaultSkillDocs(options.get("enabledSkills"))
    if not skill_docs:
        return ""
    return buildSkillDocumentsPrompt(skill_docs)


def getInitCommandPrompt(agents_md_file: str | None = None) -> str:
    if agents_md_file is None:
        init_or_update = "Generate a file named ./AGENTS.md that serves as a contributor guide for this repository."
    else:
        init_or_update = (
            f"Update {agents_md_file} to align it with repository changes made after "
            f"the last time {agents_md_file} was modified."
        )
    return INIT_COMMAND_PROMPT.replace("__INIT_OR_UPDATE__", init_or_update)


# ── snake_case aliases for session/manager.py ─────────────────

def get_extension_root() -> str:
    return str(getExtensionRoot())


def get_system_prompt(project_root: str, options: dict | None = None) -> str:
    return getSystemPrompt(project_root, options or {})


def get_runtime_context(project_root: str, model: str) -> str:
    return str(getRuntimeContext(project_root, model))


def get_tools(options: dict | None = None, mcp_tool_definitions: list | None = None) -> list[dict]:
    return getTools(options or {}, mcp_tool_definitions)


def get_compact_prompt(messages: list[dict]) -> str:
    return str(getCompactPrompt(messages))


def get_default_skill_prompt(options: dict | None = None) -> str | None:
    result = getDefaultSkillPrompt(options or {})
    return str(result) if result else None


def build_skill_documents_prompt(documents: list[dict]) -> str:
    return buildSkillDocumentsPrompt(documents)
