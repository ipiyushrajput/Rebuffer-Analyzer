"""Secrets pasted in the Settings tab reach the database encrypted when a key is configured."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import alerts, secret_store
from app.cascada import auth
from app.config import get_settings
from app.db import session as db_session
from app.db.models import SettingRow
from app.main import create_app


@pytest.fixture(autouse=True)
def api() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture()
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    value = secret_store.generate_key()
    monkeypatch.setattr(get_settings(), "rba_secrets_key", value)
    return value


def test_a_value_is_sealed_and_opened_with_the_key(key: str) -> None:
    sealed = secret_store.seal("sessionid-123")
    assert sealed.startswith(secret_store.PREFIX) and "sessionid-123" not in sealed
    assert secret_store.unseal(sealed) == "sessionid-123"
    assert secret_store.seal(sealed) == sealed, "sealing twice changes nothing"


def test_a_value_stored_before_encryption_reads_as_it_was(key: str) -> None:
    assert secret_store.unseal("plain-legacy-value") == "plain-legacy-value"


def test_without_a_key_values_are_stored_as_they_are(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "rba_secrets_key", "")
    assert secret_store.seal("x") == "x"


def test_a_value_sealed_under_another_key_reads_as_unset(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed = secret_store.seal("webhook-token")
    monkeypatch.setattr(get_settings(), "rba_secrets_key", secret_store.generate_key())
    assert secret_store.unseal(sealed) == ""


async def _stored(name: str) -> dict:  # type: ignore[type-arg]
    async with db_session.session_scope() as session:
        row = await session.get(SettingRow, name)
        assert row is not None
        return dict(row.value)


async def test_the_cascada_session_is_encrypted_in_the_settings_table(key: str) -> None:
    await auth.save_stored_session(
        {"sessionid": "abc123", "csrftoken": "tok"}, valid=True, detail=""
    )
    try:
        raw = await _stored(auth.SESSION_KEY)
        assert raw["sessionid"].startswith(secret_store.PREFIX)
        assert "abc123" not in str(raw)
        loaded = await auth.load_stored_session()
        assert loaded.cookies["sessionid"] == "abc123"
    finally:
        await auth.clear_stored_session()


async def test_the_alert_webhook_is_encrypted_in_the_settings_table(key: str) -> None:
    url = "https://hooks.example/services/T000/B000/secret-token"
    await alerts.save(alerts.AlertSettings(enabled=True, webhook_url=url))
    raw = await _stored(alerts.SETTINGS_KEY)
    assert "secret-token" not in str(raw)
    assert (await alerts.load()).webhook_url == url
    await alerts.save(alerts.AlertSettings())
