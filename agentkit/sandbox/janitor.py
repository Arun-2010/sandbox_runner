"""Remove sandbox containers that outlived their run.

Why: `--rm` plus an explicit `docker kill` on timeout cover the normal paths, but a crash of
the host process (or of the Docker CLI) between `docker run` and cleanup can leave a container
behind. Every sandbox container carries the label `agentkit.sandbox=1`, so anything with that
label older than the longest possible run is safe to delete.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from agentkit.sandbox.config import SandboxConfig
from agentkit.sandbox.docker import SANDBOX_LABEL
from agentkit.sandbox.errors import SandboxError, SandboxUnavailableError
from agentkit.sandbox.process import run_cli

logger = logging.getLogger("agentkit.sandbox")
CLI_TIMEOUT_S = 30.0


def parse_docker_timestamp(value: str) -> datetime:
    """Parse Docker's RFC 3339 timestamps, which carry nanoseconds Python cannot represent."""
    value = value.strip().replace("Z", "+00:00")
    if "." in value:
        main, rest = value.split(".", 1)
        digits = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(digits) :]
        value = f"{main}.{digits[:6].ljust(6, '0')}{tz}"
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def default_max_age_s(config: SandboxConfig) -> float:
    return max(600.0, 2 * config.outer_timeout_s)


async def remove_stale_containers(
    config: SandboxConfig | None = None,
    *,
    older_than_s: float | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Delete labelled containers older than `older_than_s`. Returns the removed IDs."""
    config = config or SandboxConfig()
    max_age = default_max_age_s(config) if older_than_s is None else older_than_s
    docker = config.docker_bin
    rc, out, _ = await run_cli(
        [docker, "ps", "-a", "--filter", f"label={SANDBOX_LABEL}", "--format", "{{.ID}}"], CLI_TIMEOUT_S
    )
    if rc != 0:
        raise SandboxUnavailableError("janitor: `docker ps` failed")
    ids = out.split()
    if not ids:
        return []
    # A container can vanish between `ps` and `inspect`; parse whatever inspect printed.
    _, out, _ = await run_cli([docker, "inspect", "--format", "{{.Id}} {{.Created}}", *ids], CLI_TIMEOUT_S)
    now = now or datetime.now(timezone.utc)
    stale = []
    for line in out.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        try:
            created = parse_docker_timestamp(parts[1])
        except ValueError:
            continue
        if (now - created).total_seconds() > max_age:
            stale.append(parts[0])
    if stale:
        rc, _, _ = await run_cli([docker, "rm", "-f", *stale], CLI_TIMEOUT_S)
        logger.info("janitor removed %d stale sandbox container(s) (rc=%s)", len(stale), rc)
    return stale


async def run_janitor_forever(config: SandboxConfig, *, interval_s: float = 300.0) -> None:
    while True:
        try:
            await remove_stale_containers(config)
        except (SandboxError, OSError) as exc:
            logger.warning("janitor pass failed: %s", exc)
        await asyncio.sleep(interval_s)
