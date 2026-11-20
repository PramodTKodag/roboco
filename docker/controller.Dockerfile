# Personal controller (API and dispatcher roles). Pure orchestration: no coding
# CLIs, no docker client, no Node toolchain and no provider credential. Project
# commands run only in the offline worker through the launcher.
# Build from the repo root: docker build -f docker/controller.Dockerfile .
FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11@sha256:77280f2f771df71f90786c314fe1bbc1e023feac652969bbf139c280babf2eb7 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_HTTP_TIMEOUT=300 \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system

# Locked dependencies first so source edits do not invalidate this layer.
COPY pyproject.toml uv.lock README.md /app/
RUN uv sync --frozen --no-dev --no-install-project

COPY roboco /app/roboco
COPY agents /app/agents
COPY docs/rag /app/docs/rag
COPY docs/map /app/docs/map
COPY alembic.ini /app/
COPY alembic /app/alembic
RUN uv sync --frozen --no-dev

FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS runner

# git: the controller still reads and writes the project's local repository.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10003 controller \
    && useradd --uid 10003 --gid 10003 --home-dir /home/controller --create-home \
        --shell /usr/sbin/nologin controller

COPY --from=builder /app /app

# Writable state lives on named volumes mounted here; creating the directories
# with their final owner first lets a new volume inherit it.
RUN install -d -o controller -g controller -m 0700 \
        /data /data/workspaces /data/logs /app/mcp-configs /app/vault \
        /app/prompts-generated /app/agent-settings /app/briefings /app/manifests \
    && git config --system --add safe.directory '*'

ENV PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10003:10003
WORKDIR /app
EXPOSE 8000
ENTRYPOINT ["python", "-m", "roboco.cli"]
CMD []
