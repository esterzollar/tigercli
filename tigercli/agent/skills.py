from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tigercli.config import settings


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    body: str
    path: Path
    tools: set[str]
    provider: str | None = None
    model: str | None = None
    context: str = "inline"
    agent: str | None = None
    disable_model_invocation: bool = False
    user_invocable: bool = True


def load_skills(project_path: str) -> dict[str, SkillDefinition]:
    skills: dict[str, SkillDefinition] = {}
    for directory in _skill_dirs(project_path):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*/SKILL.md")):
            skill = _parse_skill_file(path)
            if skill:
                skills[skill.name] = skill
    return skills


def describe_skills(project_path: str, *, include_model_disabled: bool = False) -> str:
    lines = []
    for name, skill in sorted(load_skills(project_path).items()):
        if skill.disable_model_invocation and not include_model_disabled:
            continue
        suffix = ""
        if skill.context == "fork":
            suffix += f" Context: fork via {skill.agent or 'general'}"
        if skill.provider or skill.model:
            suffix += f" Model: {skill.provider or 'session'}/{skill.model or 'session'}"
        lines.append(f"- {name}: {skill.description}{suffix}")
    return "\n".join(lines) or "No skills configured."


async def run_skill(args: dict, project_path: str) -> str:
    name = _skill_name(str(args.get("name") or args.get("skill") or ""))
    if not name:
        return "Skill error: name is required."

    skill = load_skills(project_path).get(name)
    if skill is None:
        available = ", ".join(sorted(load_skills(project_path))) or "none"
        return f"Skill error: unknown skill '{name}'. Available: {available}"

    rendered = render_skill(skill, str(args.get("arguments") or ""))
    if skill.context == "fork":
        from tigercli.agent.subagents import run_subagent

        return await run_subagent({
            "subagent_type": skill.agent or "general",
            "description": f"skill:{skill.name}",
            "prompt": rendered,
            "provider": args.get("provider") or skill.provider or args.get("_provider"),
            "model": args.get("model") or skill.model or args.get("_model"),
            "_tools": sorted(skill.tools) if skill.tools else None,
        }, project_path)

    return f"Skill '{skill.name}' loaded:\n\n{rendered}"


def resolve_slash_skill(user_message: str, project_path: str) -> str | None:
    if not user_message.startswith("/"):
        return None
    first, _, rest = user_message[1:].partition(" ")
    name = _skill_name(first)
    if not name:
        return None
    skill = load_skills(project_path).get(name)
    if skill is None or not skill.user_invocable:
        return None
    return render_skill(skill, rest.strip())


def render_skill(skill: SkillDefinition, arguments: str = "") -> str:
    rendered = skill.body
    rendered = rendered.replace("$ARGUMENTS", arguments)
    parts = _split_args(arguments)
    for index, value in enumerate(parts):
        rendered = rendered.replace(f"$ARGUMENTS[{index}]", value)
        rendered = rendered.replace(f"${index}", value)
    if arguments and "$ARGUMENTS" not in skill.body and not _has_index_placeholder(skill.body):
        rendered = f"{rendered.rstrip()}\n\nARGUMENTS: {arguments}"
    return rendered.strip()


def _skill_dirs(project_path: str) -> list[Path]:
    return [
        Path(project_path) / ".skills",
        settings.config_home / "skills",
    ]


def _parse_skill_file(path: Path) -> SkillDefinition | None:
    try:
        text = path.read_text()
    except Exception:
        return None

    meta, body = _split_frontmatter(text)
    name = _skill_name(meta.get("name") or path.parent.name)
    if not name:
        return None
    description = meta.get("description") or _first_paragraph(body) or f"Skill '{name}'."
    return SkillDefinition(
        name=name,
        description=description,
        body=body.strip() or description,
        path=path,
        tools=_parse_list(meta.get("tools") or meta.get("allowed-tools")),
        provider=meta.get("provider") or None,
        model=meta.get("model") or None,
        context=(meta.get("context") or "inline").lower(),
        agent=meta.get("agent") or None,
        disable_model_invocation=_parse_bool(meta.get("disable-model-invocation")),
        user_invocable=_parse_user_invocable(meta.get("user-invocable")),
    )


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    body = text.strip()
    meta: dict[str, str] = {}
    if not body.startswith("---"):
        return meta, body
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", body, re.DOTALL)
    if not match:
        return meta, body
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip().lower()] = value.strip().strip('"\'')
    return meta, match.group(2).strip()


def _parse_list(value: str | None) -> set[str]:
    if not value:
        return set()
    cleaned = value.strip().strip("[]")
    return {part.strip().strip('"\'') for part in re.split(r"[,\s]+", cleaned) if part.strip()}


def _parse_bool(value: str | None, *, default: bool = False, invert: bool = False) -> bool:
    if value is None:
        return default
    parsed = value.strip().lower() in {"1", "true", "yes", "on"}
    return not parsed if invert else parsed


def _parse_user_invocable(value: str | None) -> bool:
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _skill_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")


def _first_paragraph(text: str) -> str | None:
    chunks = [chunk.strip() for chunk in text.split("\n\n") if chunk.strip()]
    if not chunks:
        return None
    return re.sub(r"\s+", " ", chunks[0]).strip("# ")[:240]


def _split_args(arguments: str) -> list[str]:
    if not arguments:
        return []
    try:
        import shlex

        return shlex.split(arguments)
    except ValueError:
        return arguments.split()


def _has_index_placeholder(text: str) -> bool:
    return bool(re.search(r"\$(?:ARGUMENTS\[\d+\]|\d+)", text))
