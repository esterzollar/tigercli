from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from tigercli.common.settings import supports_multimodal

ReasoningEffort = Literal["high", "max"]


def build_thinking_request_options(
    thinking_enabled: bool,
    base_url: Optional[str] = None,
    reasoning_effort: ReasoningEffort = "max",
) -> dict[str, Any]:
    thinking: dict[str, str] = {"type": "enabled" if thinking_enabled else "disabled"}

    result: dict[str, Any] = {"thinking": thinking}

    if thinking_enabled:
        result["extra_body"] = {"reasoning_effort": reasoning_effort}

    return result


@dataclass
class MessageMeta:
    function: Any = None
    paramsMd: Optional[str] = None
    resultMd: Optional[str] = None
    asThinking: bool = False
    isSummary: bool = False
    isModelChange: bool = False
    skill: Any = None
    permissions: Any = None
    userPrompt: Any = None


class OpenAIMessageConverter:
    def __init__(
        self,
        render_init_prompt: Optional[Callable[[], str]] = None,
    ) -> None:
        self._render_init_prompt = render_init_prompt

    def build_messages(
        self,
        messages: list[dict[str, Any]],
        thinking_enabled: bool,
        model: str,
    ) -> list[dict[str, Any]]:
        active = [m for m in messages if not m.get("compacted")]
        tool_pairings = self._pair_tool_messages(active)
        openai_messages: list[dict[str, Any]] = []

        for index, message in enumerate(active):
            if message.get("role") == "tool":
                continue

            openai_messages.append(self._convert_message(message, thinking_enabled, model))

            tool_calls = self._get_assistant_tool_calls(message)
            if not tool_calls:
                continue

            for tool_call_index, tool_call in enumerate(tool_calls):
                tool_call_id = self._get_tool_call_id(tool_call)
                if not tool_call_id:
                    continue

                paired_index = tool_pairings.get(self._build_pairing_key(index, tool_call_index))
                if paired_index is not None:
                    openai_messages.append(
                        self._convert_message(active[paired_index], thinking_enabled, model)
                    )
                    continue

                openai_messages.append(
                    self._build_interrupted_openai_tool_message(tool_calls, tool_call_id)
                )

        return openai_messages

    def get_trailing_pending_tool_call_message(
        self, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        active = [m for m in messages if not m.get("compacted")]
        if not active:
            return {"message": None, "toolCalls": []}

        latest = active[-1]
        if latest.get("role") != "assistant":
            return {"message": None, "toolCalls": []}

        tool_calls = self._get_assistant_tool_calls(latest)
        if not tool_calls:
            return {"message": None, "toolCalls": []}

        filtered = [tc for tc in tool_calls if self._get_tool_call_id(tc)]
        return {"message": latest, "toolCalls": filtered}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _convert_message(
        self,
        message: dict[str, Any],
        thinking_enabled: bool,
        model: str,
    ) -> dict[str, Any]:
        content = self._render_content(message)
        base: dict[str, Any] = {"role": message.get("role", "user"), "content": content}

        msg_params = message.get("messageParams")
        if isinstance(msg_params, dict):
            tool_calls = msg_params.get("tool_calls")
            if tool_calls:
                base["tool_calls"] = tool_calls

            tool_call_id = msg_params.get("tool_call_id")
            if tool_call_id:
                base["tool_call_id"] = tool_call_id

            reasoning_content = msg_params.get("reasoning_content")
            if isinstance(reasoning_content, str):
                base["reasoning_content"] = reasoning_content
            elif thinking_enabled and message.get("role") == "assistant":
                base["reasoning_content"] = ""
        elif thinking_enabled and message.get("role") == "assistant":
            base["reasoning_content"] = ""

        role = message.get("role", "")
        content_params = message.get("contentParams")
        if role in ("user", "system") and content_params is not None:
            content_parts: list[dict[str, Any]] = []
            if content:
                content_parts.append({"type": "text", "text": content})

            params_list = content_params if isinstance(content_params, list) else [content_params]
            for part in params_list:
                if isinstance(part, dict):
                    part_type = part.get("type")
                    if part_type != "image_url" or supports_multimodal(model):
                        content_parts.append(part)

            if content_parts:
                base["content"] = content_parts

        return base

    def _render_content(self, message: dict[str, Any]) -> str:
        if message.get("role") == "user" and message.get("content") == "/init":
            if self._render_init_prompt:
                return self._render_init_prompt()
            return ""
        return message.get("content") or ""

    def _pair_tool_messages(
        self, messages: list[dict[str, Any]]
    ) -> dict[str, int]:
        pairings: dict[str, int] = {}
        used_tool_indexes: set[int] = set()

        for assistant_index, msg in enumerate(messages):
            tool_calls = self._get_assistant_tool_calls(msg)
            for tool_call_index, tool_call in enumerate(tool_calls):
                tool_call_id = self._get_tool_call_id(tool_call)
                if not tool_call_id:
                    continue

                tool_index = self._find_pairable_tool_index(
                    messages, assistant_index, tool_call_id, used_tool_indexes
                )
                if tool_index is None:
                    continue

                used_tool_indexes.add(tool_index)
                pairings[self._build_pairing_key(assistant_index, tool_call_index)] = tool_index

        return pairings

    def _find_pairable_tool_index(
        self,
        messages: list[dict[str, Any]],
        assistant_index: int,
        tool_call_id: str,
        used_indexes: set[int],
    ) -> Optional[int]:
        first_match: Optional[int] = None
        for index in range(assistant_index + 1, len(messages)):
            msg = messages[index]
            if msg.get("role") != "tool" or index in used_indexes:
                continue

            candidate_id = self._get_tool_message_call_id(msg)
            if candidate_id != tool_call_id:
                continue

            if first_match is None:
                first_match = index
            if not self._is_interrupted_tool_message(msg):
                return index

        return first_match

    def _get_assistant_tool_calls(
        self, message: dict[str, Any]
    ) -> list[Any]:
        if message.get("role") != "assistant":
            return []
        msg_params = message.get("messageParams")
        if isinstance(msg_params, dict):
            tool_calls = msg_params.get("tool_calls")
            if isinstance(tool_calls, list):
                return tool_calls
        return []

    def _get_tool_call_id(self, tool_call: Any) -> Optional[str]:
        if not isinstance(tool_call, dict):
            return None
        tid = tool_call.get("id")
        return tid if isinstance(tid, str) and tid else None

    def _get_tool_message_call_id(self, message: dict[str, Any]) -> Optional[str]:
        msg_params = message.get("messageParams")
        if isinstance(msg_params, dict):
            tid = msg_params.get("tool_call_id")
            return tid if isinstance(tid, str) and tid else None
        return None

    @staticmethod
    def _build_pairing_key(assistant_index: int, tool_call_index: int) -> str:
        return f"{assistant_index}:{tool_call_index}"

    def _is_interrupted_tool_message(self, message: dict[str, Any]) -> bool:
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return False
        try:
            parsed = json.loads(content)
            metadata = parsed.get("metadata", {})
            return metadata.get("interrupted") is True
        except (json.JSONDecodeError, TypeError):
            return False

    def _build_interrupted_openai_tool_message(
        self, tool_calls: list[Any], tool_call_id: str
    ) -> dict[str, Any]:
        tool_func = self.find_tool_function(tool_calls, tool_call_id)
        return {
            "role": "tool",
            "content": self._build_interrupted_tool_result(tool_func, "Previous tool call did not complete."),
            "tool_call_id": tool_call_id,
        }

    def find_tool_function(
        self, tool_calls: list[Any], tool_call_id: str
    ) -> Optional[Any]:
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            if tc.get("id") == tool_call_id:
                return tc.get("function")
        return None

    def _build_interrupted_tool_result(
        self, tool_function: Any, reason: str
    ) -> str:
        tool_name = "tool"
        if isinstance(tool_function, dict):
            name = tool_function.get("name")
            if isinstance(name, str):
                tool_name = name
        return json.dumps(
            {
                "ok": False,
                "name": tool_name,
                "error": reason,
                "metadata": {"interrupted": True},
            },
            indent=2,
        )
