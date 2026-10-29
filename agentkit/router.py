"""Parse and dispatch LLM tool calls (Project 1).

Why: the boundary between "text the model wrote" and "code that runs" must be one narrow,
well-tested function. `ToolRouter.parse` accepts exactly one JSON object naming an allowed
tool, validates its arguments against the tool's strict model, and raises `ToolCallError`
with a message that is safe and useful to send back to the model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from agentkit.llm import LLM, Message
from agentkit.tools import ToolArgs, ToolRegistry

DEFAULT_SYSTEM_PROMPT = (
    "You can call tools. Reply with exactly one JSON object and nothing else:\n"
    '{"tool": "<tool name>", "arguments": {...}}'
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n?```$", re.DOTALL)


class ToolCallError(ValueError):
    """The model's reply was not a valid call to an allowed tool."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: ToolArgs


@dataclass(frozen=True)
class RouterResult:
    call: ToolCall
    output: str
    raw: str


class ToolRouter:
    def __init__(self, llm: LLM, registry: ToolRegistry, system_prompt: str | None = None) -> None:
        self.llm = llm
        self.registry = registry
        self._base_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT

    def system_prompt(self, allowed: Iterable[str] | None = None) -> str:
        return f"{self._base_prompt}\n\nAvailable tools:\n{self.registry.describe(allowed)}"

    def parse(self, raw: str, allowed: Iterable[str] | None = None) -> ToolCall:
        text = raw.strip()
        fenced = _FENCE_RE.match(text)
        if fenced:
            text = fenced.group(1).strip()
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolCallError(f"reply is not valid JSON ({exc.msg})") from exc
        if not isinstance(obj, dict) or "tool" not in obj or set(obj) - {"tool", "arguments"}:
            raise ToolCallError('reply must be a JSON object {"tool": ..., "arguments": {...}}')
        name, args = obj["tool"], obj.get("arguments", {})
        if not isinstance(name, str) or not isinstance(args, dict):
            raise ToolCallError('"tool" must be a string and "arguments" an object')

        allowed_names = set(allowed) if allowed is not None else set(self.registry.names())
        if name not in allowed_names or name not in self.registry:
            raise ToolCallError(f"unknown or disallowed tool {name!r}; allowed: {sorted(allowed_names)}")
        model = self.registry.get(name).args_model
        try:
            parsed = model.model_validate_json(json.dumps(args))
        except ValidationError as exc:
            raise ToolCallError(f"invalid arguments for {name}: {summarise_validation(exc)}") from exc
        return ToolCall(name=name, args=parsed)

    def run(
        self,
        user_message: str | None = None,
        *,
        context: Sequence[Message] | None = None,
        allowed: Iterable[str] | None = None,
    ) -> RouterResult:
        if (user_message is None) == (context is None):
            raise ValueError("pass exactly one of user_message or context")
        allowed_list = list(allowed) if allowed is not None else None
        messages: list[Message] = (
            list(context) if context is not None else [Message(role="user", content=user_message or "")]
        )
        raw = self.llm.complete(self.system_prompt(allowed_list), messages)
        call = self.parse(raw, allowed_list)
        output = self.registry.get(call.name).fn(call.args)
        return RouterResult(call=call, output=output, raw=raw)


def summarise_validation(exc: ValidationError, limit: int = 5) -> str:
    parts = []
    for err in exc.errors()[:limit]:
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)
