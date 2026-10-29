"""Object storage in S3, read through CloudFront.

Credentials are never configured here. boto3 resolves them from the IAM role the host runs as
(an EC2 instance profile, an ECS task role, IRSA on EKS), so nothing in `backend/.env` or git
holds a key. Every object is written with:

* a `retention` tag — the bucket's lifecycle rules expire `standard` and `pinned` objects;
* server-side encryption (`AES256`, or the bucket's KMS key when it enforces one);
* a `Content-Disposition` naming the file, so a CloudFront download saves under its own name.

Downloads are CloudFront URLs when `RBA_CDN_BASE_URL` is set, signed with the key pair in
`RBA_CDN_KEY_PAIR_ID` / `RBA_CDN_PRIVATE_KEY` when those are set; without a CDN the backend
streams the object itself.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.storage import KEEP, StoredObject


class S3Storage:
    backend = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        region: str = "",
        endpoint_url: str = "",
        cdn_base_url: str = "",
        cdn_key_pair_id: str = "",
        cdn_private_key: str = "",
        cdn_url_ttl_s: int = 3600,
        client: Any = None,
    ) -> None:
        if not bucket:
            raise ValueError("RBA_STORAGE_BACKEND=s3 needs RBA_S3_BUCKET")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.region = region
        self.endpoint_url = endpoint_url
        self.cdn_base_url = cdn_base_url.rstrip("/")
        self.cdn_key_pair_id = cdn_key_pair_id
        self.cdn_private_key = cdn_private_key
        self.cdn_url_ttl_s = cdn_url_ttl_s
        self._client = client
        self._signer: Any = None

    @classmethod
    def from_settings(cls, settings: Any) -> S3Storage:
        return cls(
            bucket=settings.rba_s3_bucket,
            prefix=settings.rba_s3_prefix,
            region=settings.rba_s3_region,
            endpoint_url=settings.rba_s3_endpoint_url,
            cdn_base_url=settings.rba_cdn_base_url,
            cdn_key_pair_id=settings.rba_cdn_key_pair_id,
            cdn_private_key=settings.rba_cdn_private_key,
            cdn_url_ttl_s=settings.rba_cdn_url_ttl_s,
        )

    # -- the client --------------------------------------------------------------------------

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                region_name=self.region or None,
                endpoint_url=self.endpoint_url or None,
                config=Config(retries={"max_attempts": 5, "mode": "standard"}),
            )
        return self._client

    def object_key(self, key: str) -> str:
        key = key.strip("/")
        return f"{self.prefix}/{key}" if self.prefix else key

    def _extra(self, content_type: str, retention: str, filename: str | None) -> dict[str, Any]:
        extra: dict[str, Any] = {
            "ContentType": content_type,
            "Tagging": f"retention={retention}",
            "ServerSideEncryption": "AES256",
        }
        if filename:
            extra["ContentDisposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
        return extra

    # -- writes ------------------------------------------------------------------------------

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        retention: str = KEEP,
        filename: str | None = None,
    ) -> StoredObject:
        full = self.object_key(key)
        extra = self._extra(content_type, retention, filename)
        await asyncio.to_thread(
            self.client.put_object, Bucket=self.bucket, Key=full, Body=data, **extra
        )
        return StoredObject(key=key.strip("/"), size=len(data))

    async def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str = "application/octet-stream",
        retention: str = KEEP,
        filename: str | None = None,
    ) -> StoredObject:
        full = self.object_key(key)
        # upload_file splits a large archive into a multipart upload on its own.
        await asyncio.to_thread(
            self.client.upload_file,
            str(path),
            self.bucket,
            full,
            ExtraArgs=self._extra(content_type, retention, filename),
        )
        return StoredObject(key=key.strip("/"), size=path.stat().st_size)

    # -- reads -------------------------------------------------------------------------------

    def _missing(self, exc: Exception) -> bool:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        return str(code) in ("404", "NoSuchKey", "NotFound")

    async def get_bytes(self, key: str) -> bytes | None:
        try:
            response = await asyncio.to_thread(
                self.client.get_object, Bucket=self.bucket, Key=self.object_key(key)
            )
        except Exception as exc:
            if self._missing(exc):
                return None
            raise
        body = response["Body"]
        try:
            return bytes(await asyncio.to_thread(body.read))
        finally:
            body.close()

    async def iter_bytes(self, key: str, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        response = await asyncio.to_thread(
            self.client.get_object, Bucket=self.bucket, Key=self.object_key(key)
        )
        body = response["Body"]
        try:
            while True:
                chunk = await asyncio.to_thread(body.read, chunk_size)
                if not chunk:
                    return
                yield bytes(chunk)
        finally:
            body.close()

    async def size(self, key: str) -> int | None:
        try:
            head = await asyncio.to_thread(
                self.client.head_object, Bucket=self.bucket, Key=self.object_key(key)
            )
        except Exception as exc:
            if self._missing(exc):
                return None
            raise
        return int(head.get("ContentLength", 0))

    async def list_keys(self, prefix: str) -> list[StoredObject]:
        full = self.object_key(prefix).rstrip("/") + "/"

        def run() -> list[StoredObject]:
            found: list[StoredObject] = []
            paginator = self.client.get_paginator("list_objects_v2")
            strip = len(self.prefix) + 1 if self.prefix else 0
            for page in paginator.paginate(Bucket=self.bucket, Prefix=full):
                for item in page.get("Contents", []):
                    found.append(StoredObject(key=item["Key"][strip:], size=int(item["Size"])))
            return found

        return await asyncio.to_thread(run)

    # -- deletes -----------------------------------------------------------------------------

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(
            self.client.delete_object, Bucket=self.bucket, Key=self.object_key(key)
        )

    async def delete_prefix(self, prefix: str) -> int:
        objects = await self.list_keys(prefix)
        for start in range(0, len(objects), 1000):
            batch = [{"Key": self.object_key(o.key)} for o in objects[start : start + 1000]]
            await asyncio.to_thread(
                self.client.delete_objects,
                Bucket=self.bucket,
                Delete={"Objects": batch, "Quiet": True},
            )
        return len(objects)

    # -- downloads ---------------------------------------------------------------------------

    def download_url(self, key: str) -> str | None:
        if not self.cdn_base_url:
            return None
        url = f"{self.cdn_base_url}/{quote(self.object_key(key))}"
        if not (self.cdn_key_pair_id and self.cdn_private_key):
            return url
        expires = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=self.cdn_url_ttl_s)
        return str(self._cloudfront_signer().generate_presigned_url(url, date_less_than=expires))

    def _cloudfront_signer(self) -> Any:
        """A CloudFront signer over the private key the deployment points at by path."""
        if self._signer is None:
            from botocore.signers import CloudFrontSigner
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding, rsa

            pem = Path(self.cdn_private_key).read_bytes()
            key = serialization.load_pem_private_key(pem, password=None)
            if not isinstance(key, rsa.RSAPrivateKey):
                raise ValueError("RBA_CDN_PRIVATE_KEY is not an RSA private key")

            def sign(message: bytes) -> bytes:
                # CloudFront signed URLs are RSA-SHA1 by specification.
                return key.sign(message, padding.PKCS1v15(), hashes.SHA1())

            self._signer = CloudFrontSigner(self.cdn_key_pair_id, sign)
        return self._signer

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "bucket": self.bucket,
            "prefix": self.prefix,
            "region": self.region,
            "cdn": bool(self.cdn_base_url),
            "cdn_signed": bool(self.cdn_key_pair_id and self.cdn_private_key),
        }
