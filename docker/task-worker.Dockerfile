# Credential-free, offline task worker. Runs project commands only.
# Built from the repo root:  docker build -f docker/task-worker.Dockerfile .
# Deliberately not the agent base image: no CLI, no git credentials, no docker.
FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/worker \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git runs only inside the worker, against the imported snapshot. The packages come
# from the pinned base image's distribution archive (not hash-pinned; see the
# limitations list). Package lists are removed so the layer stays minimal.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY docker/task-worker.requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes --no-deps -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Only the import closure of the worker entrypoint (enforced by a test).
COPY roboco/__init__.py roboco/config.py roboco/exceptions.py roboco/logging.py /opt/worker/roboco/
COPY roboco/foundation /opt/worker/roboco/foundation
COPY roboco/conventions /opt/worker/roboco/conventions
COPY roboco/runtime/__init__.py /opt/worker/roboco/runtime/__init__.py
COPY roboco/runtime/subscription /opt/worker/roboco/runtime/subscription

RUN groupadd --gid 10001 worker \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin worker \
    && mkdir /workspace && chown 10001:10001 /workspace

USER 10001:10001
# `/` keeps project files out of the interpreter's import and config lookup path.
WORKDIR /
ENTRYPOINT ["python", "-P", "-m", "roboco.runtime.subscription.worker_entrypoint"]
