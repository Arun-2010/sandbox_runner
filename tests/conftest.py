"""Shared fixtures. Tests marked `docker` are skipped (with the reason) when the daemon or the
sandbox image is unavailable, so plain `pytest` stays green on machines without Docker."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess

import pytest

from agentkit.sandbox.config import DEFAULT_IMAGE

IMAGE = os.environ.get("AGENTKIT_SANDBOX_IMAGE", DEFAULT_IMAGE)


@functools.lru_cache(maxsize=1)
def docker_skip_reason() -> str | None:
    if shutil.which("docker") is None:
        return "docker CLI not installed"
    try:
        version = subprocess.run(["docker", "version"], capture_output=True, timeout=20)
        if version.returncode != 0:
            return "Docker daemon not reachable"
        image = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return "Docker daemon not reachable"
    if image.returncode != 0:
        return f"sandbox image {IMAGE} missing (docker build -t {IMAGE} sandbox_image/)"
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    docker_items = [item for item in items if "docker" in item.keywords]
    if not docker_items:
        return
    reason = docker_skip_reason()
    if reason:
        for item in docker_items:
            item.add_marker(pytest.mark.skip(reason=reason))
