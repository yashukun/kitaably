"""Embedding service settings.

Shares ``.env`` with the rest of the stack, so ``EMBEDDING_MODEL`` and
``EMBEDDING_DIM`` have exactly one definition. ``extra="ignore"`` lets the file's
many backend-only keys pass through untouched.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    log_level: str = "INFO"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    embedding_batch_size: int = 64

    # Weights are downloaded on first load. Mount this as a volume so a container
    # restart does not re-download ~130 MB of ONNX.
    model_cache_dir: str = "/app/.cache/fastembed"

    # Cap the request body. Untrusted input, and a 100k-item batch is a memory event.
    max_texts_per_request: int = 256
    max_chars_per_text: int = 20_000


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
