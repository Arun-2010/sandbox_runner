"""The minimal LLM interface every agentkit component depends on.

Why: agents, routers and tests should not care which provider produced a completion. A
one-method protocol keeps provider SDKs at the edge, and `ScriptedLLM` makes agent loops
fully deterministic in tests.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Literal, Protocol, TypedDict


class Message(TypedDict):
    role: Literal["user", "assistant"]
    content: str


class LLM(Protocol):
    def complete(self, system: str, messages: Sequence[Message]) -> str: ...


ScriptedResponse = str | Callable[[str, Sequence[Message]], str]


class ScriptedLLMExhausted(RuntimeError):
    """Raised when a ScriptedLLM is asked for more completions than it was given."""


class ScriptedLLM:
    """Returns pre-written responses in order and records every call it receives."""

    def __init__(self, responses: Iterable[ScriptedResponse]) -> None:
        self._responses: list[ScriptedResponse] = list(responses)
        self.calls: list[tuple[str, list[Message]]] = []

    def complete(self, system: str, messages: Sequence[Message]) -> str:
        self.calls.append((system, [Message(role=m["role"], content=m["content"]) for m in messages]))
        if not self._responses:
            raise ScriptedLLMExhausted(f"no scripted response left for call #{len(self.calls)}")
        response = self._responses.pop(0)
        return response(system, messages) if callable(response) else response
