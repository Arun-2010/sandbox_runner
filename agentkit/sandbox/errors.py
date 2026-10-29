"""Exceptions raised by sandbox backends.

Why: callers need to tell apart "this request is bad" (tell the model), "try again later"
(backpressure), "the sandbox cannot run at all" (fail closed, alert an operator) and
"something broke" (bug or infrastructure fault). Each maps to a distinct HTTP status in the
executor service.
"""

from __future__ import annotations

from typing import Any


class SandboxError(Exception):
    """Base class. `context` holds safe diagnostic fields (never code or file contents)."""

    def __init__(self, message: str, *, run_id: str | None = None, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.context = context or {}


class SandboxUnavailableError(SandboxError):
    """Docker, the image or a required limit is unavailable. Never fall back to running unsafely."""


class SandboxBusyError(SandboxError):
    """The concurrency limiter's queue is full."""

    def __init__(self, message: str, *, retry_after_s: float = 1.0, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after_s = retry_after_s


class RequestRejectedError(SandboxError):
    """The request violates a configured limit (too many files, timeout too long, ...)."""
