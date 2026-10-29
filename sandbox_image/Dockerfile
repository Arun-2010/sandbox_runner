# Sandbox image for agentkit's DockerSandbox.
#
# Build:   docker build -t agentkit-sandbox:dev sandbox_image/
#
# Production: pin the base image by digest so a re-tag upstream cannot change what you run:
#   docker pull python:3.12-slim
#   docker image inspect --format '{{index .RepoDigests 0}}' python:3.12-slim
#   FROM python:3.12-slim@sha256:<digest>
# and run the sandbox by digest too: AGENTKIT_SANDBOX_IMAGE=agentkit-sandbox@sha256:<digest>
#
# runner.py is a copy of agentkit/sandbox/runner.py (the build context is this directory).
# tests/sandbox/test_image_files.py fails if the two drift apart.
FROM python:3.12-slim

# One BLAS/OpenMP thread: matches --cpus 1 and keeps the address space well under RLIMIT_AS
# (with one thread per visible host CPU, OpenBLAS alone reserves over 1 GB).
ENV MPLBACKEND=Agg \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    MPLCONFIGDIR=/tmp/mpl

COPY requirements.txt /tmp/requirements.txt

# 1. Install pinned libraries, then precompile bytecode (the root FS is read-only at runtime,
#    so without this every run would recompile pandas/numpy and start slowly).
# 2. Remove pip/setuptools/wheel: there is no network anyway, and less tooling is less surface.
# 3. Strip setuid/setgid bits so nothing in the image can raise privileges.
# 4. Create the unprivileged sandbox user (no home directory, no login shell).
# 5. Pre-build matplotlib's font cache; the runner copies it into the tmpfs MPLCONFIGDIR.
RUN pip install --no-cache-dir --timeout 120 --retries 10 -r /tmp/requirements.txt \
 && rm -f /tmp/requirements.txt \
 && python -m compileall -q /usr/local/lib/python3.12 \
 && python -m pip uninstall -y pip setuptools wheel \
 && rm -rf /usr/local/bin/pip* /root/.cache \
 && groupadd --gid 10001 sandbox \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin sandbox \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} + \
 && MPLCONFIGDIR=/opt/mplcache python -c "import matplotlib.font_manager" \
 && chmod -R a+rX,go-w /opt/mplcache \
 && mkdir -p /opt/runner /workspace

COPY --chown=root:root --chmod=0444 runner.py /opt/runner/runner.py

USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["python", "-I", "/opt/runner/runner.py"]
CMD ["--container"]
