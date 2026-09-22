"""Settings and analysis thresholds.

Every number a rule compares against lives here. Rules never inline a threshold: they
read it from `Thresholds` so the Settings tab can change behaviour without a code change.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class UserAgentProfile(NamedTuple):
    """One Smart-TV client the analyzer can identify as."""

    label: str
    user_agent: str


# Every Tizen release TV Plus ships on, newest first, as the device sends it. A CDN or a
# packager can serve differently per User-Agent, so which one a run went out as is part of
# what the run measured — it is recorded in the result and stated in the report.
#
# **These strings are copied character for character from the device.** The 2.4 entry says
# `Linux` where every later one says `LINUX`, and `Tizen 2.4.0` where the others carry one
# decimal; both are correct and neither is to be tidied up.
USER_AGENT_PROFILE_TABLE: dict[str, UserAgentProfile] = {
    "tizen10": UserAgentProfile(
        "Tizen 10.0 (2026)",
        "Mozilla/5.0 (SMART-TV; LINUX; Tizen 10.0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) 130.0.6723.116/10.0 TV Safari/537.36",
    ),
    "tizen9": UserAgentProfile(
        "Tizen 9.0 (2025)",
        "Mozilla/5.0 (SMART-TV; LINUX; Tizen 9.0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) 120.0.6099.5/9.0 TV Safari/537.36",
    ),
    "tizen8": UserAgentProfile(
        "Tizen 8.0 (2024)",
        "Mozilla/5.0 (SMART-TV; LINUX; Tizen 8.0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) 108.0.5359.1/8.0 TV Safari/537.36",
    ),
    "tizen7": UserAgentProfile(
        "Tizen 7.0 (2023)",
        "Mozilla/5.0 (SMART-TV; LINUX; Tizen 7.0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) 94.0.4606.31/7.0 TV Safari/537.36",
    ),
    "tizen65": UserAgentProfile(
        "Tizen 6.5 (2022)",
        "Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.5) AppleWebKit/537.36 "
        "(KHTML, like Gecko) 85.0.4183.93/6.5 TV Safari/537.36",
    ),
    "tizen6": UserAgentProfile(
        "Tizen 6.0 (2021)",
        "Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) 76.0.3809.146/6.0 TV Safari/537.36",
    ),
    "tizen55": UserAgentProfile(
        "Tizen 5.5 (2020)",
        "Mozilla/5.0 (SMART-TV; LINUX; Tizen 5.5) AppleWebKit/537.36 "
        "(KHTML, like Gecko) 69.0.3497.106.1/5.5 TV Safari/537.36",
    ),
    "tizen5": UserAgentProfile(
        "Tizen 5.0 (2019)",
        "Mozilla/5.0 (SMART-TV; LINUX; Tizen 5.0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Version/5.0 TV Safari/537.36",
    ),
    "tizen4": UserAgentProfile(
        "Tizen 4.0 (2018)",
        "Mozilla/5.0 (SMART-TV; LINUX; Tizen 4.0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Version/4.0 TV Safari/537.36",
    ),
    "tizen3": UserAgentProfile(
        "Tizen 3.0 (2017)",
        "Mozilla/5.0 (SMART-TV; LINUX; Tizen 3.0) AppleWebKit/538.1 "
        "(KHTML, like Gecko) Version/3.0 TV Safari/538.1",
    ),
    "tizen24": UserAgentProfile(
        "Tizen 2.4 (2016)",
        "Mozilla/5.0 (SMART-TV; Linux; Tizen 2.4.0) AppleWebKit/538.1 "
        "(KHTML, like Gecko) Version/2.4.0 TV Safari/538.1",
    ),
    # Not a television. Kept for the comparison an analyst runs when a CDN is suspected of
    # serving Smart-TV clients differently.
    "desktop": UserAgentProfile(
        "Desktop Chrome (comparison)",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    ),
}

# The profile a job uses when nothing else says otherwise. A deployment changes this in
# Settings, which is read through `default_ua_profile()`; this is the fallback when no
# setting has been stored.
DEFAULT_UA_PROFILE = "tizen10"

USER_AGENT_PROFILES: dict[str, str] = {
    key: profile.user_agent for key, profile in USER_AGENT_PROFILE_TABLE.items()
}

TIZEN_USER_AGENT = USER_AGENT_PROFILES[DEFAULT_UA_PROFILE]


class VpbMode(str, Enum):
    """Virtual Player Buffer sensitivity."""

    STRICT = "STRICT"
    NORMAL = "NORMAL"
    OUTAGE_ONLY = "OUTAGE_ONLY"


class Thresholds(BaseModel):
    """Analysis thresholds (§6.1). Persisted in the DB, editable in the Settings tab."""

    rebuffer_ratio_threshold: float = 0.25
    cross_variant_msn_error_spread: int = 5
    # Renditions are polled independently, so one can carry a discontinuity the next has not
    # published yet and the counters differ for a poll or two. Past this spread the ladder
    # disagrees by more than poll skew can account for.
    cross_variant_dsn_tolerance: int = 2
    stale_playlist_factor: float = 1.5
    download_ratio_warn: float = 0.5
    download_ratio_error: float = 1.0
    ttfb_budget_ms: int = 1500
    bandwidth_overshoot_tolerance: float = 0.10
    extinf_vs_actual_tolerance_s: float = 0.1
    pts_gap_tolerance_ms: int = 50
    av_skew_normal_ms: int = 40
    av_skew_error_ms: int = 200
    tiny_segment_bytes: int = 32768
    min_live_window_multiple: int = 3
    lowest_rung_max_kbps: int = 800
    max_adjacent_rung_ratio: float = 2.0
    playlist_cache_max_age_factor: float = 0.5
    request_timeout_s: float = 5.0
    segment_extinf_max_ratio_to_td: float = 1.5
    segment_extinf_absolute_max_s: float = 120.0
    av_pts_delta_critical_ms: int = 1000
    # A demuxed rung and its audio rendition are polled independently, so the audio segment
    # matching a video segment is often not published yet at the moment the video one is
    # sampled. The pair is held for this many playlist refreshes before it is given up on,
    # rather than measured against whichever audio segment happens to be there.
    av_pair_defer_refreshes: int = 3
    # How far two segments' decode times may sit apart and still be the same moment on the
    # timeline. Audio and video are segmented on their own boundaries, so a pair that shares
    # a media sequence number still starts a fraction of a second apart by design.
    av_pair_overlap_tolerance_s: float = 1.0
    master_repoll_interval_s: int = 20
    # A packager recomputes BANDWIDTH and AVERAGE-BANDWIDTH per poll, so a small drift is
    # ordinary. Past this fraction the declared rate no longer describes the rung and ABR
    # picks against a figure that is not true.
    bandwidth_variation_tolerance: float = 0.05

    # Virtual Player Buffer.
    #
    # Plus Player's multiqueue is bounded by a byte cap and a time cap together, and the
    # smaller one binds: at 7.5 Mbit/s the FHD cap of 3 MB is 3.2 s of media, nowhere near
    # its 15 s. The profile is chosen from the tallest rung the ladder offers — a ladder
    # topping out at 1080p is FHD, one reaching 2160p is UHD.
    # The byte caps are implemented and configurable, and applied only when this is on.
    # Taken literally they say a healthy 1080p channel underruns every segment — 3 MB is
    # 3.2 s at 7.5 Mbit/s, less than one segment — which is not what devices do. Section 2
    # of the player document lists `OutputMgr` as a separate queue holding downloaded
    # segments, so the byte figures describe the decoder-side multiqueue rather than the
    # buffer that governs rebuffering. The time figures are applied; these wait on the
    # player team confirming which queue they size.
    vpb_apply_byte_caps: bool = False
    vpb_fhd_total_mb: float = 3.0
    vpb_fhd_total_s: float = 15.0
    vpb_uhd_total_mb: float = 60.0
    vpb_uhd_total_s: float = 15.0
    vpb_uhd_min_height: int = 2160
    # Multiqueue watermarks, as fractions of the profile total. Startup fills to the lower
    # one so playback begins quickly; a resume after an underrun fills to the higher one so
    # it does not empty again immediately.
    vpb_startup_fraction: float = 0.33
    vpb_resume_fraction: float = 0.66
    vpb_low_watermark_fraction: float = 0.01
    vpb_mode: VpbMode = VpbMode.NORMAL
    vpb_outage_threshold_s: float = 2.0

    # CASCADA rebuffering data.
    #
    # CASCADA reports `rebuffering_ratio` in percent — a value of 0.159 is 0.159% — which is
    # not the same quantity as `rebuffer_ratio_threshold` above, a fraction where 0.25 means
    # 25%. The two are a hundredfold apart, so the field metric carries its own threshold and
    # the comparison lives in one place, `app.cascada.series.is_above`.
    cascada_rebuffering_threshold_pct: float = 0.25
    # The current window. CASCADA answers with this window tagged `origin` and the week before
    # it tagged `comparison`, which is what makes the week-over-week overlay possible.
    cascada_window_days: int = 7
    # A country scan is one call per channel, so the whole country is walked a few at a time.
    cascada_scan_concurrency: int = 4
    # How long a stored channel window is served before CASCADA is asked again.
    cascada_cache_ttl_minutes: int = 45

    incident_open_s: float = 10.0
    incident_clear_s: float = 60.0
    nth_segment_sampling_other_rungs: int = 3
    ladder_sweep_interval_s: int = 300

    # Evidence recording window around an incident, in seconds.
    evidence_window_s: float = 30.0
    # How much sampled media an evidence bundle may carry. A rung sampled in full for a day
    # is tens of gigabytes, so the store keeps a rolling window: the newest segments per
    # rendition, under one overall budget, with anything sampled during an incident evicted
    # last. A bundle states how many segments it holds and how many were dropped.
    #
    # **This budget is per job, and aging runs record evidence by default.** The analyzer's
    # worst case is therefore `evidence_max_bytes * rba_max_concurrent_jobs` held in memory —
    # 480 MB at these defaults and twenty concurrent jobs. Raise it only against the memory
    # the host actually has.
    evidence_max_bytes: int = 24 * 1024 * 1024
    # Twelve segments of a six-second rung is just over a minute of each rendition, which is
    # long enough to carry an incident and its approach.
    evidence_segments_per_rendition: int = 12

    # Tizen segment retry policy modelled by the Virtual Player Buffer.
    segment_retry_attempts: int = 2
    segment_retry_backoff_s: float = 0.5


class Settings(BaseSettings):
    """Process configuration. Values come from `backend/.env`, which is git-ignored."""

    model_config = SettingsConfigDict(
        env_file=(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="",
    )

    db_engine: str = Field(default="sqlite")
    db_host: str = Field(default="")
    db_port: int = Field(default=0)
    db_user: str = Field(default="")
    db_password: str = Field(default="")
    db_name: str = Field(default="rba")
    db_table_prefix: str = Field(default="")

    rba_host: str = "0.0.0.0"
    rba_port: int = 8010
    rba_public_origin: str = ""
    rba_cors_origins: str = "http://107.109.131.68:8080,http://localhost:5173"

    rba_data_dir: Path = BACKEND_ROOT / "var"
    rba_reports_dir: Path = BACKEND_ROOT / "var" / "reports"
    rba_evidence_dir: Path = BACKEND_ROOT / "var" / "evidence"

    # Where a rendered report lives: "database" keeps the bytes in the reports table and
    # writes nothing to disk; "both" also mirrors a copy into RBA_REPORTS_DIR. Evidence and
    # bulk archives are built in memory and streamed either way.
    rba_report_storage: str = "database"

    rba_max_concurrent_jobs: int = 20
    rba_bulk_default_concurrency: int = 5
    rba_per_host_connections: int = 8
    rba_sample_retention_days: int = 30

    rba_log_level: str = "INFO"

    # CASCADA. The session cookies belong in `backend/.env`, never in git; `.env.example`
    # carries the names with empty values. An operator can paste a session in the Settings
    # tab instead, which is stored in the settings table rather than on disk.
    cascada_base_url: str = "https://cascada.samsungcloud.tv"
    cascada_sessionid: str = ""
    cascada_csrftoken: str = ""
    # `limit` is computed from the window the call asks for; this is the headroom added on
    # top, so a window that grows by a few minutes between the request and the answer is
    # still served whole.
    cascada_row_limit_margin: int = 600
    cascada_timeout_s: float = 60.0

    # DRM. The CPIX credentials are a filesystem path or an HTTPS URL, never key material:
    # `.env.example` carries the names with empty values, and an operator sets them either
    # there or in Settings → DRM, which stores them in the settings table. Nothing about DRM
    # is per-channel — a protected playback URL is pasted into Realtime, Aging, Bulk or a
    # batch exactly like a clear one.
    rba_drm_enabled: bool = True
    cpix_endpoint: str = ""
    cpix_client_cert: str = ""
    cpix_client_key: str = ""
    cpix_server_cert: str = ""
    cpix_content_id: str = "rba"
    # The Widevine licence server the player acquires a licence from, relayed by
    # `POST /api/drm/license` so a browser never has to reach it directly.
    drm_license_url: str = ""

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.rba_cors_origins.split(",") if o.strip()]

    @property
    def database_url(self) -> str:
        """Async SQLAlchemy URL. Credentials stay in the environment; this is never logged."""
        from urllib.parse import quote_plus

        engine = self.db_engine.lower()
        if engine in ("sqlite", ""):
            self.rba_data_dir.mkdir(parents=True, exist_ok=True)
            return f"sqlite+aiosqlite:///{self.rba_data_dir / 'rba.db'}"
        pwd = quote_plus(self.db_password)
        if engine in ("mysql", "mariadb"):
            driver = "aiomysql"
            return f"mysql+{driver}://{self.db_user}:{pwd}@{self.db_host}:{self.db_port}/{self.db_name}"
        if engine in ("postgres", "postgresql"):
            return (
                f"postgresql+asyncpg://{self.db_user}:{pwd}@"
                f"{self.db_host}:{self.db_port}/{self.db_name}"
            )
        raise ValueError(f"Unsupported DB_ENGINE: {self.db_engine}")

    @property
    def server_database_url(self) -> str:
        """URL without a database name, used once to create the `rba` database."""
        from urllib.parse import quote_plus

        engine = self.db_engine.lower()
        pwd = quote_plus(self.db_password)
        if engine in ("mysql", "mariadb"):
            return f"mysql+aiomysql://{self.db_user}:{pwd}@{self.db_host}:{self.db_port}"
        if engine in ("postgres", "postgresql"):
            return (
                f"postgresql+asyncpg://{self.db_user}:{pwd}@{self.db_host}:{self.db_port}/postgres"
            )
        raise ValueError(f"Unsupported DB_ENGINE: {self.db_engine}")

    def table_name(self, base: str) -> str:
        return f"{self.db_table_prefix}{base}"

    @property
    def reports_on_disk(self) -> bool:
        """True when a rendered report is also mirrored into RBA_REPORTS_DIR."""
        return self.rba_report_storage.lower() in ("disk", "both")

    @property
    def reports_in_database(self) -> bool:
        """True when the report bytes are stored in the reports table."""
        return self.rba_report_storage.lower() in ("database", "db", "both", "")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


_thresholds = Thresholds()


def get_thresholds() -> Thresholds:
    """Current thresholds. Replaced at startup by the values stored in the DB."""
    return _thresholds


def set_thresholds(new: Thresholds) -> Thresholds:
    global _thresholds
    _thresholds = new
    return _thresholds


# The profile every job starts with, replaced at startup and on save by what Settings holds.
# It lived as a hardcoded literal in five places, so the Settings selection was stored and
# never read: changing it changed nothing. One value, one reader.
_default_ua_profile = DEFAULT_UA_PROFILE


def default_ua_profile() -> str:
    """The User-Agent profile a job uses unless it names another one."""
    return _default_ua_profile


def set_default_ua_profile(profile: str) -> str:
    """Adopt the stored default. A profile the table no longer declares is refused.

    A stale id must not silently become somebody else's User-Agent, for the same reason a
    stale rule-severity override is dropped rather than carried onto whichever rule takes
    that identifier next.
    """
    global _default_ua_profile
    _default_ua_profile = profile if profile in USER_AGENT_PROFILE_TABLE else DEFAULT_UA_PROFILE
    return _default_ua_profile
