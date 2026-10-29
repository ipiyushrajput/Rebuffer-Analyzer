"""Object storage on this host's disk, for development and single-host deployments.

Keys map to paths under the root. The retention class is written beside each file so the
purge loop can expire it the way an S3 lifecycle rule would.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app.storage import KEEP, PINNED, STANDARD, StoredObject

RETENTION_SUFFIX = ".retention"


def _safe(key: str) -> str:
    parts = [part for part in key.replace("\\", "/").split("/") if part not in ("", ".", "..")]
    if not parts:
        raise ValueError("An empty storage key")
    return "/".join(parts)


class LocalStorage:
    backend = "local"

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, key: str) -> Path:
        return self.root / _safe(key)

    def _write(self, key: str, data: bytes | None, source: Path | None, retention: str) -> int:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source is not None:
            shutil.copyfile(source, target)
        else:
            target.write_bytes(data or b"")
        target.with_name(target.name + RETENTION_SUFFIX).write_text(retention, encoding="utf-8")
        return target.stat().st_size

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        retention: str = KEEP,
        filename: str | None = None,
    ) -> StoredObject:
        size = await asyncio.to_thread(self._write, key, data, None, retention)
        return StoredObject(key=_safe(key), size=size)

    async def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str = "application/octet-stream",
        retention: str = KEEP,
        filename: str | None = None,
    ) -> StoredObject:
        size = await asyncio.to_thread(self._write, key, None, path, retention)
        return StoredObject(key=_safe(key), size=size)

    async def get_bytes(self, key: str) -> bytes | None:
        target = self.path(key)
        if not target.is_file():
            return None
        return await asyncio.to_thread(target.read_bytes)

    async def iter_bytes(self, key: str, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        target = self.path(key)
        handle = await asyncio.to_thread(target.open, "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, chunk_size)
                if not chunk:
                    return
                yield chunk
        finally:
            handle.close()

    async def size(self, key: str) -> int | None:
        target = self.path(key)
        return target.stat().st_size if target.is_file() else None

    async def list_keys(self, prefix: str) -> list[StoredObject]:
        base = self.root / _safe(prefix) if prefix.strip("/") else self.root
        if not base.exists():
            return []

        def walk() -> list[StoredObject]:
            return [
                StoredObject(key=path.relative_to(self.root).as_posix(), size=path.stat().st_size)
                for path in sorted(base.rglob("*"))
                if path.is_file() and not path.name.endswith(RETENTION_SUFFIX)
            ]

        return await asyncio.to_thread(walk)

    async def delete(self, key: str) -> None:
        target = self.path(key)
        target.unlink(missing_ok=True)
        target.with_name(target.name + RETENTION_SUFFIX).unlink(missing_ok=True)

    async def delete_prefix(self, prefix: str) -> int:
        base = self.root / _safe(prefix)
        if not base.exists():
            return 0
        removed = len(await self.list_keys(prefix))
        await asyncio.to_thread(shutil.rmtree, base, True)
        return removed

    def download_url(self, key: str) -> str | None:
        return None

    def describe(self) -> dict[str, Any]:
        return {"backend": self.backend, "root": str(self.root), "cdn": False}

    async def purge(self, *, standard_before: dt.datetime, pinned_before: dt.datetime) -> int:
        """Expire objects as the bucket's lifecycle rules would: by class and age."""

        def run() -> int:
            removed = 0
            if not self.root.exists():
                return 0
            cutoffs = {STANDARD: standard_before.timestamp(), PINNED: pinned_before.timestamp()}
            for marker in self.root.rglob("*" + RETENTION_SUFFIX):
                retention = marker.read_text(encoding="utf-8").strip()
                cutoff = cutoffs.get(retention)
                target = marker.with_name(marker.name[: -len(RETENTION_SUFFIX)])
                if cutoff is None or not target.exists():
                    continue
                if target.stat().st_mtime < cutoff:
                    target.unlink(missing_ok=True)
                    marker.unlink(missing_ok=True)
                    removed += 1
            return removed

        return await asyncio.to_thread(run)
