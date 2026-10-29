"""Where each of a run's files lives in storage: one folder per channel, one per run inside it.

    channels/<channel-slug>-<channel-id>/<YYYY-MM-DD>/<job-id>/
        reports/<report file>              the rendered reports            retention: keep
        recording/full/...                  a realtime run's recording      retention: standard
        recording/clips/...                 an aging run's incident clips   retention: pinned
        archives/network-log.zip            every poll and fetch of the run retention: pinned
        archives/evidence.zip               findings, playlists, segments   retention: pinned
        archives/recording.zip              the recording, joined, playable as the recording

The channel folder is named by a readable slug of the channel's name *and* a short hash of its
playback URL without the query (`review.channel_key`): names are renamed, repeat across
countries and carry characters a key should not, while the URL is what identifies the channel
across runs. The date and the job id keep two runs of one channel apart, and every run of a
channel shares one folder to browse.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re

from app.analysis.review import channel_key

SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(text: str | None, limit: int = 48) -> str:
    return SAFE.sub("-", text or "").strip("-.").lower()[:limit] or "channel"


def channel_folder(channel_name: str | None, playback_url: str | None) -> str:
    digest = hashlib.sha1(channel_key(playback_url or "").encode(), usedforsecurity=False)
    return f"channels/{slug(channel_name)}-{digest.hexdigest()[:10]}"


def job_prefix(
    *,
    job_id: str,
    channel_name: str | None,
    playback_url: str | None,
    created_at: dt.datetime | None,
) -> str:
    day = (created_at or dt.datetime.now(dt.UTC)).strftime("%Y-%m-%d")
    return f"{channel_folder(channel_name, playback_url)}/{day}/{job_id}"


def report_key(prefix: str, filename: str) -> str:
    return f"{prefix}/reports/{slug(filename, 120)}"


def recording_key(prefix: str, mode: str, relative: str) -> str:
    folder = "clips" if mode == "incidents" else "full"
    return f"{prefix}/recording/{folder}/{relative.lstrip('/')}"


def archive_key(prefix: str, name: str) -> str:
    return f"{prefix}/archives/{name}"
