"""Strict tool definitions for LLM tool calling (Project 1).

Why: LLM output is untrusted. Every tool declares its arguments as a strict Pydantic model
(`extra="forbid"`, `strict=True`) so a hallucinated field or a wrongly typed value is
rejected before any side effect runs, and the validation error can be fed back to the model.
"""

from __future__ import annotations

import inspect
import json
import typing
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict


class ToolArgs(BaseModel):
    """Base class for tool arguments: unknown fields and type coercion are both errors."""

    model_config = ConfigDict(extra="forbid", strict=True)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[ToolArgs]
    fn: Callable[[Any], str]

    def json_schema(self) -> dict[str, Any]:
        return self.args_model.model_json_schema()


class ToolRegistry:
    """Name -> Tool mapping with a decorator for registration."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def tool(
        self,
        name: str | None = None,
        *,
        description: str | None = None,
        args_model: type[ToolArgs] | None = None,
    ) -> Callable[[Callable[[Any], str]], Callable[[Any], str]]:
        """Register `fn(args: SomeToolArgs) -> str`. The args model is read from the annotation
        unless given explicitly (needed for models built at runtime)."""

        def decorator(fn: Callable[[Any], str]) -> Callable[[Any], str]:
            model = args_model or _infer_args_model(fn)
            self.register(
                Tool(
                    name=name or fn.__name__,
                    description=description or inspect.getdoc(fn) or "",
                    args_model=model,
                    fn=fn,
                )
            )
            return fn

        return decorator

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def names(self) -> list[str]:
        return list(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def describe(self, allowed: Iterable[str] | None = None) -> str:
        """Human/LLM-readable list of tools and their JSON schemas."""
        allowed_set = set(allowed) if allowed is not None else None
        parts = []
        for tool in self:
            if allowed_set is not None and tool.name not in allowed_set:
                continue
            schema = json.dumps(tool.json_schema().get("properties", {}), separators=(",", ":"))
            parts.append(f"- {tool.name}: {tool.description}\n  arguments schema: {schema}")
        return "\n".join(parts)


def _infer_args_model(fn: Callable[..., Any]) -> type[ToolArgs]:
    params = list(inspect.signature(fn).parameters)
    if len(params) != 1:
        raise TypeError(f"tool {fn.__name__!r} must take exactly one ToolArgs parameter")
    model = typing.get_type_hints(fn).get(params[0])
    if not (isinstance(model, type) and issubclass(model, ToolArgs)):
        raise TypeError(f"tool {fn.__name__!r}: parameter must be annotated with a ToolArgs subclass")
    return model
