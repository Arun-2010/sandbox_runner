# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Repository overview

`agent-lab` builds production patterns for LLM agents one project at a time. All code lives in
the `agentkit` package so the projects compose into one system.

| Project | Module | Status |
|---|---|---|
| 1. Strict Pydantic Tool Router | `agentkit/tools.py`, `agentkit/router.py`, `agentkit/llm.py` | done |
| 2. Dynamic Context Budget Assembler | `agentkit/context.py` | done |
| 4. Sandboxed Code Executor | `agentkit/sandbox/`, `agentkit/agents/`, `services/executor/`, `sandbox_image/` | **current task** |

Existing APIs you must reuse rather than reimplement:

- `ToolArgs` (strict Pydantic base, `extra="forbid"`, `strict=True`) and `ToolRegistry` / `@registry.tool()`.
- `ToolRouter(llm, registry).parse(raw, allowed)` and `.run(user_message | context=...)`.
- `LLM` protocol (`complete(system, messages) -> str`) and `ScriptedLLM` for deterministic tests.
- `truncate_to_tokens(text, budget, counter)` and `HeuristicCounter` from `agentkit/context.py`.

Do not change public behaviour of Projects 1 and 2. All existing tests must keep passing.

## Commands

```bash
pip install -e ".[dev,sandbox,service]"          # sandbox/service extras are added by this task
pytest                                           # unit tests; Docker tests auto-skip if Docker is unavailable
pytest -m docker                                 # integration + attack tests (needs Docker and the sandbox image)
docker build -t agentkit-sandbox:dev sandbox_image/    # build the sandbox image
python demo_sandbox.py                           # code-interpreter agent demo
python demo_attacks.py                           # runs each attack and shows it being blocked
uvicorn services.executor.app:app --port 8080    # executor service (dev)
ruff check . && ruff format --check .
```

## Conventions

- Python 3.10+, full type hints, `from __future__ import annotations`.
- Pydantic v2 for every data model that crosses a boundary (config, requests, results, payloads).
- No bare `except:`. Catch specific exceptions; anything unexpected becomes `SandboxError` with context.
- Standard library only inside the sandbox runner (`agentkit/sandbox/runner.py`). It runs inside the image.
- Every module gets a top docstring explaining *why* it exists, not just what it does.
- Tests: `pytest`, parametrize failure cases, mark Docker-dependent tests with `@pytest.mark.docker`
  and skip them cleanly (with a reason) when Docker or the image is missing.
- Keep the README project table and layout section up to date when you add modules.

---

# Current task: Project 4, Sandboxed Code Executor (production-ready, Docker, no E2B)

## Goal

Let an agent write Python and run it safely. LLM-written code is **untrusted**: it may be buggy,
or hijacked by prompt injection hidden in files or web pages the agent reads. It must never be able
to read host files, reach the network, exhaust host resources, persist between runs, or forge results.

## Threat model (write this up in `docs/SECURITY.md`)

Attacker capability: arbitrary Python code inside the sandbox, plus control of every byte the
sandbox returns. Assets to protect: host filesystem, host secrets/env vars, network, host CPU/RAM/disk,
other users' runs, the integrity of the agent process.

Rules that follow from this:

1. **Isolation is enforced by the container runtime, never by inspecting code.** An optional AST
   "lint" may warn the LLM about obviously useless code (e.g. `pip install` with no network), but it is
   UX only and must be documented as *not a security control*.
2. **Everything coming out of the sandbox is untrusted.** Validate the runner's result with Pydantic,
   cap every size, sanitise artifact names. Untrusted code runs as the same UID as the runner and could
   overwrite its output.
3. **Defence in depth.** Several independent layers (no network, read-only root, non-root user, dropped
   capabilities, resource limits, timeouts, fresh container per run). One failure must not be fatal.
4. **Fail closed.** If a limit cannot be applied, or Docker/the image is unavailable, refuse to run.
   Never fall back silently to the unsafe local backend.
5. **One container per execution, destroyed afterwards.** Never reuse a container across executions,
   even for the same user.

## Architecture

```
agent / router ──► run_python tool ──► Sandbox (interface)
                                         ├── DockerSandbox        (production default)
                                         ├── RemoteSandbox        (HTTP client for the executor service)
                                         └── LocalSubprocessSandbox (UNSAFE, dev/tests only)

DockerSandbox.run(request)
  1. validate request (sizes, file names)            host side
  2. acquire concurrency slot (semaphore + bounded queue)
  3. docker run --rm -i <hardened flags> IMAGE       payload JSON on stdin
        └─ /opt/runner/runner.py (inside container)
             - writes code + input files into /workspace (tmpfs)
             - runs `python main.py` as a child with inner timeout + rlimits
             - captures stdout/stderr with caps (head + tail)
             - collects /workspace/output/* (regular files only, no symlinks, size caps)
             - prints ONE JSON result line to its own stdout
  4. outer wall-clock timeout -> `docker kill`; always remove the container
  5. read at most MAX_RESULT_BYTES of stdout, validate with Pydantic, detect OOM / timeout
  6. return ExecutionResult
```

### Why stdin payload via the Docker CLI (decision already made, keep it)

- Root filesystem is read-only and `/workspace` is a tmpfs. `docker cp` / `put_archive` cannot
  reliably write into a tmpfs, and bind mounts expose host paths. So inputs travel on stdin.
- `docker run -i` with stdin works the same on Linux, macOS and Windows (Docker Desktop) and avoids
  the Docker SDK's stdin socket multiplexing. Use `asyncio.create_subprocess_exec` (never `shell=True`),
  pass arguments as a list.
- Give each container a unique `--name` (`agentkit-sbx-<run_id>`) so the timeout path can `docker kill` it.

## Package layout to create

```
agentkit/sandbox/
  __init__.py        public exports
  config.py          SandboxConfig (limits, image, runtime) loadable from env vars (AGENTKIT_SANDBOX_*)
  models.py          InputFile, ExecutionRequest, Artifact, ExecutionResult, ExecutionStatus,
                     RunnerPayload, RunnerResult (the wire format between host and runner)
  errors.py          SandboxError, SandboxUnavailableError, SandboxBusyError, RequestRejectedError
  base.py            Sandbox protocol: async run(request) -> ExecutionResult; run_sync wrapper; health()
  runner.py          stdlib-only runner executed INSIDE the sandbox (also used by the local backend)
  docker.py          DockerSandbox: hardened `docker run`, timeouts, kill, OOM detection, result parsing
  local.py           LocalSubprocessSandbox: runs runner.py in a subprocess; refuses unless allow_unsafe=True
  limiter.py         ConcurrencyLimiter: semaphore + max queue length -> SandboxBusyError (backpressure)
  janitor.py         remove leftover containers labelled agentkit.sandbox=1 older than N minutes
  remote.py          RemoteSandbox: httpx client for the executor service (same interface)
  formatting.py      ExecutionResult -> compact text for the LLM, within a token budget
  tool.py            RunPythonArgs(ToolArgs) + register_run_python(registry, sandbox)
agentkit/agents/
  __init__.py
  code_interpreter.py  CodeInterpreterAgent: multi-step loop using ToolRouter for validation
services/executor/
  app.py             FastAPI executor service
  Dockerfile         image for the service itself
  docker-compose.yml service + pre-built sandbox image
sandbox_image/
  Dockerfile         the sandbox image
  requirements.txt   pinned data libraries
docs/SECURITY.md     threat model, layers, known limitations, deployment guidance
demo_sandbox.py
demo_attacks.py
tests/sandbox/       unit, runner, docker integration, attack, service, agent tests
```

Add optional dependency groups in `pyproject.toml`:
`sandbox = []` (CLI only, nothing needed), `service = ["fastapi", "uvicorn", "httpx"]`,
and add `ruff` to `dev`. Register the `docker` pytest marker in `pyproject.toml`.

## Data models (`models.py`)

- `InputFile`: `name` (regex `^[A-Za-z0-9._-]{1,100}$`, not `.`/`..`), `content_b64` (validated base64).
- `ExecutionRequest`: `code` (1..100_000 chars), `files: list[InputFile]` (max count/total size from config),
  optional `timeout_s` (cannot exceed config max).
- `ExecutionStatus` enum: `ok`, `error` (non-zero exit / exception), `timeout`, `oom`, `output_limit`,
  `rejected` (request failed validation), `sandbox_error` (infrastructure failure).
- `Artifact`: `name`, `size`, `mime` (guessed from extension, allow-list), `content_b64`.
- `ExecutionResult`: `run_id`, `status`, `exit_code`, `stdout`, `stderr`, `stdout_truncated`,
  `stderr_truncated`, `artifacts`, `duration_ms`, `backend`, `limits` (the limits actually applied).
- All models `extra="forbid"`. `RunnerResult` is validated strictly on the host because it is untrusted.

## Config defaults (`config.py`)

| Setting | Default | Notes |
|---|---|---|
| `image` | `agentkit-sandbox:dev` | production: pin by digest (`name@sha256:...`) |
| `runtime` | `None` | set `"runsc"` to use gVisor when installed |
| `timeout_s` / `max_timeout_s` | 10 / 60 | inner timeout in runner; outer = timeout + `startup_grace_s` (5) |
| `memory_mb` | 512 | also set `--memory-swap` to the same value (no swap) |
| `cpus` | 1.0 | |
| `pids_limit` | 64 | stops fork bombs |
| `workspace_mb` / `tmp_mb` | 64 / 32 | tmpfs sizes |
| `max_code_chars` | 100_000 | |
| `max_input_files` / `max_input_bytes` | 10 / 20 MB | |
| `max_stdout_bytes` / `max_stderr_bytes` | 64 KB each | keep head and tail, mark truncated |
| `max_artifacts` / `max_artifact_bytes` / `max_total_artifact_bytes` | 10 / 5 MB / 10 MB | |
| `max_result_bytes` | 20 MB | hard cap on what the host reads from the container |
| `max_concurrency` / `max_queue` | 4 / 16 | |

## Hardened `docker run` flags (all required; never weaken them to make a test pass)

```
docker run --rm -i
  --name agentkit-sbx-<run_id> --label agentkit.sandbox=1 --label agentkit.run_id=<run_id>
  --pull never --init
  --network none
  --read-only
  --tmpfs /workspace:rw,nosuid,nodev,size=<workspace_mb>m,uid=10001,gid=10001,mode=0700
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=<tmp_mb>m,uid=10001,gid=10001,mode=0700
  --user 10001:10001
  --cap-drop ALL
  --security-opt no-new-privileges
  --memory <m>m --memory-swap <m>m --cpus <c> --pids-limit <p>
  --ulimit nofile=256:256 --ulimit nproc=<p>:<p>
  --hostname sandbox --workdir /workspace
  [--runtime runsc]
  <image>
```

Never: `--privileged`, host network/PID/IPC namespaces, any `-v`/bind mount (especially the Docker
socket), `--env-file`, or passing host environment variables. Keep Docker's default seccomp profile.
Only these env vars inside: `HOME=/tmp`, `MPLCONFIGDIR=/tmp/mpl`, `PYTHONDONTWRITEBYTECODE=1`,
`PYTHONUNBUFFERED=1`.

## Sandbox image (`sandbox_image/Dockerfile`)

- Base `python:3.12-slim` (document how to pin by digest).
- `pip install --no-cache-dir -r requirements.txt` with pinned versions: numpy, pandas, matplotlib, scipy.
  Then uninstall pip and remove setuid/setgid bits (`find / -xdev -perm /6000 -type f -exec chmod a-s {} +`).
- Create user `sandbox` uid/gid 10001, no shell login, no home dir writes outside tmpfs.
- Copy `agentkit/sandbox/runner.py` to `/opt/runner/runner.py`, owned by root, mode 0444.
- `ENTRYPOINT ["python", "-I", "/opt/runner/runner.py"]`, `USER 10001:10001`.
- Matplotlib must use the `Agg` backend (set `MPLBACKEND=Agg`).

## Runner (`runner.py`, stdlib only)

1. Read the whole payload from stdin (refuse > max payload), parse JSON, validate the fields it needs.
2. Write `main.py` and input files into `/workspace` (refuse names with path separators), create `/workspace/output`.
3. Run `python -I main.py` as a child with `subprocess.Popen`, `start_new_session=True`, a `preexec_fn`
   setting `RLIMIT_CPU`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE`, `RLIMIT_CORE=0` (POSIX only; skip gracefully on Windows
   for the local backend).
4. Read stdout/stderr concurrently (threads) with byte caps, keeping first and last halves.
   On inner timeout kill the whole process group (`os.killpg`, SIGKILL).
5. Collect artifacts from `/workspace/output`: `os.lstat`, regular files only, skip symlinks and
   directories, enforce count and size caps, never follow links (use `O_NOFOLLOW` where available).
6. Detect `MemoryError` / signal 9 for OOM hints, then print exactly one JSON line and exit 0.
   Runner bugs exit non-zero with a short message on stderr.

The same `runner.py` is used by `LocalSubprocessSandbox` (with a temp dir instead of `/workspace`,
passed by argument), which lets most of the logic be tested without Docker.

## DockerSandbox details

- `health()`: `docker version` succeeds and `docker image inspect <image>` finds the image; otherwise
  `SandboxUnavailableError` with an actionable message ("run: docker build -t ... sandbox_image/").
- Outer timeout: on expiry run `docker kill <name>` (then `docker rm -f` as a fallback), status `timeout`.
- OOM: exit code 137 without a timeout, or runner reported `MemoryError`, gives status `oom`. Inspecting
  `State.OOMKilled` is not possible after `--rm`; document this choice.
- Never read unbounded output: read the subprocess stdout in chunks up to `max_result_bytes`, then kill.
- If the runner's JSON is missing or invalid, return status `sandbox_error` with a safe message, never raw output.
- Log one structured line per run (run_id, status, exit_code, duration_ms, bytes in/out, backend). No code or file
  contents in logs.
- The concurrency limiter wraps `run`. When the queue is full raise `SandboxBusyError` immediately.

## Tool and agent integration

- `RunPythonArgs(ToolArgs)`: `code: str` (min 1, max from config), `files: list[str]` (names of files the
  agent's workspace already holds; the tool resolves them to `InputFile`s; unknown names fail validation).
- `register_run_python(registry, sandbox, workspace_files)` registers the tool. The tool returns
  `formatting.to_llm_text(result, budget_tokens)`:
  status line, exit code, trimmed stdout, the *last* lines of the traceback (most useful part), and
  artifact names/sizes (never base64 into the prompt). Use `truncate_to_tokens`.
- Add a `final_answer(text)` tool for the agent loop.
- `CodeInterpreterAgent(llm, sandbox, max_steps=6)`: each step the LLM must call `run_python` or
  `final_answer` (validated by `ToolRouter.parse`); execution results are appended as user messages;
  stop on `final_answer` or `max_steps` (then return a clear "step limit reached" result).
  Keep a transcript of steps (code, status, duration) on the result object for later tracing.

## Executor service (`services/executor/app.py`)

Why: whoever can talk to the Docker daemon is effectively root on that host. In production the agent
process must not hold the Docker socket. Run the executor on a dedicated VM/host and call it over HTTP.

- `POST /v1/executions` (body `ExecutionRequest`) returns `ExecutionResult`; `GET /healthz` (process up);
  `GET /readyz` (Docker reachable and image present).
- Auth: `Authorization: Bearer <token>` from env `AGENTKIT_EXECUTOR_TOKEN`; compare with `hmac.compare_digest`;
  refuse to start without a token unless `AGENTKIT_EXECUTOR_DEV=1`.
- Reject bodies over the configured size (413), queue full (429 with `Retry-After`), unavailable (503).
- Run the janitor on startup and periodically.
- `RemoteSandbox` (httpx) implements the same `Sandbox` interface, with timeouts and retries only on
  connection errors (never retry a request that may have executed).
- `docker-compose.yml` documents mounting the Docker socket into the *executor service only*, with a
  comment explaining the risk and recommending a dedicated host or rootless Docker.

## Tests (required)

Unit (no Docker):
- request validation: bad file names (`../x`, `a/b`, empty, too long), too many/large files, oversized code, bad base64.
- runner via `LocalSubprocessSandbox(allow_unsafe=True)`: hello world, exception traceback, inner timeout,
  stdout cap with head+tail, artifacts collected, symlink in output skipped, too many/large artifacts.
- host-side result validation: garbage/missing/oversized runner output -> `sandbox_error`.
- `LocalSubprocessSandbox` refuses without `allow_unsafe=True`.
- docker command builder: snapshot test that every required flag is present and forbidden ones are absent.
- limiter: concurrency respected, queue overflow raises `SandboxBusyError`.
- formatter: respects token budget, keeps traceback tail, no base64.
- agent loop with `ScriptedLLM` + fake sandbox: success path, error then fix, step limit.
- service with FastAPI `TestClient` + fake sandbox: auth required, 413, 429, happy path.

Attack suite (`@pytest.mark.docker`), each must be blocked and produce the expected status:

| Attack | Expected |
|---|---|
| read host files (`/etc/hostname` differs from host, no home dir, `/var/run/docker.sock` absent) | not visible |
| read host env vars (set a canary env var on the host before running) | canary absent |
| network: DNS lookup, HTTP request, raw socket connect | fails |
| `while True: pass` | `timeout`, wall time < timeout + grace + small slack |
| allocate 2 GB | `oom` |
| fork bomb (`os.fork` loop) | contained, run ends, host fine |
| fill disk (write 1 GB to /workspace and /tmp) | write fails (tmpfs size), run ends |
| print 100 MB | `stdout_truncated`, host memory bounded |
| write to `/usr`, `/etc`, `/opt/runner` | permission denied / read-only |
| escalate (`os.setuid(0)`, check capabilities in `/proc/self/status` are 0) | fails |
| persistence: write file in run 1, look for it in run 2 | absent |
| artifact symlink to `/etc/passwd` in output | not returned |
| ignore SIGTERM and sleep forever | killed (SIGKILL), `timeout` |
| leave a background process running | container gone after run (`docker ps -a --filter label=...` empty) |
| forge runner output (print fake JSON to `/proc/<ppid>/fd/1`) | host validation keeps result well-formed and capped |

## Build order and acceptance criteria

Work in this order. After each phase run `pytest` and `ruff check .`; do not start the next phase with failures.

1. **Models, config, errors, base interface.** Unit tests for validation pass.
2. **Runner + LocalSubprocessSandbox.** Runner unit tests pass on Linux/macOS (skip POSIX-only parts on Windows).
3. **Sandbox image.** `docker build` succeeds; image runs as uid 10001; pip absent; no setuid binaries.
4. **DockerSandbox + limiter + janitor.** Command-builder snapshot test; Docker integration tests pass.
5. **Attack suite.** Every row in the table passes. If one fails, fix the isolation; never loosen the test.
6. **Formatter, run_python tool, CodeInterpreterAgent.** Agent tests pass with `ScriptedLLM`.
7. **Executor service + RemoteSandbox + compose.** Service tests pass; `curl` example in README works.
8. **Docs and demos.** `docs/SECURITY.md` (threat model, layers, known limits: shared kernel with runc,
   gVisor/microVM for untrusted multi-tenant use, Docker socket = root), README section for Project 4
   (problem, design, run instructions for Linux/macOS/Windows, decisions table, exercises, interview points),
   `demo_sandbox.py` (agent analyses a CSV and saves a chart), `demo_attacks.py` (table of attack -> result).

## Definition of done

- `pytest` green with Docker installed; without Docker, unit tests green and Docker tests skipped with a reason.
- `python demo_attacks.py` shows every attack blocked.
- No host path, env var or network access possible from sandboxed code, proven by tests.
- README and SECURITY.md updated; Projects 1 and 2 untouched in behaviour.