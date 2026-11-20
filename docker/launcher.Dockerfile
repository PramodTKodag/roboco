# Worker launcher: the only image that holds the Docker socket. It starts
# credential-free, network-less workers and nothing else; it has no model
# credential, no project code and no provider key.
# Build from the repo root: docker build -f docker/launcher.Dockerfile .
FROM docker:29-cli@sha256:34507eb5069d8be82c95dd3b6987d4250fd5fee49b82e2509b7d7a7030740d6d AS docker-cli

FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/launcher \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DOCKER_CONFIG=/tmp/docker

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

COPY docker/task-worker.requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes --no-deps -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Only the launcher's import closure (enforced by a test).
COPY roboco/__init__.py roboco/config.py roboco/exceptions.py roboco/logging.py /opt/launcher/roboco/
COPY roboco/foundation /opt/launcher/roboco/foundation
COPY roboco/runtime/__init__.py /opt/launcher/roboco/runtime/__init__.py
COPY roboco/runtime/subscription /opt/launcher/roboco/runtime/subscription

# Runs as root only to open the Docker socket; the deployment drops every
# capability and mounts the root filesystem read-only.
WORKDIR /
ENTRYPOINT ["python", "-P", "-m", "roboco.runtime.subscription.launcher_main"]
