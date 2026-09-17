"""How a CASCADA call proves who it is.

CASCADA is a Django application behind the corporate identity provider, and that provider
requires MFA, so the analyzer cannot log itself in with a stored password. It carries a
session instead: the `sessionid` and `csrftoken` cookies an operator already holds.

The browser cannot hand its own CASCADA session over. Those cookies belong to
`cascada.samsungcloud.tv`, so the browser never attaches them to a request aimed at the
analyzer, and a page on the analyzer's origin cannot read them; Django marks `sessionid`
HttpOnly, so script on a CASCADA page cannot read it either. The operator therefore pastes
the session once, in the Settings tab, and the analyzer holds it server-side.

Two sources are supported, and a third drops in without touching anything that calls this:

* `env_session()` — `CASCADA_SESSIONID` / `CASCADA_CSRFTOKEN` from `backend/.env`, which is
  git-ignored. This is how the deployment host is configured.
* `load_stored_session()` — a session pasted in the Settings tab and kept in the settings
  table, so a restart and a second analyzer instance both keep working.

The cookie value never leaves the backend: it is never returned by an endpoint, never
logged, and never written into a report. `describe()` is what the UI sees.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import get_settings
from app.db import session as db_session
from app.db.models import SettingRow

logger = logging.getLogger(__name__)

SESSION_KEY = "cascada_session"

# The cookies a CASCADA session is made of. `sessionid` is the one that authenticates;
# `csrftoken` rides along because the application expects the pair.
SESSION_COOKIE = "sessionid"
CSRF_COOKIE = "csrftoken"
# Django sets a third cookie carrying one-shot UI notices. It is accepted when pasted and
# passed back, because a session copied whole is easier to paste than one picked apart.
MESSAGES_COOKIE = "messages"

ACCEPTED_COOKIES = (SESSION_COOKIE, CSRF_COOKIE, MESSAGES_COOKIE)


class CascadaAuthError(Exception):
    """The session is absent, rejected or expired. The operator pastes a new one."""


def mask(value: str) -> str:
    """The last four characters of a secret, which identify it without disclosing it."""
    cleaned = value.strip()
    if not cleaned:
        return ""
    return f"…{cleaned[-4:]}" if len(cleaned) > 4 else "…"


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Read the cookies out of a pasted `Cookie:` header line.

    Devtools' "Copy as cURL" produces the whole header, which is one copy and one paste for
    the operator. A bare `name=value; name=value` string is the same thing without the label,
    and both are accepted. Cookies CASCADA does not need are dropped rather than forwarded.
    """
    text = raw.strip()
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:") :].strip()
    # A pasted cURL line wraps the header in quotes.
    text = text.strip().strip("'\"")

    found: dict[str, str] = {}
    for part in text.split(";"):
        name, sep, value = part.strip().partition("=")
        if not sep:
            continue
        key = name.strip().lower()
        if key in ACCEPTED_COOKIES and value.strip():
            found[key] = value.strip()
    return found


def cookie_header(cookies: dict[str, str]) -> str:
    """The `Cookie` request header for a set of cookies, in a stable order."""
    return "; ".join(f"{name}={cookies[name]}" for name in ACCEPTED_COOKIES if cookies.get(name))


@dataclass(frozen=True, slots=True)
class SessionState:
    """What the UI is allowed to know about the stored session."""

    configured: bool
    source: str
    masked: str
    last_validated_at: str | None
    valid: bool | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "source": self.source,
            "masked": self.masked,
            "last_validated_at": self.last_validated_at,
            "valid": self.valid,
            "detail": self.detail,
        }


class CascadaAuth(Protocol):
    """What a CASCADA call needs from whatever proves its identity."""

    source: str

    def headers(self) -> dict[str, str]:
        """Request headers that authenticate the call, or raise if there is no session."""

    def describe(self) -> SessionState:
        """What may be shown on screen. Never the cookie itself."""


@dataclass(slots=True)
class _CookieAuth:
    """A session held as cookies. The two concrete providers differ only in where they read."""

    cookies: dict[str, str]
    source: str
    last_validated_at: str | None = None
    valid: bool | None = None
    detail: str = ""

    def headers(self) -> dict[str, str]:
        if not self.cookies.get(SESSION_COOKIE):
            raise CascadaAuthError(
                "No CASCADA session is configured. Paste a session in the Settings tab: open "
                "CASCADA in a signed-in browser tab, copy the request's Cookie header from "
                "devtools, and paste it into the CASCADA session panel."
            )
        headers = {"Cookie": cookie_header(self.cookies), "Accept": "application/json"}
        csrf = self.cookies.get(CSRF_COOKIE)
        if csrf:
            # Django checks this header against the cookie on any non-idempotent call, and
            # accepts it on a GET, so sending it costs nothing and keeps the pair consistent.
            headers["X-CSRFToken"] = csrf
        return headers

    def describe(self) -> SessionState:
        return SessionState(
            configured=bool(self.cookies.get(SESSION_COOKIE)),
            source=self.source,
            masked=mask(self.cookies.get(SESSION_COOKIE, "")),
            last_validated_at=self.last_validated_at,
            valid=self.valid,
            detail=self.detail,
        )


def env_session() -> _CookieAuth:
    """The session configured on the deployment host, from the git-ignored `backend/.env`."""
    settings = get_settings()
    cookies = {
        SESSION_COOKIE: settings.cascada_sessionid.strip(),
        CSRF_COOKIE: settings.cascada_csrftoken.strip(),
    }
    return _CookieAuth(
        cookies={k: v for k, v in cookies.items() if v},
        source="environment",
        detail="Read from the analyzer host's environment.",
    )


async def load_stored_session() -> _CookieAuth:
    """The session pasted in the Settings tab, from the settings table."""
    stored: dict[str, Any] = {}
    try:
        async with db_session.session_scope() as session:
            row = await session.get(SettingRow, SESSION_KEY)
            if row is not None and isinstance(row.value, dict):
                stored = row.value
    except Exception as exc:
        # A database that does not answer is not a silent no-session: it is reported.
        logger.warning("the stored CASCADA session could not be read: %s", type(exc).__name__)
        return _CookieAuth(
            cookies={},
            source="stored",
            detail=f"The settings table did not answer ({type(exc).__name__}).",
        )

    cookies = {
        name: str(stored.get(name, "")).strip()
        for name in ACCEPTED_COOKIES
        if str(stored.get(name, "")).strip()
    }
    return _CookieAuth(
        cookies=cookies,
        source="stored",
        last_validated_at=stored.get("last_validated_at"),
        valid=stored.get("valid"),
        detail=str(stored.get("detail", "")),
    )


async def save_stored_session(cookies: dict[str, str], *, valid: bool, detail: str) -> _CookieAuth:
    """Store a pasted session together with the result of validating it."""
    if not cookies.get(SESSION_COOKIE):
        raise CascadaAuthError(
            "The pasted text carries no `sessionid` cookie. Copy the whole Cookie header from "
            "a signed-in CASCADA request in devtools, or paste the sessionid value on its own."
        )

    record: dict[str, Any] = {
        **{name: cookies[name] for name in ACCEPTED_COOKIES if cookies.get(name)},
        "last_validated_at": dt.datetime.now(dt.UTC).isoformat(),
        "valid": valid,
        "detail": detail,
    }
    async with db_session.session_scope() as session:
        row = await session.get(SettingRow, SESSION_KEY)
        if row is None:
            session.add(SettingRow(key=SESSION_KEY, value=record))
        else:
            row.value = record

    return await load_stored_session()


async def clear_stored_session() -> None:
    """Forget the pasted session.

    An operator who pasted the wrong session, or who is handing the host to someone else,
    needs it gone rather than overwritten.
    """
    async with db_session.session_scope() as session:
        row = await session.get(SettingRow, SESSION_KEY)
        if row is not None:
            await session.delete(row)


async def mark_stored_session(*, valid: bool, detail: str) -> None:
    """Record what a live call discovered about the stored session, and when.

    A session that stops working is a fact the operator needs on screen before the next scan,
    not one they infer from a failed run.
    """
    try:
        async with db_session.session_scope() as session:
            row = await session.get(SettingRow, SESSION_KEY)
            if row is None or not isinstance(row.value, dict):
                return
            row.value = {
                **row.value,
                "valid": valid,
                "detail": detail,
                "last_validated_at": dt.datetime.now(dt.UTC).isoformat(),
            }
    except Exception as exc:
        logger.warning("the CASCADA session state was not updated: %s", type(exc).__name__)


async def resolve_auth() -> _CookieAuth:
    """The session a call should use.

    The host's own environment wins, because a deployment configured with a service session is
    stating which identity its calls go out as. A pasted session is what an operator uses on a
    host that has none.
    """
    env = env_session()
    if env.cookies.get(SESSION_COOKIE):
        return env
    return await load_stored_session()
