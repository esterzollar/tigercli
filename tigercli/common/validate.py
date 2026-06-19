from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol, TypedDict, Union


class ValidationResult(TypedDict):
    ok: bool
    input: Any
    error: str


def _ok(input: Dict[str, Any]) -> ValidationResult:
    return {"ok": True, "input": input, "error": ""}


def _err(error: str) -> ValidationResult:
    return {"ok": False, "input": {}, "error": error}


class ToolExecutionContext(TypedDict, total=False):
    sessionId: str
    projectRoot: str
    toolCall: Dict[str, Any]
    createOpenAIClient: Any
    onProcessStart: Any
    onProcessExit: Any
    onProcessStdout: Any
    onProcessTimeoutControl: Any
    onBackgroundProcessComplete: Any
    onBeforeFileMutation: Any
    onAfterFileMutation: Any
    bashTimeoutMs: int
    bashMinTimeoutMs: int


class ToolExecutionResult(TypedDict, total=False):
    ok: bool
    name: str
    output: str
    error: str
    metadata: Dict[str, Any]
    awaitUserResponse: bool
    followUpMessages: List[Any]


SchemaValidator = Callable[[Dict[str, Any]], ValidationResult]


def semantic_boolean(default_value: bool = False) -> SchemaValidator:
    def validate(value: Any) -> Any:
        if value == "true":
            return True
        if value == "false":
            return False
        return value

    def validator(input: Dict[str, Any]) -> ValidationResult:
        return _ok(input)

    return validator


def semantic_integer(label: str, min_val: Optional[int] = None) -> SchemaValidator:
    effective_min = min_val if min_val is not None else -(2**53)

    def preprocess(value: Any) -> Any:
        if isinstance(value, str) and value.strip():
            try:
                return int(value)
            except (ValueError, TypeError):
                return value
        return value

    def validate(input: Dict[str, Any]) -> Any:
        for key, value in input.items():
            if label.lower() in key.lower():
                processed = preprocess(value)
                if not isinstance(processed, int) or isinstance(processed, bool):
                    continue
                if processed < effective_min:
                    raise ValueError(f"{label} must be >= {effective_min}.")
                input[key] = processed
        return input

    def validator(input: Dict[str, Any]) -> ValidationResult:
        try:
            result = validate(input)
            return _ok(result)
        except ValueError as e:
            return _err(str(e))

    return validator


def format_validation_error(issues: List[Dict[str, Any]]) -> str:
    if not issues:
        return "Invalid tool input."
    issue = issues[0]
    path = issue.get("path", [])
    path_str = f"{'.'.join(str(p) for p in path)}: " if path else ""
    message = issue.get("message", "Invalid tool input.")
    return f"{path_str}{message}"


def execute_validated_tool(
    name: str,
    schema: SchemaValidator,
    raw_args: Dict[str, Any],
    context: ToolExecutionContext,
    handler: Callable[[Dict[str, Any], ToolExecutionContext], ToolExecutionResult],
    preprocess: Optional[Callable[[Dict[str, Any]], ValidationResult]] = None,
) -> ToolExecutionResult:
    if preprocess is not None:
        preprocessed = preprocess(raw_args)
    else:
        preprocessed = _ok(raw_args)

    if not preprocessed["ok"]:
        return {
            "ok": False,
            "name": name,
            "error": f"InputValidationError: {preprocessed['error']}",
        }

    parsed = schema(preprocessed["input"])
    if not parsed["ok"]:
        return {
            "ok": False,
            "name": name,
            "error": f"InputValidationError: {parsed['error']}",
        }

    return handler(parsed["input"], context)
