"""Supabase Storage. Phase 3.

Both buckets are private. The backend talks to Storage with the service-role key,
which bypasses every policy — so every call here must already have been authorized
by a guard. This file and core/security.py are the whole Supabase surface, keeping
the vendor blast radius to two files (DECISIONS.md D1).
"""

from collections.abc import AsyncIterator

import httpx

from app.core.config import settings
from app.core.errors import UpstreamUnavailable

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _headers(content_type: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "apikey": settings.supabase_service_role_key,
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _url(bucket: str, path: str) -> str:
    return f"{settings.supabase_url}/storage/v1/object/{bucket}/{path}"


async def upload_stream(
    bucket: str,
    path: str,
    stream: AsyncIterator[bytes],
    content_type: str,
    size: int,
) -> str:
    """Upload without ever holding the whole document in memory.

    Content-Length is set explicitly: with an iterator httpx would otherwise fall
    back to chunked transfer-encoding, and a known length lets Storage reject an
    oversized body before reading all of it.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            _url(bucket, path),
            content=stream,
            headers=_headers(content_type)
            | {"x-upsert": "true", "Content-Length": str(size)},
        )
    if response.status_code >= 400:
        raise UpstreamUnavailable("Could not store the uploaded file.")
    return path


async def upload(bucket: str, path: str, data: bytes, content_type: str) -> str:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            _url(bucket, path),
            content=data,
            headers=_headers(content_type) | {"x-upsert": "true"},
        )
    if response.status_code >= 400:
        raise UpstreamUnavailable("Could not store the uploaded file.")
    return path


async def download(bucket: str, path: str) -> bytes:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(_url(bucket, path), headers=_headers())
    if response.status_code >= 400:
        raise UpstreamUnavailable("Could not read the stored file.")
    return response.content


async def delete(bucket: str, path: str) -> None:
    """Delete an object. A missing object is not an error — deletion is idempotent."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.delete(_url(bucket, path), headers=_headers())
    if response.status_code >= 400 and response.status_code != 404:
        raise UpstreamUnavailable("Could not delete the stored file.")


async def create_signed_upload_url(bucket: str, path: str) -> str:
    """Mint a signed upload URL and return it **storage-relative**.

    Relative because the browser and the backend reach Supabase at different URLs
    (CLAUDE.md: baked-in-at-build-time): this process knows Storage as
    ``SUPABASE_URL``, which locally is a hostname only containers resolve. The
    caller's browser prefixes its own ``NEXT_PUBLIC_SUPABASE_URL`` + ``/storage/v1``.

    ``x-upsert`` so a retried upload of the same still is a rewrite, not an error —
    the evidence path is minted per event id, so there is nothing to clobber but
    the same image.

    Every call here bypasses Storage policies (service role), so the guard on the
    route is the authorization; this function must never be reachable from an
    unguarded path.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # The empty JSON object is required: Storage 400s on a bodyless request
        # once the content-type says JSON.
        response = await client.post(
            f"{settings.supabase_url}/storage/v1/object/upload/sign/{bucket}/{path}",
            content=b"{}",
            headers=_headers("application/json") | {"x-upsert": "true"},
        )
    if response.status_code >= 400:
        raise UpstreamUnavailable("Could not prepare the upload.")

    url = response.json().get("url", "")
    if not url:
        raise UpstreamUnavailable("Could not prepare the upload.")
    return url


async def create_signed_download_url(bucket: str, path: str, *, seconds: int = 900) -> str:
    """Mint a signed READ url and return it **storage-relative**.

    The sibling of :func:`create_signed_upload_url`, and relative for the same reason:
    this process knows Storage as ``SUPABASE_URL``, which locally is a hostname only
    containers resolve, while the caller's browser prefixes its own
    ``NEXT_PUBLIC_SUPABASE_URL`` + ``/storage/v1``.

    Short-lived on purpose. This is how an author's browser fetches a proctoring
    still, and a still is a photograph of somebody sitting an exam: a link that
    outlives the page it was rendered for is a photograph that can be forwarded.

    Bypasses Storage policies (service role), so the ROUTE's guard is the whole
    authorization — this must never be reachable from anything but an author-guarded
    path (CLAUDE.md invariant 3).
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"{settings.supabase_url}/storage/v1/object/sign/{bucket}/{path}",
            json={"expiresIn": seconds},
            headers=_headers("application/json"),
        )
    if response.status_code >= 400:
        raise UpstreamUnavailable(f"Could not sign {bucket}/{path}: {response.text[:200]}")
    signed = response.json().get("signedURL") or response.json().get("signedUrl") or ""
    # Storage returns "/object/sign/..."; the browser adds its own origin + /storage/v1.
    return signed.removeprefix("/storage/v1")
