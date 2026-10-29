"""Operational surfaces: Prometheus metrics, and a pool sized for the jobs it serves."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.jobs import lease
from app.main import create_app


def test_metrics_are_prometheus_text_naming_this_instance() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/metrics")
    assert response.status_code == 200
    body = response.text
    for name in (
        "streamlens_up",
        "streamlens_jobs_capacity",
        "streamlens_db_pool_checked_out",
        "streamlens_event_loop_lag_max_ms",
        "streamlens_archive_failures_total",
        "streamlens_process_resident_bytes",
    ):
        assert f"# TYPE {name} " in body, name
    assert f'instance="{lease.instance_id()}"' in body
    assert 'storage="local"' in body


def test_a_server_database_gets_a_sized_pool(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.config import Settings
    from app.db import session as db_session

    captured: dict[str, object] = {}

    def fake_engine(url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    settings = Settings(db_engine="mysql", db_host="db", db_port=3306, db_user="u", db_password="p")
    monkeypatch.setattr(db_session, "get_settings", lambda: settings)
    monkeypatch.setattr(db_session, "create_async_engine", fake_engine)
    monkeypatch.setattr(db_session, "_engine", None)
    db_session.engine()
    monkeypatch.setattr(db_session, "_engine", None)

    assert captured["pool_size"] == settings.rba_db_pool_size
    assert captured["max_overflow"] == settings.rba_db_max_overflow
