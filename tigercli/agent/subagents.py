from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from tigercli.client import call
from tigercli.config import get_active_provider_model, settings


READ_ONLY_TOOLS = {"read", "glob", "grep", "webfetch", "websearch"}
GENERAL_TOOLS = READ_ONLY_TOOLS | {"bash"}
SUBAGENT_MAX_TURNS = 8


@dataclass(frozen=True)
class SubagentDefinition:
    name: str
    description: str
    prompt: str
    tools: set[str]
    provider: str | None = None
    model: str | None = None


def load_subagents(project_path: str) -> dict[str, SubagentDefinition]:
    agents: dict[str, SubagentDefinition] = {
        "explore": SubagentDefinition(
            name="explore",
            description="Search and read the codebase without making changes.",
            prompt=(
                "You are a fast codebase exploration sub-agent. Search, read, and summarize. "
                "Do not modify files. Return a concise report with file paths and line references."
            ),
            tools=set(READ_ONLY_TOOLS),
        ),
        "general": SubagentDefinition(
            name="general",
            description="Research or run bounded shell commands, then report back.",
            prompt=(
                "You are a general-purpose sub-agent. Work independently, use tools as needed, "
                "and return only the final result to the main agent. Do not modify files unless "
                "your agent definition explicitly enables write/edit tools."
            ),
            tools=set(GENERAL_TOOLS),
        ),
    }

    for directory in _agent_dirs(project_path):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            definition = _parse_agent_file(path)
            if definition:
                agents[definition.name] = definition
    return agents


async def run_subagent(args: dict, project_path: str) -> str:
    from tigercli.agent.tools import execute_tool, get_tool_schemas_by_names

    subagent_type = str(args.get("subagent_type") or args.get("agent") or "general")
    task_prompt = str(args.get("prompt") or "").strip()
    description = str(args.get("description") or subagent_type).strip()
    if not task_prompt:
        return "Subagent error: prompt is required."

    agents = load_subagents(project_path)
    definition = agents.get(subagent_type)
    if definition is None:
        available = ", ".join(sorted(agents))
        return f"Subagent error: unknown subagent_type '{subagent_type}'. Available: {available}"

    effective_tools = set(args.get("_tools") or definition.tools)
    provider = str(args.get("provider") or definition.provider or args.get("_provider") or "")
    model = str(args.get("model") or definition.model or args.get("_model") or "")
    if not provider or not model:
        provider, model = get_active_provider_model()
    tool_schemas = get_tool_schemas_by_names(effective_tools)
    messages = [
        {"role": "system", "content": _system_prompt(definition, project_path, effective_tools)},
        {"role": "user", "content": f"Task: {description}\n\n{task_prompt}"},
    ]

    for turn in range(SUBAGENT_MAX_TURNS):
        try:
            response = await call(
                provider=provider,
                model=model,
                messages=messages,
                tools=tool_schemas,
                stream=False,
            )
        except Exception as e:
            return f"Subagent error: {e}"

        content = response.get("content") or ""
        tool_calls = response.get("tool_calls") or []
        assistant_msg = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            return _format_result(definition.name, description, content)

        for tool_call in tool_calls:
            name = tool_call.get("function", {}).get("name", "")
            raw_args = tool_call.get("function", {}).get("arguments") or "{}"
            try:
                tool_args = json.loads(raw_args)
            except json.JSONDecodeError:
                tool_args = {}

            if name not in effective_tools or name == "task":
                result = f"Tool '{name}' is not available to subagent '{definition.name}'."
            else:
                result = await execute_tool(name, tool_args, project_path)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.get("id", f"subagent_tool_{turn}"),
                "content": result,
            })

    return _format_result(
        definition.name,
        description,
        "Subagent stopped after reaching the turn limit without a final answer.",
    )


def describe_subagents(project_path: str) -> str:
    lines = []
    for name, definition in sorted(load_subagents(project_path).items()):
        tools = ", ".join(sorted(definition.tools)) or "none"
        model_hint = ""
        if definition.provider or definition.model:
            model_hint = f" Model: {definition.provider or 'session'}/{definition.model or 'session'}"
        lines.append(f"- {name}: {definition.description} Tools: {tools}{model_hint}")
    return "\n".join(lines)


def _agent_dirs(project_path: str) -> list[Path]:
    return [
        Path(project_path) / ".agents",
        settings.config_home / "agents",
    ]


def _parse_agent_file(path: Path) -> SubagentDefinition | None:
    try:
        text = path.read_text()
    except Exception:
        return None

    meta: dict[str, str] = {}
    body = text.strip()
    if body.startswith("---"):
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", body, re.DOTALL)
        if match:
            for line in match.group(1).splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                meta[key.strip().lower()] = value.strip().strip('"\'')
            body = match.group(2).strip()

    name = _agent_name(meta.get("name") or path.stem)
    if not name:
        return None

    description = meta.get("description") or _first_heading(body) or f"Custom subagent '{name}'."
    tools = _parse_tools(meta.get("tools"))
    if not tools:
        tools = set(READ_ONLY_TOOLS)

    return SubagentDefinition(
        name=name,
        description=description,
        prompt=body or description,
        tools=tools,
        provider=meta.get("provider") or None,
        model=meta.get("model") or None,
    )


def _parse_tools(value: str | None) -> set[str]:
    if not value:
        return set()
    cleaned = value.strip().strip("[]")
    tools = {part.strip().strip('"\'') for part in cleaned.split(",")}
    return {tool for tool in tools if tool}


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip() or None
    return None


def _agent_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")


def _system_prompt(definition: SubagentDefinition, project_path: str, tools_set: set[str] | None = None) -> str:
    tools = ", ".join(sorted(tools_set or definition.tools)) or "none"
    model_line = ""
    if definition.provider or definition.model:
        model_line = f"Preferred model: {definition.provider or 'session provider'}/{definition.model or 'session model'}\n"
    return (
        f"Subagent: {definition.name}\n"
        f"Working directory: {project_path}\n"
        f"{model_line}"
        f"Available tools: {tools}\n\n"
        f"{definition.prompt}\n\n"
        "Rules:\n"
        "- Work independently and return a final report only.\n"
        "- Include specific files and line numbers when relevant.\n"
        "- Do not ask the user questions. If blocked, state the blocker clearly.\n"
        "- Keep the final answer concise and factual."
    )


def _format_result(agent_name: str, description: str, content: str) -> str:
    content = content.strip() or "No result returned."
    return f"Subagent '{agent_name}' completed: {description}\n\n{content}"
