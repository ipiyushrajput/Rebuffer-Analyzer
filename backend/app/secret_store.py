"""Secrets operators store through the Settings tab, encrypted before they reach the database.

The CASCADA session and the alert webhook URL are credentials an operator pastes in the
Settings tab. They live in the settings table, which every database backup copies; with
`RBA_SECRETS_KEY` set (a Fernet key, `python -m app.cli secret-key`) each value is encrypted
with it before it is written and decrypted when it is read, so a backup holds ciphertext.

A value written before a key was configured has no prefix and is read as it is, so turning
encryption on needs no migration; it is sealed the next time it is saved. A sealed value that
does not decrypt — the key was changed or lost — reads as unset and says why in the log,
without the value: the operator pastes it again.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"


def generate_key() -> str:
    return Fernet.generate_key().decode()


def _fernet() -> Fernet | None:
    key = get_settings().rba_secrets_key.strip()
    if not key:
        return None
    return Fernet(key.encode())


def enabled() -> bool:
    return _fernet() is not None


def seal(value: str) -> str:
    """The value as it is to be stored: encrypted when a key is configured."""
    if not value or value.startswith(PREFIX):
        return value
    fernet = _fernet()
    if fernet is None:
        return value
    return PREFIX + fernet.encrypt(value.encode()).decode()


def unseal(value: str) -> str:
    """The stored value as it was pasted. Unsealed values pass through unchanged."""
    if not value or not value.startswith(PREFIX):
        return value
    fernet = _fernet()
    if fernet is None:
        logger.error(
            "a stored secret is encrypted and RBA_SECRETS_KEY is not set; it reads as unset"
        )
        return ""
    try:
        return fernet.decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken:
        logger.error("a stored secret does not decrypt with RBA_SECRETS_KEY; it reads as unset")
        return ""
