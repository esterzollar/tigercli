---
name: code-review
description: Review code changes for bugs, regressions, security risks, and missing tests.
tools: read, glob, grep
context: fork
agent: reviewer
---
Review the current code changes or the area named in the arguments.

Arguments: $ARGUMENTS

Return findings first, ordered by severity. Include file paths and line references. Keep the summary brief.
