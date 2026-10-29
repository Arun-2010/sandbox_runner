# Sandboxed Code Executor: security model

This document covers `agentkit.sandbox` (Project 4): how LLM-written Python is executed, what
it is protected against, and what it is **not** protected against.

## Threat model

**Attacker capability.** The attacker controls the Python program, in full. That is the normal
case for LLM-written code: the model can be wrong, or it can be steered by prompt injection
hidden in a file, web page or tool output it read. The attacker also controls every byte that
comes back out of the sandbox, including the runner's result line. The program runs as the
same UID as the runner, so it may try to overwrite or forge that line.

**Assets we protect.**

| Asset | Example of harm |
|---|---|
| Host filesystem | read `~/.ssh`, source code, cloud credentials; write anywhere |
| Host secrets / env vars | `OPENAI_API_KEY`, `AWS_SECRET_ACCESS_KEY` in the agent process |
| Network | exfiltration, SSRF to cloud metadata (169.254.169.254), lateral movement, crypto-mining pools |
| Host CPU / RAM / disk / PIDs | denial of service against the agent host and other tenants |
| Other users' runs | reading or tampering with a concurrent or later execution |
| Agent process integrity | forged results, prompt injection via huge or crafted output, memory blow-up when parsing |

**Out of scope.** Kernel 0-days in a runc (shared-kernel) setup, a malicious Docker daemon or
image registry, physical or side-channel attacks (Spectre-class, cache timing), and the
*content* of program output being misleading. Output is labelled untrusted for the model, but
the model can still be fooled by it.

## Design rules

1. **Isolation is enforced by the container runtime, never by inspecting code.** No regex or
   AST filter decides whether code is "safe". Any lint (for example a warning that `pip install`
   cannot work offline) is UX only and is **not a security control**.
2. **Everything coming out of the sandbox is untrusted.** The host reads at most
   `max_result_bytes`, finds the result line by a per-run random nonce, validates it with a
   strict Pydantic model (`extra="forbid"`, `strict=True`), re-applies every size cap, decodes
   and size-checks every artifact, sanitises names, and assigns MIME types from an allow-list
   (HTML and SVG become `application/octet-stream`). If no valid line is found the status is
   `sandbox_error` and no raw output is echoed.
3. **Defence in depth.** Each layer below is independent; a failure in one is not fatal.
4. **Fail closed.** `DockerSandbox.health()` must pass before code runs: daemon reachable,
   cgroup memory/swap/pids/CPU limits enforceable (`docker info`), configured runtime installed,
   image present (`--pull never`). Config values have hard lower bounds, so there is no
   "0 = unlimited". Nothing ever falls back to `LocalSubprocessSandbox`, which refuses to
   construct without `allow_unsafe=True`.
5. **One container per execution, destroyed afterwards.** `--rm`, a unique name, `docker kill`
   on timeout, `docker rm -f` on any abnormal exit, and a label-based janitor for crashes.

## Layers

| Layer | Mechanism | Stops |
|---|---|---|
| Fresh container per run | `docker run --rm`, unique `--name`, janitor by label | persistence, cross-run leakage |
| No network | `--network none` (loopback only) | exfiltration, SSRF, downloads |
| No host filesystem | no `-v`/bind mounts, inputs on stdin, `--read-only` root | reading or writing host files |
| Bounded writable space | `--tmpfs /workspace` (64 MB), `--tmpfs /tmp` (32 MB, `noexec`), `nosuid,nodev`, `RLIMIT_FSIZE` | disk exhaustion, dropped binaries in /tmp |
| Unprivileged user | `--user 10001:10001`, image has no setuid/setgid binaries, no pip | root inside container |
| No capabilities | `--cap-drop ALL`, `--security-opt no-new-privileges`, default seccomp profile kept | privilege escalation, raw sockets, mount, ptrace of other UIDs |
| Resource limits | `--memory` = `--memory-swap` (no swap), `--cpus`, `--pids-limit`, `--ulimit nofile/nproc`, child `RLIMIT_CPU`, `RLIMIT_CORE=0` | memory/CPU/fork bombs, core dumps |
| Timeouts | inner (runner kills the process group with SIGKILL), outer (host `docker kill` at timeout + grace) | infinite loops, SIGTERM-ignoring code, a hung runner |
| Process cleanup | runner kills the child's process group, then `kill(-1, SIGKILL)` for anything that escaped with `setsid()`; `--init` reaps zombies | background processes outliving the run |
| Minimal environment | exactly `HOME`, `MPLCONFIGDIR`, `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED` passed with explicit values; never `--env-file` or bare `-e KEY` | host secret leakage |
| Output caps | runner keeps head+tail of each stream; host caps the total it reads and kills the container past it | host memory exhaustion, context flooding |
| Result integrity | nonce, strict validation, runner marks itself non-dumpable (`PR_SET_DUMPABLE=0`) so the child cannot open `/proc/<runner>/fd/1` or read its memory | forged exit codes or artifacts |
| Safe artifacts | `lstat` + `O_NOFOLLOW`, regular files only, `st_nlink == 1`, inode re-check after open, count/size caps | symlink/hardlink exfiltration, FIFO hangs |
| Backpressure | semaphore + bounded queue → `SandboxBusyError` / HTTP 429 | resource exhaustion through request volume |
| Docker socket separation | agents use `RemoteSandbox`; only the executor service touches Docker | agent compromise becoming host root |

### OOM detection

`--rm` deletes the container, so `State.OOMKilled` cannot be inspected afterwards. Keeping the
container around just to inspect it would add a cleanup race. Instead: if the runner reports the
child died by SIGKILL without a timeout (the cgroup OOM killer; the child gets
`oom_score_adj=1000` so it is chosen before the runner), or `MemoryError` appears at the end of
stderr, the status is `oom`. If the runner itself is killed and Docker exits 137 with no result
line, the status is also `oom`. A program can fake `MemoryError` in its own stderr. That only
changes its own status label, so it is harmless.

## Known limitations

- **Shared kernel (runc).** Containers share the host kernel. A kernel exploit reachable
  through the default seccomp profile escapes the sandbox. For untrusted, **multi-tenant**
  workloads, use gVisor (`AGENTKIT_SANDBOX_RUNTIME=runsc`) or a microVM runtime
  (Kata Containers, Firecracker) and keep the host kernel patched. Health checks refuse to run
  if the configured runtime is not installed.
- **The Docker socket is root.** Anyone who can reach the daemon can start a privileged
  container that mounts `/`. The executor service is the only component that holds the
  socket. Run it on a dedicated VM/host with nothing else of value, or use rootless Docker
  (`DOCKER_HOST=unix:///run/user/<uid>/docker.sock`).
- **`--ulimit nproc` counts per UID across the host kernel.** All sandboxes use UID 10001, so a
  fork bomb in one run can briefly cause `fork()` to fail in a concurrent run. `--pids-limit`
  (per container) is the real containment. With userns-remap, or one VM per tenant, the
  interference goes away.
- **CPU and memory bandwidth** are shared. `--cpus` caps quota, not cache or memory-bus
  contention.
- **The runner shares a UID with the program.** The program can kill the runner. The host then
  gets no valid result and reports `sandbox_error`, which fails closed. It cannot forge a
  result without the nonce, which lives only in the non-dumpable runner's memory. Even a
  forged result is still capped and validated.
- **The program can write to the container's stdout through PID 1.** `--init` runs
  `docker-init` as PID 1 with the same `--user`, and it is dumpable, so `/proc/1/fd/1` is
  writable (verified by `test_forged_runner_output`). The runner's own `/proc/<pid>/fd/1` is
  not. The program can therefore inject lines, or corrupt the runner's line, but only for its
  own run. Injected lines lack the nonce and are ignored. Corruption gives `sandbox_error`,
  and a flood past `max_result_bytes` gives `output_limit` (`test_flooding_container_stdout_is_capped`).
  None of this can affect the host or other runs.
- **Output is untrusted text.** It reaches the model labelled as data, but prompt injection
  through program output remains possible. Treat agent actions downstream of a sandbox run
  with the same caution as actions after reading a web page.
- **Docker Desktop (macOS/Windows)** runs containers in a Linux VM. Isolation is between the
  container and that VM, and the VM provides an extra boundary to the real host. Timing and
  performance differ from Linux.
- **The local backend is not a sandbox.** `LocalSubprocessSandbox` has no network, filesystem
  or memory isolation, and on Windows it has no rlimits or process-group kill.

## Deployment guidance

1. Build the sandbox image in CI, push it, and run it **by digest**:
   `AGENTKIT_SANDBOX_IMAGE=registry/agentkit-sandbox@sha256:...`. Pin the base image by digest
   in `sandbox_image/Dockerfile`.
2. Run the executor service on a dedicated host/VM; agents use `RemoteSandbox(url, token)`.
   Generate the token with `secrets.token_urlsafe(32)`; put TLS in front; bind to a private
   network.
3. Prefer gVisor (`runsc`) or a microVM runtime when code comes from untrusted users.
4. Keep the default seccomp/AppArmor profiles. Never add `--privileged`, `--cap-add`, host
   namespaces, bind mounts or `--env-file`. `tests/sandbox/test_docker_command.py` fails if
   any of these appear.
5. Alert on `sandbox_error` rates and janitor removals. Both indicate something abnormal.
6. Ship the one-line-per-run JSON logs (`agentkit.sandbox` logger). They contain run id,
   status, exit code, duration and byte counts, never code or output.

## Verification

`pytest -m docker` runs `tests/sandbox/test_attacks.py`: host files, env canary, DNS/HTTP/TCP/UDP,
infinite loop, 2 GB allocation, fork bomb, disk fill, 100 MB stdout, writes to `/usr` `/etc`
`/opt/runner`, `setuid(0)` and capability sets, persistence, artifact symlinks and hardlinks,
SIGTERM-ignoring sleep, escaped background processes, and forged runner output.
`python demo_attacks.py` prints the same checks as a table.
