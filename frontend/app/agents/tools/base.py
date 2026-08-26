"""Tool-calling foundation for the ChatGPT → AutoFix → OpenCode bridge.

The model never edits files and never runs commands directly.  It may only
call the narrowly scoped tools registered here, each of which:

- has a STRICT JSON-schema input contract (``additionalProperties: false``,
  every property required),
- declares a permission level (:class:`Permission`),
- returns a structured JSON result — success payloads carry ``ok: true``,
  failures carry ``{"ok": false, "error": {"code", "message"}}`` so the
  model can react honestly instead of guessing.

Validation is implemented locally (no new dependencies) for exactly the
schema subset this package uses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable


class Permission(str, Enum):
    """Escalation ladder — a tool may not exceed the context's ceiling."""

    READ_ONLY = "READ_ONLY"
    PLANNING = "PLANNING"
    EXECUTION = "EXECUTION"
    CONTROL = "CONTROL"

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK = {
    Permission.READ_ONLY: 0,
    Permission.PLANNING: 1,
    Permission.EXECUTION: 2,
    Permission.CONTROL: 3,
}


class ToolError(Exception):
    """Structured tool failure surfaced to the model as JSON."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_payload(self) -> dict:
        return {"code": self.code, "message": self.message}


#: Error codes used across the tools package (documentation + tests).
E_UNKNOWN_TOOL = "unknown_tool"
E_INVALID_ARGUMENTS = "invalid_arguments"
E_PERMISSION_DENIED = "permission_denied"
E_PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
E_SECRET_FILE = "secret_file"
E_NOT_FOUND = "not_found"
E_APPROVAL_REQUIRED = "approval_required"
E_CANCELLED = "cancelled_no_restart"
E_CONFLICT = "conflict"
E_FAILED = "failed"


@dataclass(frozen=True)
class ToolSpec:
    """One callable tool exposed to the model."""

    name: str
    description: str
    parameters: dict  # strict JSON schema for the INPUT arguments
    handler: Callable  # (arguments: dict, context: ToolContext) -> dict
    permission: Permission
    #: Declared shape of successful results; ``required`` keys are enforced
    #: after execution so a broken handler can never masquerade as success.
    result_required: tuple[str, ...] = ("summary",)

    def to_api_schema(self) -> dict:
        """OpenAI chat-completions function definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _strictify(self.parameters),
            },
        }


@dataclass
class ToolContext:
    """Everything a tool handler is allowed to touch.

    ``services`` carries the AutoFix bridges (task board / pipeline access).
    Tools must never reach beyond workspace + services + env.
    """

    workspace: Path
    env: dict = field(default_factory=dict)
    services: object | None = None
    #: Highest permission a tool may have in this context.
    max_permission: Permission = Permission.CONTROL
    #: Observability sink: emit(event_name, redacted_payload_dict).
    emit: Callable[[str, dict], None] | None = None

    def observe(self, event: str, payload: dict | None = None) -> None:
        if self.emit is None:
            return
        try:
            self.emit(event, dict(payload or {}))
        except Exception:
            pass


# ----------------------------------------------------------------------
# Strict schema validation (subset: object/string/integer/number/
# boolean/array with enum/min/max/length constraints)
# ----------------------------------------------------------------------

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def validate_arguments(schema: dict, value, path: str = "") -> None:
    """Validate *value* against *schema*; raise ToolError on any violation."""
    where = path or "root"
    expected_type = schema.get("type", "object")
    check = _TYPE_CHECKS.get(expected_type)
    if check is None or not check(value):
        raise ToolError(
            E_INVALID_ARGUMENTS,
            f"Argument '{where}' must be of type {expected_type}.",
        )

    if "enum" in schema and value not in schema["enum"]:
        raise ToolError(
            E_INVALID_ARGUMENTS,
            f"Argument '{where}' must be one of {schema['enum']!r}.",
        )

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ToolError(
                E_INVALID_ARGUMENTS,
                f"Argument '{where}' is shorter than {schema['minLength']} characters.",
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ToolError(
                E_INVALID_ARGUMENTS,
                f"Argument '{where}' exceeds {schema['maxLength']} characters.",
            )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolError(
                E_INVALID_ARGUMENTS,
                f"Argument '{where}' must be >= {schema['minimum']}.",
            )
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolError(
                E_INVALID_ARGUMENTS,
                f"Argument '{where}' must be <= {schema['maximum']}.",
            )

    if isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_arguments(item_schema, item, f"{where}[{index}]")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise ToolError(
                    E_INVALID_ARGUMENTS,
                    f"Missing required argument '{key}'.",
                )
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    raise ToolError(
                        E_INVALID_ARGUMENTS,
                        f"Unexpected argument '{key}' (strict schema).",
                    )
        for key, sub_value in value.items():
            sub_schema = properties.get(key)
            if sub_schema is not None:
                validate_arguments(sub_schema, sub_value, f"{where}.{key}")


def _strictify(schema: dict) -> dict:
    """Return a copy of *schema* guaranteed to be strict."""
    out = dict(schema or {})
    properties = out.get("properties")
    if properties is not None:
        out["additionalProperties"] = False
        props = {name: dict(sub) for name, sub in properties.items()}
        out["properties"] = props
        declared = set(props)
        required = [r for r in out.get("required", []) if r in declared]
        missing = declared - set(required)
        out["required"] = sorted(set(required) | missing)
    return out


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


class ToolRegistry:
    """Ordered registry of :class:`ToolSpec` instances."""

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        # Fail fast on schemas that could never validate.
        validate_arguments(spec.parameters, {})
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def api_schemas(self) -> list[dict]:
        return [spec.to_api_schema() for spec in self._tools.values()]

    # ------------------------------------------------------------------

    def dispatch(self, name, arguments, context: ToolContext) -> dict:
        """Validate + execute; ALWAYS returns a structured JSON payload."""
        spec = self._tools.get(name)
        if spec is None:
            return _failure(E_UNKNOWN_TOOL, f"Unknown tool: {name!r}.")

        if spec.permission.rank > context.max_permission.rank:
            return _failure(
                E_PERMISSION_DENIED,
                f"Tool '{name}' requires permission "
                f"{spec.permission.value}, above the allowed "
                f"{context.max_permission.value}.",
            )

        try:
            parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
        except (TypeError, ValueError):
            return _failure(
                E_INVALID_ARGUMENTS,
                "Arguments must be a JSON object.",
            )

        try:
            validate_arguments(spec.parameters, parsed)
        except ToolError as exc:
            return _failure(exc.code, exc.message)

        try:
            result = spec.handler(parsed, context)
        except ToolError as exc:
            context.observe(f"tool_error:{name}", {"code": exc.code})
            return _failure(exc.code, exc.message)
        except Exception as exc:  # defensive — never leak tracebacks to the model
            context.observe(f"tool_error:{name}", {"exception": type(exc).__name__})
            return _failure(E_FAILED, f"{type(exc).__name__}: {exc}")

        if not isinstance(result, dict):
            return _failure(E_FAILED, "Tool returned an invalid result.")

        missing = [key for key in spec.result_required if key not in result]
        if missing:
            return _failure(
                E_FAILED,
                f"Tool '{name}' result is missing required field(s): "
                + ", ".join(missing),
            )

        payload = {"ok": True}
        payload.update(result)
        context.observe(f"tool_call:{name}", {"ok": True})
        return payload


def _failure(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}
