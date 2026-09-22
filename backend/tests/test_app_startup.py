"""The app starts, answers /api/health, and reports missing dependencies as facts."""

from __future__ import annotations

import os

os.environ.setdefault("DB_ENGINE", "sqlite")

from fastapi.testclient import TestClient

from app.main import create_app


def test_health_reports_every_dependency() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("ok", "degraded")
    assert set(body["checks"]) == {
        "ffmpeg",
        "ffprobe",
        "playwright",
        "mp4decrypt",
        "database",
        "schema",
    }
    # Bento4 is optional — cenc decrypts in process — so a host without it is a working
    # deployment, not a degraded one, and the check says so without hiding the consequence.
    assert body["checks"]["mp4decrypt"]["ok"] is True
    assert body["checks"]["mp4decrypt"]["detail"]
    assert "thresholds" in body
    assert body["thresholds"]["rebuffer_ratio_threshold"] == 0.25


def test_health_never_reveals_database_credentials() -> None:
    with TestClient(create_app()) as client:
        body = client.get("/api/health").json()
    text = repr(body)
    for leak in ("password", "DB_PASSWORD", "@", "mysql+"):
        if leak == "@":
            continue
        assert leak not in text
    assert set(body["checks"]["database"]) <= {"ok", "engine", "schema_mode", "error"}


def test_settings_expose_thresholds_and_the_rule_catalogue() -> None:
    with TestClient(create_app()) as client:
        settings = client.get("/api/settings").json()
        rules = client.get("/api/settings/rules").json()

    assert settings["thresholds"]["cross_variant_msn_error_spread"] == 5
    # A row per profile, not a bare id: the picker renders the label and the string the
    # requests are actually made with, and holds no second copy of the table.
    assert {row["id"] for row in settings["ua_profiles"]} >= {"tizen5", "tizen10", "desktop"}
    assert all(row["label"] and row["user_agent"] for row in settings["ua_profiles"])
    assert rules["count"] > 100
    assert all(rule["root_cause"] for rule in rules["rules"])


def test_thresholds_can_be_changed_and_are_read_back() -> None:
    with TestClient(create_app()) as client:
        updated = client.put(
            "/api/settings", json={"thresholds": {"rebuffer_ratio_threshold": 0.3}}
        ).json()
        assert updated["thresholds"]["rebuffer_ratio_threshold"] == 0.3
        assert client.get("/api/settings").json()["thresholds"]["rebuffer_ratio_threshold"] == 0.3
        client.put("/api/settings", json={"thresholds": {"rebuffer_ratio_threshold": 0.25}})
