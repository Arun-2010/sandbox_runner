"""The `run_python` and `final_answer` tools, built on the Project 1 registry.

Why: the model should only be able to name files the agent already holds, never host paths.
`RunPythonArgs` takes file *names*; the tool resolves them against the agent's workspace
mapping, and an unknown name is a validation error the model sees and can correct. Limits
come from the sandbox config so the model is rejected early with a clear message.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ConfigDict, Field, ValidationError, field_validator

from agentkit.router import summarise_validation
from agentkit.sandbox.base import Sandbox
from agentkit.sandbox.config import SandboxConfig
from agentkit.sandbox.formatting import DEFAULT_BUDGET_TOKENS, to_llm_text
from agentkit.sandbox.models import ExecutionRequest, InputFile
from agentkit.tools import Tool, ToolArgs, ToolRegistry

RUN_PYTHON_DESCRIPTION = (
    "Run a Python 3.12 program in an isolated sandbox (numpy, pandas, matplotlib, scipy; no "
    "network, no pip, nothing persists between runs). Listed `files` are copied into the "
    "working directory. Print results to stdout; save files (e.g. charts) to ./output/ to "
    "return them."
)
FINAL_ANSWER_DESCRIPTION = "Finish the task with a final answer for the user."


class RunPythonArgs(ToolArgs):
    code: str = Field(min_length=1, max_length=100_000, description="Complete Python program")
    files: list[str] = Field(default_factory=list, description="Workspace file names to provide")


class FinalAnswerArgs(ToolArgs):
    text: str = Field(min_length=1, max_length=20_000)


def make_run_python_args(config: SandboxConfig, workspace_files: Mapping[str, bytes]) -> type[RunPythonArgs]:
    """Args model bound to this config and workspace. The workspace is checked at validation
    time, so files added to the mapping later become usable."""
    max_chars, max_files = config.max_code_chars, config.max_input_files

    class BoundRunPythonArgs(RunPythonArgs):
        model_config = ConfigDict(title="RunPythonArgs")

        @field_validator("code")
        @classmethod
        def _code_limit(cls, value: str) -> str:
            if len(value) > max_chars:
                raise ValueError(f"code is {len(value)} chars; limit is {max_chars}")
            return value

        @field_validator("files")
        @classmethod
        def _known_files(cls, value: list[str]) -> list[str]:
            if len(value) > max_files:
                raise ValueError(f"at most {max_files} files")
            if len(set(value)) != len(value):
                raise ValueError("duplicate file names")
            unknown = [name for name in value if name not in workspace_files]
            if unknown:
                available = sorted(workspace_files)[:20]
                raise ValueError(f"unknown file(s) {unknown}; available: {available}")
            return value

    return BoundRunPythonArgs


def build_request(args: RunPythonArgs, workspace_files: Mapping[str, bytes]) -> ExecutionRequest:
    return ExecutionRequest(
        code=args.code,
        files=[InputFile.from_bytes(name, workspace_files[name]) for name in args.files],
    )


def register_run_python(
    registry: ToolRegistry,
    sandbox: Sandbox,
    workspace_files: Mapping[str, bytes] | None = None,
    *,
    config: SandboxConfig | None = None,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> Tool:
    files: Mapping[str, bytes] = workspace_files if workspace_files is not None else {}
    cfg = config or getattr(sandbox, "config", None) or SandboxConfig()

    def run_python(args: RunPythonArgs) -> str:
        try:
            request = build_request(args, files)
        except ValidationError as exc:
            return f"status: rejected\nnote: {summarise_validation(exc)}"
        return to_llm_text(sandbox.run_sync(request), budget_tokens)

    return registry.register(
        Tool(
            name="run_python",
            description=RUN_PYTHON_DESCRIPTION,
            args_model=make_run_python_args(cfg, files),
            fn=run_python,
        )
    )


def register_final_answer(registry: ToolRegistry) -> Tool:
    def final_answer(args: FinalAnswerArgs) -> str:
        return args.text

    return registry.register(
        Tool(name="final_answer", description=FINAL_ANSWER_DESCRIPTION, args_model=FinalAnswerArgs, fn=final_answer)
    )
