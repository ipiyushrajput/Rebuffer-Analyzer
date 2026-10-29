"""Object storage for the files a run produces.

Report bytes, recordings and the archives a finished run leaves behind (network log, evidence,
recording) are files, not rows. They go through one interface with two implementations:

* `LocalStorage` (`RBA_STORAGE_BACKEND=local`) writes under `RBA_STORAGE_DIR`, so a developer
  runs everything on a laptop with nothing to provision;
* `S3Storage` (`RBA_STORAGE_BACKEND=s3`) writes to `RBA_S3_BUCKET` with the credentials of the
  IAM role the host runs as, and hands out CloudFront URLs for downloads.

Every object carries a **retention class**, which the bucket's lifecycle rules (S3) or the
purge loop (local) act on:

* `standard` — a realtime run's full recording: kept `RBA_SAMPLE_RETENTION_DAYS`;
* `pinned` — incident clips and a finished run's archives: kept `RBA_PINNED_RETENTION_DAYS`;
* `keep` — reports: kept until the run is deleted.

The layout is one folder per channel and one per run inside it (`app/storage/keys.py`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

STANDARD = "standard"
PINNED = "pinned"
KEEP = "keep"
RETENTION_CLASSES = (STANDARD, PINNED, KEEP)


@dataclass(slots=True)
class StoredObject:
    key: str
    size: int


class Storage(Protocol):
    """Where a run's files live. Every method is safe to call from the event loop."""

    backend: str

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        retention: str = KEEP,
        filename: str | None = None,
    ) -> StoredObject: ...

    async def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str = "application/octet-stream",
        retention: str = KEEP,
        filename: str | None = None,
    ) -> StoredObject: ...

    async def get_bytes(self, key: str) -> bytes | None: ...

    def iter_bytes(self, key: str, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]: ...

    async def size(self, key: str) -> int | None: ...

    async def list_keys(self, prefix: str) -> list[StoredObject]: ...

    async def delete(self, key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> int: ...

    def download_url(self, key: str) -> str | None:
        """A URL the browser fetches the object from directly, or None to stream it."""
        ...

    def describe(self) -> dict[str, Any]: ...


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    from app.config import get_settings

    settings = get_settings()
    backend = settings.rba_storage_backend.strip().lower()
    if backend == "s3":
        from app.storage.s3 import S3Storage

        return S3Storage.from_settings(settings)
    if backend not in ("", "local"):
        raise ValueError(f"Unsupported RBA_STORAGE_BACKEND: {settings.rba_storage_backend}")
    from app.storage.local import LocalStorage

    return LocalStorage(settings.rba_storage_dir)


def reset_storage() -> None:
    """Drop the cached backend, so a settings change or a test picks a new one."""
    get_storage.cache_clear()
