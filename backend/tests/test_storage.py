"""Object storage: the same interface on this host's disk and in S3 behind CloudFront.

The S3 tests run against moto, which answers the S3 API in process; nothing reaches AWS.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app.storage import KEEP, PINNED, STANDARD, keys
from app.storage.local import LocalStorage
from app.storage.s3 import S3Storage

BUCKET = "streamlens-test"


@pytest.fixture()
def s3() -> Iterator[S3Storage]:
    from moto import mock_aws

    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        import boto3

        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield S3Storage(
            bucket=BUCKET,
            prefix="prod",
            region="us-east-1",
            cdn_base_url="https://d111.cloudfront.net",
            client=client,
        )


async def _roundtrip(storage, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    await storage.put_bytes("channels/a/r.html", b"<html>", content_type="text/html")
    big = tmp_path / "big.zip"
    big.write_bytes(b"z" * 300_000)
    await storage.put_file("channels/a/archives/x.zip", big, retention=PINNED, filename="x.zip")

    assert await storage.get_bytes("channels/a/r.html") == b"<html>"
    assert await storage.get_bytes("channels/a/missing") is None
    assert await storage.size("channels/a/archives/x.zip") == 300_000
    streamed = b"".join([c async for c in storage.iter_bytes("channels/a/archives/x.zip", 65536)])
    assert streamed == big.read_bytes()
    listed = {o.key for o in await storage.list_keys("channels/a")}
    assert listed == {"channels/a/r.html", "channels/a/archives/x.zip"}

    assert await storage.delete_prefix("channels/a") == 2
    assert await storage.list_keys("channels/a") == []


async def test_local_storage_reads_back_what_it_wrote(tmp_path: Path) -> None:
    await _roundtrip(LocalStorage(tmp_path / "store"), tmp_path)


async def test_s3_storage_reads_back_what_it_wrote(s3: S3Storage, tmp_path: Path) -> None:
    await _roundtrip(s3, tmp_path)


async def test_s3_objects_carry_their_retention_encryption_and_file_name(s3: S3Storage) -> None:
    await s3.put_bytes("channels/a/rec.ts", b"x", retention=STANDARD, filename="rec one.ts")
    full = "prod/channels/a/rec.ts"
    tags = s3.client.get_object_tagging(Bucket=BUCKET, Key=full)["TagSet"]
    head = s3.client.head_object(Bucket=BUCKET, Key=full)

    assert {"Key": "retention", "Value": STANDARD} in tags
    assert head["ServerSideEncryption"] == "AES256"
    assert "rec%20one.ts" in head["ContentDisposition"]


def test_a_download_goes_through_cloudfront_and_is_signed_when_a_key_pair_is_set(
    s3: S3Storage, tmp_path: Path
) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    assert (
        s3.download_url("channels/a/x.zip") == "https://d111.cloudfront.net/prod/channels/a/x.zip"
    )

    pem = tmp_path / "cf.pem"
    pem.write_bytes(
        rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    s3.cdn_key_pair_id, s3.cdn_private_key = "K2ABCDEF", str(pem)
    signed = s3.download_url("channels/a/x.zip")
    assert signed is not None
    query = parse_qs(urlsplit(signed).query)
    assert query["Key-Pair-Id"] == ["K2ABCDEF"]
    assert int(query["Expires"][0]) > dt.datetime.now(dt.UTC).timestamp()


def test_local_storage_streams_through_the_backend_and_refuses_escaping_keys(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path)
    assert storage.download_url("channels/a/x.zip") is None
    assert storage.path("../../etc/passwd") == tmp_path / "etc/passwd"


async def test_local_purge_expires_by_retention_class_as_a_lifecycle_rule_would(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path)
    for name, retention in (("full.ts", STANDARD), ("clip.ts", PINNED), ("r.html", KEEP)):
        await storage.put_bytes(f"c/{name}", b"x", retention=retention)
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=60)).timestamp()
    for name in ("full.ts", "clip.ts", "r.html"):
        os.utime(storage.path(f"c/{name}"), (old, old))

    now = dt.datetime.now(dt.UTC)
    removed = await storage.purge(
        standard_before=now - dt.timedelta(days=30), pinned_before=now - dt.timedelta(days=180)
    )
    assert removed == 1
    assert {o.key for o in await storage.list_keys("c")} == {"c/clip.ts", "c/r.html"}


def test_every_run_of_a_channel_shares_one_folder_named_for_people_and_for_machines() -> None:
    day = dt.datetime(2026, 10, 29, tzinfo=dt.UTC)
    a = keys.job_prefix(
        job_id="j1",
        channel_name="Samsung News / US",
        playback_url="https://cdn.example/live/news/master.m3u8?token=a",
        created_at=day,
    )
    b = keys.job_prefix(
        job_id="j2",
        channel_name="Samsung News / US",
        playback_url="https://cdn.example/live/news/master.m3u8?token=b",
        created_at=day,
    )
    assert a.startswith("channels/samsung-news-us-") and a.endswith("/2026-10-29/j1")
    assert a.rsplit("/", 2)[0] == b.rsplit("/", 2)[0], "tokens differ, the channel does not"
    assert keys.recording_key(a, "incidents", "PLAYBACK/v1/1.ts").endswith(
        "recording/clips/PLAYBACK/v1/1.ts"
    )
    assert keys.archive_key(a, "network-log.zip").endswith("/archives/network-log.zip")
