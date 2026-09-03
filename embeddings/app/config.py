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

    # ONNX Runtime's CPU arena allocator, which is a memory LEAK on this workload.
    #
    # The arena grows by doubling and never returns a byte to the OS, so it is sized
    # by the largest batch the process has ever seen and stays there. Measured on this
    # stack after a few book ingests: the service held **4.95 GiB** of an 11.67 GiB
    # Docker VM for a 33M-parameter model, and restarting it dropped the same process
    # to 212 MiB.
    #
    # That is not merely untidy. Ollama shares the VM, so the arena starves the model
    # server: token generation fell from the 16.5 tok/s D21 measured to **0.47 tok/s**,
    # and every assessment-generation call then hit its 180s timeout holding a
    # half-written JSON reply. The paper came back short and blamed the book.
    #
    # Off by default. The arena buys throughput on a service that embeds continuously;
    # this one embeds in bursts at ingest and then idles, so it was paying for an
    # allocation cache with the model server's working set.
    embedding_cpu_mem_arena: bool = False

    # Cap the request body. Untrusted input, and a 100k-item batch is a memory event.
    max_texts_per_request: int = 256
    max_chars_per_text: int = 20_000


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
