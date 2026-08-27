"""Request and response contracts for ``/embed``."""

from pydantic import BaseModel, Field

from app.config import settings


class EmbedRequest(BaseModel):
    texts: list[str] = Field(
        ...,
        min_length=1,
        max_length=settings.max_texts_per_request,
        description="Chunk texts to encode. Order is preserved in the response.",
    )


class EmbedResponse(BaseModel):
    model: str
    dim: int
    embeddings: list[list[float]]
