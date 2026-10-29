"""agentkit: the shared package behind every agent-lab project.

Each project adds one production pattern (strict tool routing, context budgeting, sandboxed
execution, ...) as a module here, so later projects can compose earlier ones instead of
re-implementing them.
"""

from __future__ import annotations

from agentkit.context import HeuristicCounter, TokenCounter, truncate_to_tokens
from agentkit.llm import LLM, Message, ScriptedLLM
from agentkit.router import ToolCall, ToolCallError, ToolRouter
from agentkit.tools import Tool, ToolArgs, ToolRegistry

__all__ = [
    "LLM",
    "HeuristicCounter",
    "Message",
    "ScriptedLLM",
    "TokenCounter",
    "Tool",
    "ToolArgs",
    "ToolCall",
    "ToolCallError",
    "ToolRegistry",
    "ToolRouter",
    "truncate_to_tokens",
]
