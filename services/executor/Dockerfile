# Image for the executor service itself (NOT the sandbox image).
# Build from the repository root:
#   docker build -f services/executor/Dockerfile -t agentkit-executor:dev .
#
# The service drives the Docker CLI, so the static docker client is installed. It needs access
# to a Docker daemon (see docker-compose.yml for the security implications).
FROM python:3.12-slim

ARG DOCKER_CLI_VERSION=27.3.1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && arch="$(uname -m)" \
 && curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_CLI_VERSION}.tgz" \
    | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY agentkit ./agentkit
COPY services ./services
RUN pip install --no-cache-dir ".[service]"

# Runs as root only because the mounted Docker socket is root-owned. Prefer a remote/rootless
# daemon via DOCKER_HOST and drop to a non-root user in that setup.
EXPOSE 8080
CMD ["uvicorn", "services.executor.app:app", "--host", "0.0.0.0", "--port", "8080"]
