"""Sandbox runner: the trusted half of the code that runs inside the sandbox.

Why it exists: the container gives isolation, but something inside it must turn a JSON
payload into files, run the untrusted program as a child with an inner timeout and rlimits,
capture its output with byte caps, collect artifacts without following links, clean up every
process the program left behind, and report one JSON line. Keeping that logic in one
standard-library-only script means the same file runs in the Docker image and in the local
(unsafe) backend used by tests.

The host treats everything this script prints as untrusted, because the child runs as the
same UID and may try to interfere with it. This script narrows that window (it marks itself
non-dumpable so the child cannot open its /proc entries, and in container mode it kills every
other process before collecting artifacts), but the security boundary is the container.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

PROTOCOL_VERSION = 1
DEFAULT_WORKSPACE = "/workspace"
DEFAULT_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
MPL_CACHE_SEED = "/opt/mplcache"
READ_CHUNK = 64 * 1024
READER_JOIN_S = 2.0
MAX_SKIPPED = 50
NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
RESERVED = frozenset({".", "..", "main.py", "output"})
CHILD_ENV_KEYS = (
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "MPLBACKEND", "MPLCONFIGDIR",
    "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "SYSTEMROOT", "TEMP", "TMP",
)  # fmt: skip
LIMIT_KEYS = (
    "max_stdout_bytes", "max_stderr_bytes", "max_artifacts", "max_artifact_bytes",
    "max_total_artifact_bytes", "max_file_bytes", "max_open_files", "cpu_seconds",
    "max_address_space_bytes",
)  # fmt: skip
POSIX = os.name == "posix"
SIGKILL = getattr(signal, "SIGKILL", 9)
SIGXCPU = getattr(signal, "SIGXCPU", None)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
O_BINARY = getattr(os, "O_BINARY", 0)


class RunnerError(Exception):
    """Bad payload or environment. Reported on stderr with exit status 2."""


def safe_name(name: object) -> bool:
    return isinstance(name, str) and NAME_RE.fullmatch(name) is not None and name not in RESERVED


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RunnerError(message)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------- payload


def read_payload(stream, max_bytes: int) -> dict:
    data = stream.read(max_bytes + 1)
    _require(len(data) <= max_bytes, "payload too large")
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RunnerError("payload is not valid JSON") from exc
    return validate_payload(payload)


def validate_payload(payload: object) -> dict:
    _require(isinstance(payload, dict), "payload must be an object")
    _require(payload.get("version") == PROTOCOL_VERSION, "unsupported payload version")
    nonce, code = payload.get("nonce"), payload.get("code")
    _require(isinstance(nonce, str) and 16 <= len(nonce) <= 128, "invalid nonce")
    _require(isinstance(code, str) and code != "", "code must be a non-empty string")
    timeout_s = payload.get("timeout_s")
    _require(
        isinstance(timeout_s, (int, float)) and not isinstance(timeout_s, bool) and timeout_s > 0,
        "timeout_s must be a positive number",
    )
    limits = payload.get("limits")
    _require(isinstance(limits, dict), "limits must be an object")
    for key in LIMIT_KEYS:
        _require(_is_int(limits.get(key)) and limits[key] >= 0, f"invalid limit {key}")

    raw_files = payload.get("files")
    _require(isinstance(raw_files, list), "files must be a list")
    files = []
    seen = set()
    for item in raw_files:
        _require(isinstance(item, dict), "file entries must be objects")
        name, content = item.get("name"), item.get("content_b64")
        _require(safe_name(name) and name not in seen, "invalid or duplicate file name")
        _require(isinstance(content, str), "content_b64 must be a string")
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RunnerError("invalid base64 in input file") from exc
        seen.add(name)
        files.append((name, data))
    return {
        "nonce": nonce,
        "code": code,
        "files": files,
        "timeout_s": float(timeout_s),
        "limits": {k: limits[k] for k in LIMIT_KEYS},
    }


def _write_new_file(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | O_BINARY, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def prepare_workspace(workspace: str, code: str, files: list) -> str:
    os.makedirs(workspace, exist_ok=True)
    _write_new_file(os.path.join(workspace, "main.py"), code.encode("utf-8"))
    for name, data in files:
        _require(safe_name(name) and os.sep not in name and (os.altsep or "/") not in name, "bad name")
        _write_new_file(os.path.join(workspace, name), data)
    output_dir = os.path.join(workspace, "output")
    os.mkdir(output_dir, 0o700)
    return output_dir


# --------------------------------------------------------------------------- child process


class CappedReader(threading.Thread):
    """Drains a pipe completely, keeping only the first and last `cap / 2` bytes."""

    def __init__(self, stream, cap: int) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.head_cap = cap // 2
        self.tail_cap = cap - self.head_cap
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def run(self) -> None:
        try:
            while True:
                chunk = self.stream.read(READ_CHUNK)
                if not chunk:
                    return
                self.total += len(chunk)
                room = self.head_cap - len(self.head)
                if room > 0:
                    self.head += chunk[:room]
                    chunk = chunk[room:]
                if chunk:
                    self.tail += chunk
                    if len(self.tail) > self.tail_cap:
                        del self.tail[: len(self.tail) - self.tail_cap]
        except (OSError, ValueError):
            return

    @property
    def truncated(self) -> bool:
        return self.total > len(self.head) + len(self.tail)

    def text(self) -> str:
        head = bytes(self.head).decode("utf-8", "replace")
        tail = bytes(self.tail).decode("utf-8", "replace")
        if not self.truncated:
            return head + tail
        omitted = self.total - len(self.head) - len(self.tail)
        return f"{head}\n...[{omitted} bytes truncated]...\n{tail}"


def _lower_rlimit(resource, which: int, soft: int, hard: int | None = None) -> None:
    hard = soft if hard is None else hard
    _, cur_hard = resource.getrlimit(which)
    if cur_hard != resource.RLIM_INFINITY:
        hard = min(hard, cur_hard)
    resource.setrlimit(which, (min(soft, hard), hard))


def _make_preexec(limits: dict, container: bool):
    """rlimits for the child (POSIX only). Errors propagate so the run fails closed."""
    if not POSIX:
        return None
    import resource

    def preexec() -> None:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        _lower_rlimit(resource, resource.RLIMIT_CPU, limits["cpu_seconds"], limits["cpu_seconds"] + 1)
        _lower_rlimit(resource, resource.RLIMIT_FSIZE, limits["max_file_bytes"])
        _lower_rlimit(resource, resource.RLIMIT_NOFILE, limits["max_open_files"])
        if container:
            # Huge allocations fail fast with MemoryError instead of thrashing the cgroup until
            # the OOM killer fires. Container only: macOS rejects RLIMIT_AS.
            _lower_rlimit(resource, resource.RLIMIT_AS, limits["max_address_space_bytes"])
        try:  # make the child, not the runner, the OOM killer's first choice
            with open("/proc/self/oom_score_adj", "w") as fh:
                fh.write("1000")
        except OSError:
            pass

    return preexec


def _kill_group(proc: subprocess.Popen) -> None:
    if POSIX:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, SIGKILL)
    else:
        with contextlib.suppress(OSError):
            proc.kill()


def _kill_everything_else() -> None:
    """Container mode only: SIGKILL every process we may signal (Linux skips PID 1 and the
    caller). Catches children that escaped the process group with setsid()."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(-1, SIGKILL)


def _harden_self() -> None:
    """PR_SET_DUMPABLE=0: the same-UID child can no longer open /proc/<runner>/{fd,mem,environ},
    so it cannot write into the runner's stdout or read the nonce from memory."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def run_child(workspace: str, timeout_s: float, limits: dict, container: bool) -> dict:
    env = {k: os.environ[k] for k in CHILD_ENV_KEYS if k in os.environ}
    kwargs: dict = {
        "cwd": workspace,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "bufsize": 0,
        "env": env,
        "close_fds": True,
    }
    if POSIX:
        kwargs["start_new_session"] = True
        kwargs["preexec_fn"] = _make_preexec(limits, container)
    # -I ignores PYTHON* env vars, so unbuffered output and no-bytecode are passed as flags.
    cmd = [sys.executable, "-I", "-u", "-B", "main.py"]

    start = time.monotonic()
    proc = subprocess.Popen(cmd, **kwargs)
    out = CappedReader(proc.stdout, limits["max_stdout_bytes"])
    err = CappedReader(proc.stderr, limits["max_stderr_bytes"])
    out.start()
    err.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        proc.wait()
    duration_ms = int((time.monotonic() - start) * 1000)
    _kill_group(proc)  # background processes left in the child's session
    if container:
        _kill_everything_else()
    out.join(READER_JOIN_S)
    err.join(READER_JOIN_S)

    rc = proc.returncode
    sig = -rc if rc < 0 else None
    if sig is not None and sig == SIGXCPU:
        timed_out = True
    stderr_text = err.text()
    oom_hint = not timed_out and (sig == SIGKILL or "MemoryError" in stderr_text[-4000:])
    return {
        "exit_code": rc if rc >= 0 else 128 + sig,
        "signal": sig,
        "timed_out": timed_out,
        "oom_hint": oom_hint,
        "stdout": out.text(),
        "stderr": stderr_text,
        "stdout_truncated": out.truncated,
        "stderr_truncated": err.truncated,
        "stdout_bytes": out.total,
        "stderr_bytes": err.total,
        "duration_ms": duration_ms,
    }


# --------------------------------------------------------------------------- artifacts


def _read_regular_file(path: str, expected: os.stat_result, limit: int) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_BINARY)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_ino, st.st_dev) != (expected.st_ino, expected.st_dev):
            return None
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, min(READ_CHUNK, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks) if size <= limit else None
    finally:
        os.close(fd)


def collect_artifacts(output_dir: str, limits: dict) -> tuple[list, list]:
    artifacts: list = []
    skipped: list = []

    def skip(name: str) -> None:
        if len(skipped) < MAX_SKIPPED:
            skipped.append(name if safe_name(name) else "<invalid name>")

    try:
        names = sorted(os.listdir(output_dir))
    except OSError:
        return artifacts, skipped
    total = 0
    for name in names:
        path = os.path.join(output_dir, name)
        if not safe_name(name) or len(artifacts) >= limits["max_artifacts"]:
            skip(name)
            continue
        try:
            st = os.lstat(path)
        except OSError:
            skip(name)
            continue
        # Regular files only (no symlinks, dirs, FIFOs, devices), and no hard links, which
        # could otherwise point at a file outside the output directory.
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            skip(name)
            continue
        if st.st_size > limits["max_artifact_bytes"] or total + st.st_size > limits["max_total_artifact_bytes"]:
            skip(name)
            continue
        data = _read_regular_file(path, st, limits["max_artifact_bytes"])
        if data is None or total + len(data) > limits["max_total_artifact_bytes"]:
            skip(name)
            continue
        total += len(data)
        artifacts.append({"name": name, "size": len(data), "content_b64": base64.b64encode(data).decode("ascii")})
    return artifacts, skipped


# --------------------------------------------------------------------------- entry point


def _seed_matplotlib_cache() -> None:
    """Copy the font cache built into the image into the (empty, tmpfs) MPLCONFIGDIR, so each
    run does not spend ~1.5 s rebuilding it. Best effort: matplotlib rebuilds it if missing."""
    target = os.environ.get("MPLCONFIGDIR")
    if not target or not os.path.isdir(MPL_CACHE_SEED):
        return
    try:
        os.makedirs(target, mode=0o700, exist_ok=True)
        for name in os.listdir(MPL_CACHE_SEED):
            src = os.path.join(MPL_CACHE_SEED, name)
            if os.path.isfile(src) and not os.path.islink(src):
                shutil.copyfile(src, os.path.join(target, name))
    except OSError:
        pass


def execute(payload: dict, workspace: str, container: bool) -> dict:
    if container:
        _seed_matplotlib_cache()
    output_dir = prepare_workspace(workspace, payload["code"], payload["files"])
    result = run_child(workspace, payload["timeout_s"], payload["limits"], container)
    artifacts, skipped = collect_artifacts(output_dir, payload["limits"])
    result["artifacts"] = artifacts
    result["skipped_artifacts"] = skipped
    return result


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="agentkit sandbox runner")
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    parser.add_argument("--container", action="store_true", help="kill all other processes after the run")
    parser.add_argument("--max-payload-bytes", type=int, default=DEFAULT_MAX_PAYLOAD_BYTES)
    args = parser.parse_args(argv)

    _harden_self()
    try:
        payload = read_payload(sys.stdin.buffer, args.max_payload_bytes)
        result = execute(payload, args.workspace, args.container)
    except RunnerError as exc:
        sys.stderr.write(f"runner error: {exc}\n")
        return 2
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"runner error: {type(exc).__name__}\n")
        return 2
    line = json.dumps({"nonce": payload["nonce"], "result": result}, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.buffer.write(line.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
