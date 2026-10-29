"""Token budgeting for prompt context (Project 2).

Why: every piece of text that enters a prompt (retrieved docs, tool output, history) competes
for a finite context window. Components truncate to an explicit token budget instead of
hoping things fit, and `assemble_context` fills a budget by priority.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

TRUNCATION_MARKER = "\n...[truncated]"


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


@dataclass(frozen=True)
class HeuristicCounter:
    """Provider-agnostic estimate (~4 characters per token). Good enough for budgeting."""

    chars_per_token: float = 4.0

    def count(self, text: str) -> int:
        return math.ceil(len(text) / self.chars_per_token) if text else 0


def truncate_to_tokens(
    text: str,
    budget: int,
    counter: TokenCounter | None = None,
    marker: str = TRUNCATION_MARKER,
) -> str:
    """Return the longest prefix of `text` (plus `marker` if cut) that fits in `budget` tokens."""
    counter = counter or HeuristicCounter()
    if budget <= 0:
        return ""
    if counter.count(text) <= budget:
        return text
    if counter.count(marker) > budget:
        marker = ""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if counter.count(text[:mid] + marker) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + marker


@dataclass(frozen=True)
class ContextBlock:
    name: str
    text: str
    priority: int = 0
    min_tokens: int = 1


def assemble_context(
    blocks: Sequence[ContextBlock],
    budget: int,
    counter: TokenCounter | None = None,
    separator: str = "\n\n",
) -> str:
    """Give budget to blocks in priority order (truncating the last one that partly fits),
    drop blocks that cannot get `min_tokens`, and emit survivors in their original order."""
    counter = counter or HeuristicCounter()
    sep_cost = counter.count(separator)
    remaining = budget
    kept: dict[int, str] = {}
    for index in sorted(range(len(blocks)), key=lambda i: -blocks[i].priority):
        block = blocks[index]
        available = remaining - (sep_cost if kept else 0)
        if available < block.min_tokens:
            continue
        text = truncate_to_tokens(block.text, available, counter)
        if not text:
            continue
        kept[index] = text
        remaining = available - counter.count(text)
    return separator.join(kept[i] for i in sorted(kept))
