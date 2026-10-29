"""The image build context carries its own copy of the runner; it must match the package."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_image_runner_matches_package_runner() -> None:
    package = (ROOT / "agentkit" / "sandbox" / "runner.py").read_bytes().replace(b"\r\n", b"\n")
    image = (ROOT / "sandbox_image" / "runner.py").read_bytes().replace(b"\r\n", b"\n")
    assert image == package, "run: cp agentkit/sandbox/runner.py sandbox_image/runner.py"


def test_runner_is_stdlib_only() -> None:
    source = (ROOT / "agentkit" / "sandbox" / "runner.py").read_text()
    assert "import agentkit" not in source and "from agentkit" not in source
    assert "pydantic" not in source
