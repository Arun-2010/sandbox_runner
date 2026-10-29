from __future__ import annotations

import base64
import json

from agentkit.agents import CodeInterpreterAgent
from agentkit.llm import ScriptedLLM
from agentkit.sandbox.models import Artifact, ExecutionStatus
from tests.sandbox.helpers import FakeSandbox, make_result


def run_call(code: str, files: list[str] | None = None) -> str:
    return json.dumps({"tool": "run_python", "arguments": {"code": code, "files": files or []}})


def answer(text: str) -> str:
    return json.dumps({"tool": "final_answer", "arguments": {"text": text}})


def test_success_path() -> None:
    chart = Artifact(name="chart.png", size=3, mime="image/png", content_b64=base64.b64encode(b"png").decode())
    sandbox = FakeSandbox([make_result(stdout="mean=2.5", artifacts=[chart])])
    llm = ScriptedLLM([run_call("print(df.mean())", ["data.csv"]), answer("The mean is 2.5")])
    agent = CodeInterpreterAgent(llm, sandbox, files={"data.csv": b"x\n1\n4\n"})
    result = agent.run("Average of x?")
    assert result.status == "answered" and result.answer == "The mean is 2.5"
    assert [s.status for s in result.steps] == ["ok", "final_answer"]
    assert result.artifacts[0].name == "chart.png" and "chart.png" in agent.workspace
    assert sandbox.requests[0].files[0].name == "data.csv"
    _, messages = llm.calls[1]
    assert "mean=2.5" in messages[-1]["content"] and "cG5n" not in messages[-1]["content"]


def test_error_then_fix() -> None:
    sandbox = FakeSandbox(
        [
            make_result(ExecutionStatus.ERROR, stderr="Traceback ...\nNameError: name 'pd' is not defined"),
            make_result(stdout="42"),
        ]
    )
    llm = ScriptedLLM([run_call("print(pd)"), run_call("import pandas as pd\nprint(42)"), answer("42")])
    result = CodeInterpreterAgent(llm, sandbox).run("compute")
    assert result.status == "answered" and [s.status for s in result.steps] == ["error", "ok", "final_answer"]
    assert "NameError" in llm.calls[1][1][-1]["content"]


def test_invalid_call_is_fed_back() -> None:
    llm = ScriptedLLM(["sure, here is code", run_call("1", ["/etc/passwd"]), answer("done")])
    result = CodeInterpreterAgent(llm, FakeSandbox()).run("x")
    assert [s.status for s in result.steps] == ["invalid_call", "invalid_call", "final_answer"]
    assert "unknown file" in (result.steps[1].error or "")


def test_step_limit() -> None:
    llm = ScriptedLLM([run_call("print(1)")] * 3)
    result = CodeInterpreterAgent(llm, FakeSandbox(), max_steps=3).run("loop forever")
    assert result.status == "step_limit" and result.answer is None
    assert len(result.steps) == 3 and "step limit" in (result.message or "")
