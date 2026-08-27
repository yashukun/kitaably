# syntax=docker/dockerfile:1
#
# One image, two roles: the API server and the Celery workers run the same code with
# different commands (DEPLOYMENT.md). Same build, same models, same migrations.
#
# Multi-stage so the final layer carries no compiler and no package-manager cache.

# --------------------------------------------------------------------- builder
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies resolve from the lockfile in their own layer, so editing application
# code does not re-resolve the world.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# --------------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# No shell tooling, no package manager cache, and not root.
RUN groupadd --system --gid 1001 kitaably \
    && useradd --system --uid 1001 --gid kitaably --no-create-home kitaably

WORKDIR /app

COPY --from=builder --chown=kitaably:kitaably /app/.venv /app/.venv
COPY --chown=kitaably:kitaably app ./app

# The one writable path in the image. Celery beat persists its schedule here; /app
# itself stays root-owned so a compromised process cannot rewrite its own code.
RUN mkdir -p /app/.state && chown kitaably:kitaably /app/.state

USER kitaably

EXPOSE 8000

# Overridden in compose and in the Kubernetes Deployments for the worker and beat.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
