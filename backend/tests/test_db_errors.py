"""What a database failure tells an operator, and what it must never tell anyone.

Two rules meet here. The analyzer never fails silently, so a failure has to say what went
wrong: a driver that will not import, a host that refuses the connection and a password that
is wrong are three different jobs and must not arrive as one word. And credentials never
leave the process, so the message that says which is stripped of the configured password
first — a SQLAlchemy error will quote the connection URL given the chance.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.db import session as db_session


@pytest.fixture()
def password() -> str:
    """A configured password, restored afterwards so no other test sees it."""
    settings = get_settings()
    previous = settings.db_password
    settings.db_password = "sup3r-s3cret/pw"
    try:
        yield settings.db_password
    finally:
        settings.db_password = previous


def test_the_reason_names_what_actually_failed() -> None:
    """The class alone sent an operator to read an ASGI traceback to find the cause."""
    exc = ImportError("cannot import name 'escape_bytes_prefixed' from 'pymysql.converters'")

    described = db_session.describe_error(exc)

    assert described.startswith("ImportError: ")
    assert "escape_bytes_prefixed" in described


def test_a_quoted_connection_url_loses_its_password(password: str) -> None:
    """SQLAlchemy quotes the URL it could not connect with, credentials included."""
    exc = OSError(
        f"Can't connect to MySQL server on 'mysql+aiomysql://rba:{password}@10.0.0.9:3306/rba'"
    )

    described = db_session.describe_error(exc)

    assert password not in described
    assert "***" in described
    # Everything an operator needs to act is still there.
    assert "10.0.0.9:3306" in described


def test_a_percent_encoded_password_is_removed_too(password: str) -> None:
    """The URL carries the password quoted, so the raw spelling alone is not enough."""
    from urllib.parse import quote_plus

    encoded = quote_plus(password)
    assert encoded != password, "this case is only meaningful for a password that encodes"

    described = db_session.describe_error(OSError(f"refused for rba:{encoded}@host"))

    assert encoded not in described
    assert "***" in described


def test_a_deployment_with_no_password_still_gets_its_reason() -> None:
    settings = get_settings()
    previous = settings.db_password
    settings.db_password = ""
    try:
        assert "refused" in db_session.describe_error(OSError("connection refused"))
    finally:
        settings.db_password = previous


def test_the_health_endpoint_reports_a_reason_without_credentials(password: str) -> None:
    """`/api/health` is read by anyone who can reach the analyzer."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        body = client.get("/api/health").json()

    database = body["checks"]["database"]
    assert "error" not in database or password not in str(database["error"])
