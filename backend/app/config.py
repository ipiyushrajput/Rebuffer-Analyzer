"""Settings and analysis thresholds.

Every number a rule compares against lives here. Rules never inline a threshold: they
read it from `Thresholds` so the Settings tab can change behaviour without a code change.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# The Tizen Smart-TV client the TV Plus player identifies as. Every outbound request uses
# this profile unless the job selects another one.
TIZEN_USER_AGENT = (
    "Mozilla/5.0 (SMART-TV; Linux; Tizen 5.0) AppleWebKit/538.1 "
    "(KHTML, like Gecko) Version/5.0 TV Safari/538.1"
)

USER_AGENT_PROFILES: dict[str, str] = {
    "tizen5": TIZEN_USER_AGENT,
    "tizen4": (
        "Mozilla/5.0 (SMART-TV; Linux; Tizen 4.0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Version/4.0 TV Safari/537.36"
    ),
    "tizen6": (
        "Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Version/6.0 TV Safari/537.36"
    ),
    "tizen7": (
        "Mozilla/5.0 (SMART-TV; LINUX; Tizen 7.0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) 92.0.4515.166/7.0 TV Safari/537.36"
    ),
    "desktop": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


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
    master_repoll_interval_s: int = 20
    # A packager recomputes BANDWIDTH and AVERAGE-BANDWIDTH per poll, so a small drift is
    # ordinary. Past this fraction the declared rate no longer describes the rung and ABR
    # picks against a figure that is not true.
    bandwidth_variation_tolerance: float = 0.05

    # Virtual Player Buffer
    vpb_startup_buffer_td_multiple: float = 3.0
    vpb_rebuffer_resume_td_multiple: float = 1.0
    vpb_max_buffer_s: float = 60.0
    vpb_mode: VpbMode = VpbMode.NORMAL
    vpb_outage_threshold_s: float = 2.0

    incident_open_s: float = 10.0
    incident_clear_s: float = 60.0
    nth_segment_sampling_other_rungs: int = 3
    ladder_sweep_interval_s: int = 300

    # Evidence recording window around an incident, in seconds.
    evidence_window_s: float = 30.0

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
