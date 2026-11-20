# Trusted Claude service: the only image that holds the subscription login.
# It runs the pinned official CLI and serves worker tools to it; it has no Docker
# socket, no project code and no provider key.
#
# Build from the repo root with the staged, digest-verified CLI as a named context:
#   python scripts/personal/probe_cli_contract.py stage
#   docker build -f docker/claude-subscription.Dockerfile \
#     --build-context cli=.probe-cache/<version>-<arch> \
#     --build-arg CLAUDE_CLI_SHA256=<binary sha256 from docs/personal/cli-contract.md> .
FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/service \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY docker/claude-subscription.requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes --no-deps -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Only the service's import closure (enforced by a test).
COPY roboco/__init__.py roboco/agents_config.py roboco/config.py roboco/exceptions.py roboco/logging.py /opt/service/roboco/
COPY roboco/foundation /opt/service/roboco/foundation
COPY roboco/models /opt/service/roboco/models
COPY roboco/seeds /opt/service/roboco/seeds
COPY roboco/mcp/__init__.py roboco/mcp/flow_server.py roboco/mcp/do_server.py roboco/mcp/intake_server.py roboco/mcp/secretary_server.py roboco/mcp/utils.py /opt/service/roboco/mcp/
COPY roboco/agent_sdk/__init__.py roboco/agent_sdk/secretary_driver.py /opt/service/roboco/agent_sdk/
COPY roboco/runtime/__init__.py /opt/service/roboco/runtime/__init__.py
COPY roboco/runtime/subscription /opt/service/roboco/runtime/subscription

# The vendor binary is verified against the pinned digest before it is usable.
ARG CLAUDE_CLI_SHA256
COPY --from=cli claude /opt/claude/claude
RUN test -n "${CLAUDE_CLI_SHA256}" \
    && echo "${CLAUDE_CLI_SHA256}  /opt/claude/claude" | sha256sum --check --strict \
    && chmod 0555 /opt/claude/claude

# The credential volume mounts at /home/claude/.claude; nothing else is writable
# except the tmpfs mounts the deployment adds for /run/roboco and /tmp.
RUN groupadd --gid 10002 claude \
    && useradd --uid 10002 --gid 10002 --home-dir /home/claude --create-home --shell /usr/sbin/nologin claude \
    && install -d -m 0555 -o root -g root /var/lib/roboco/empty

USER 10002:10002
# `/` keeps any file in the working directory off the interpreter's import path.
WORKDIR /
ENTRYPOINT ["python", "-P", "-m", "roboco.runtime.subscription.service_main"]
