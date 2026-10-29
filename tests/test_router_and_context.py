from __future__ import annotations

import pytest
from pydantic import Field

from agentkit.context import ContextBlock, HeuristicCounter, assemble_context, truncate_to_tokens
from agentkit.llm import ScriptedLLM
from agentkit.router import ToolCallError, ToolRouter
from agentkit.tools import ToolArgs, ToolRegistry


class AddArgs(ToolArgs):
    a: int
    b: int = Field(ge=0)


def make_router(responses: list[str]) -> ToolRouter:
    registry = ToolRegistry()

    @registry.tool()
    def add(args: AddArgs) -> str:
        """Add two numbers."""
        return str(args.a + args.b)

    return ToolRouter(ScriptedLLM(responses), registry)


def test_parse_and_run() -> None:
    router = make_router(['```json\n{"tool": "add", "arguments": {"a": 2, "b": 3}}\n```'])
    assert router.run("2+3?").output == "5"


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        '{"tool": "nope", "arguments": {}}',
        '{"tool": "add", "arguments": {"a": "2", "b": 3}}',
        '{"tool": "add", "arguments": {"a": 2, "b": 3, "c": 1}}',
        '{"tool": "add", "arguments": {"a": 2, "b": -1}}',
        '{"tool": "add", "arguments": {"a": 1, "b": 1}, "extra": true}',
    ],
)
def test_parse_rejects(raw: str) -> None:
    with pytest.raises(ToolCallError):
        make_router([]).parse(raw)


def test_parse_respects_allowed() -> None:
    with pytest.raises(ToolCallError, match="disallowed"):
        make_router([]).parse('{"tool": "add", "arguments": {"a": 1, "b": 1}}', allowed=[])


def test_truncate_to_tokens() -> None:
    counter = HeuristicCounter()
    text = "x" * 1000
    out = truncate_to_tokens(text, 50, counter)
    assert counter.count(out) <= 50 and out.endswith("[truncated]")
    assert truncate_to_tokens("short", 50) == "short"
    assert truncate_to_tokens("abc", 0) == ""


def test_assemble_context_priority() -> None:
    blocks = [ContextBlock("low", "l" * 400, priority=0), ContextBlock("high", "h" * 400, priority=10)]
    out = assemble_context(blocks, budget=120)
    assert out.startswith("l") and "h" * 400 in out
