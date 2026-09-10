# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/app/.cache/huggingface \
    XDG_CACHE_HOME=/app/.cache
RUN groupadd --system --gid 1001 kitaably \
    && useradd --system --uid 1001 --gid kitaably --create-home --home-dir /home/kitaably kitaably
WORKDIR /app
COPY --from=builder --chown=kitaably:kitaably /app/.venv /app/.venv
COPY --chown=kitaably:kitaably app ./app
RUN mkdir -p /app/.cache/fastembed /app/.cache/huggingface \
    && chown -R kitaably:kitaably /app/.cache
USER kitaably
EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
