from __future__ import annotations

import base64

from agentkit.context import HeuristicCounter
from agentkit.router import ToolCallError, ToolRouter
from agentkit.sandbox.formatting import tail_to_tokens, to_llm_text
from agentkit.sandbox.models import Artifact, ExecutionStatus
from agentkit.sandbox.tool import register_final_answer, register_run_python
from agentkit.tools import ToolRegistry
from tests.sandbox.helpers import FakeSandbox, make_result

COUNTER = HeuristicCounter()


def big_traceback() -> str:
    frames = "".join(f'  File "main.py", line {i}, in f{i}\n    f{i + 1}()\n' for i in range(300))
    return "Traceback (most recent call last):\n" + frames + "ZeroDivisionError: division by zero\n"


def test_formatter_respects_budget_and_keeps_traceback_tail() -> None:
    result = make_result(ExecutionStatus.ERROR, stdout="line\n" * 5000, stderr=big_traceback())
    text = to_llm_text(result, budget_tokens=300)
    assert COUNTER.count(text) <= 300
    assert "ZeroDivisionError: division by zero" in text
    assert "status: error" in text and "exit code: 1" in text


def test_formatter_never_includes_base64() -> None:
    payload = base64.b64encode(b"\x89PNG" + b"z" * 3000).decode()
    artifact = Artifact(name="chart.png", size=3004, mime="image/png", content_b64=payload)
    text = to_llm_text(make_result(stdout="done", artifacts=[artifact]), budget_tokens=500)
    assert "chart.png" in text and "2.9 KB" in text and payload[:40] not in text


def test_tail_to_tokens() -> None:
    out = tail_to_tokens("a\n" * 1000 + "LAST", 20, COUNTER)
    assert out.endswith("LAST") and COUNTER.count(out) <= 20
    assert tail_to_tokens("x" * 1000, 10, COUNTER).endswith("x")


def test_formatter_tiny_budget() -> None:
    text = to_llm_text(make_result(stdout="x" * 1000), budget_tokens=10)
    assert COUNTER.count(text) <= 10


def make_registry(files: dict[str, bytes], sandbox: FakeSandbox) -> ToolRegistry:
    registry = ToolRegistry()
    register_run_python(registry, sandbox, files)
    register_final_answer(registry)
    return registry


def test_run_python_tool_resolves_files() -> None:
    sandbox = FakeSandbox([make_result(stdout="3 rows")])
    registry = make_registry({"data.csv": b"a\n1\n"}, sandbox)
    router = ToolRouter(None, registry)  # type: ignore[arg-type]
    call = router.parse('{"tool": "run_python", "arguments": {"code": "print(1)", "files": ["data.csv"]}}')
    output = registry.get("run_python").fn(call.args)
    assert "3 rows" in output and sandbox.requests[0].files[0].data() == b"a\n1\n"


def test_unknown_file_fails_validation() -> None:
    registry = make_registry({"data.csv": b""}, FakeSandbox())
    router = ToolRouter(None, registry)  # type: ignore[arg-type]
    for name in ("missing.csv", "/etc/passwd", "../data.csv"):
        try:
            router.parse(f'{{"tool": "run_python", "arguments": {{"code": "1", "files": ["{name}"]}}}}')
        except ToolCallError as exc:
            assert "unknown file" in str(exc)
        else:
            raise AssertionError(f"{name} accepted")
