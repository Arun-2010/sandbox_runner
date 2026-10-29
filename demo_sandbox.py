"""Demo: a code-interpreter agent analyses a CSV and saves a chart, inside the Docker sandbox.

Why it exists: shows the whole Project 4 loop end to end (strict tool calls, sandboxed runs,
budgeted feedback, artifacts) without needing an API key: the "LLM" is scripted, including
one buggy first attempt so you can see the error-then-fix cycle.

    python demo_sandbox.py            # Docker backend (needs the sandbox image)
    python demo_sandbox.py --local    # UNSAFE local backend, no Docker, no isolation
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentkit.agents import CodeInterpreterAgent
from agentkit.llm import ScriptedLLM
from agentkit.sandbox import DockerSandbox, LocalSubprocessSandbox, SandboxUnavailableError

CSV = b"""month,revenue,cost
2024-01,120,80
2024-02,135,82
2024-03,150,90
2024-04,160,95
2024-05,158,97
2024-06,175,99
"""

BUGGY = """import pandas as pd
df = pd.read_csv("sales.csv")
print(df["profit"].mean())
"""

FIXED = """import pandas as pd
import matplotlib.pyplot as plt
df = pd.read_csv("sales.csv")
df["profit"] = df["revenue"] - df["cost"]
print(df.to_string(index=False))
print("mean profit:", round(df["profit"].mean(), 2))
best = df.loc[df["profit"].idxmax()]
print("best month:", best["month"], "profit", best["profit"])
ax = df.plot(x="month", y=["revenue", "cost", "profit"], marker="o", title="Monthly P&L")
ax.figure.tight_layout()
ax.figure.savefig("output/pnl.png", dpi=120)
"""


def call(tool: str, **arguments: object) -> str:
    return json.dumps({"tool": tool, "arguments": arguments})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="use the UNSAFE local backend")
    args = parser.parse_args()

    sandbox = LocalSubprocessSandbox(allow_unsafe=True) if args.local else DockerSandbox()
    try:
        print("sandbox:", sandbox.health_sync())
    except SandboxUnavailableError as exc:
        print(f"sandbox unavailable: {exc}", file=sys.stderr)
        return 1

    llm = ScriptedLLM(
        [
            call("run_python", code=BUGGY, files=["sales.csv"]),
            call("run_python", code=FIXED, files=["sales.csv"]),
            call("final_answer", text="Profit grew from 40 to 76; June was the best month. Chart: pnl.png"),
        ]
    )
    agent = CodeInterpreterAgent(llm, sandbox, files={"sales.csv": CSV})
    result = agent.run("Compute monthly profit from sales.csv, find the best month and chart it.")

    for _system, messages in llm.calls[1:]:
        print("\n--- fed back to the model ---\n" + messages[-1]["content"])
    print("\n=== transcript ===")
    for step in result.steps:
        print(f"step {step.index}: {step.tool or '-'} -> {step.status} ({step.duration_ms or 0} ms)")
    print("\nanswer:", result.answer)
    for artifact in result.artifacts:
        path = Path(artifact.name)
        path.write_bytes(artifact.data())
        print(f"saved artifact {path.resolve()} ({artifact.size} bytes, {artifact.mime})")
    return 0 if result.status == "answered" else 2


if __name__ == "__main__":
    sys.exit(main())
