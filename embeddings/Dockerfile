# syntax=docker/dockerfile:1
#
# The embeddings service. CPU-only, no GPU, no CUDA layers.
#
# Model weights are NOT baked in: they land in a mounted cache volume on first load.
# That keeps the image small and makes the slow-start behaviour real, which is the
# whole point of giving this service a readiness probe of its own.

# --------------------------------------------------------------------- builder
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# --------------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm AS runtime

# huggingface_hub's downloader writes a chunk cache under $HOME/.cache before the
# weights ever reach the model cache dir. Both are pinned into the mounted volume, so
# neither depends on $HOME being writable and both survive a restart.
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

# Writable by the non-root user; the whole cache tree is one mounted volume so a
# restart does not re-download the weights.
RUN mkdir -p /app/.cache/fastembed /app/.cache/huggingface \
    && chown -R kitaably:kitaably /app/.cache

USER kitaably

EXPOSE 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
