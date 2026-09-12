"""TLS inspection.

Fetches run with verification disabled so a broken chain never blocks analysis. This module
opens a separate, unverified connection purely to read the certificate and negotiated
parameters, so the chain is still reported. Disabling verification hides nothing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import socket
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

# Ciphers a 2016-2018 Tizen TLS stack negotiates. Presence is reported so the team can see
# whether the edge still offers a suite those devices can use.
LEGACY_TIZEN_CIPHERS = (
    "ECDHE-RSA-AES128-SHA",
    "ECDHE-RSA-AES256-SHA",
    "AES128-SHA",
    "AES256-SHA",
    "ECDHE-RSA-AES128-GCM-SHA256",
)


@dataclass(slots=True)
class TlsResult:
    host: str
    port: int
    negotiated_version: str | None = None
    negotiated_cipher: str | None = None
    subject: str = ""
    issuer: str = ""
    not_before: str | None = None
    not_after: str | None = None
    days_to_expiry: int | None = None
    san: list[str] = field(default_factory=list)
    san_matches_host: bool | None = None
    chain_length: int = 0
    chain_complete: bool | None = None
    key_type: str = ""
    key_bits: int | None = None
    handshake_ms: float = 0.0
    legacy_cipher_offered: dict[str, bool] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "version": self.negotiated_version,
            "cipher": self.negotiated_cipher,
            "subject": self.subject,
            "issuer": self.issuer,
            "not_before": self.not_before,
            "not_after": self.not_after,
            "days_to_expiry": self.days_to_expiry,
            "san": self.san,
            "san_matches_host": self.san_matches_host,
            "chain_length": self.chain_length,
            "chain_complete": self.chain_complete,
            "key_type": self.key_type,
            "key_bits": self.key_bits,
            "handshake_ms": self.handshake_ms,
            "legacy_cipher_offered": self.legacy_cipher_offered,
            "error": self.error,
        }


def _host_matches(pattern: str, host: str) -> bool:
    pattern = pattern.lower().strip()
    host = host.lower().strip()
    if pattern == host:
        return True
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix) and host.count(".") == pattern.count(".")
    return False


def _blocking_inspect(host: str, port: int) -> TlsResult:
    import time

    result = TlsResult(host=host, port=port)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # §0.1 — inspection must survive a broken chain.

    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=6.0) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                result.handshake_ms = (time.perf_counter() - t0) * 1000
                result.negotiated_version = tls.version()
                cipher = tls.cipher()
                result.negotiated_cipher = cipher[0] if cipher else None
                der = tls.getpeercert(binary_form=True)
                cert = tls.getpeercert()
                if der:
                    result.chain_length = 1
                if cert:
                    # `getpeercert()` returns a loosely typed mapping; every field read here
                    # is a string or a tuple of pairs, so the values are narrowed explicitly.
                    fields: dict[str, Any] = dict(cert)
                    result.subject = _rdn_to_str(fields.get("subject", ()))
                    result.issuer = _rdn_to_str(fields.get("issuer", ()))
                    not_before = fields.get("notBefore")
                    not_after = fields.get("notAfter")
                    result.not_before = str(not_before) if not_before else None
                    result.not_after = str(not_after) if not_after else None
                    result.san = [
                        str(value)
                        for kind, value in tuple(fields.get("subjectAltName", ()))
                        if kind == "DNS"
                    ]
                    result.san_matches_host = any(_host_matches(s, host) for s in result.san)
                    if result.not_after:
                        try:
                            expiry = dt.datetime.strptime(
                                result.not_after, "%b %d %H:%M:%S %Y %Z"
                            ).replace(tzinfo=dt.UTC)
                            result.days_to_expiry = (expiry - dt.datetime.now(dt.UTC)).days
                        except ValueError:
                            result.days_to_expiry = None
    except (TimeoutError, OSError, ssl.SSLError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    # A second handshake with default verification tells us whether the chain the server
    # sends is complete against the system trust store.
    verify_ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=6.0) as sock:
            with verify_ctx.wrap_socket(sock, server_hostname=host) as tls:
                chain = getattr(tls, "get_verified_chain", None)
                if chain is not None:
                    result.chain_length = len(chain())
                result.chain_complete = True
    except ssl.SSLCertVerificationError as exc:
        result.chain_complete = False
        result.error = result.error or f"Chain verification failed: {exc.verify_message}"
    except (TimeoutError, OSError, ssl.SSLError):
        result.chain_complete = None

    for name in LEGACY_TIZEN_CIPHERS:
        result.legacy_cipher_offered[name] = _offers_cipher(host, port, name)
    return result


def _offers_cipher(host: str, port: int, cipher: str) -> bool:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers(cipher)
    except ssl.SSLError:
        return False
    try:
        with socket.create_connection((host, port), timeout=4.0) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                return True
    except (TimeoutError, OSError, ssl.SSLError):
        return False


def _rdn_to_str(rdns: Any) -> str:
    parts = []
    for rdn in rdns:
        for key, value in rdn:
            parts.append(f"{key}={value}")
    return ", ".join(parts)


async def inspect(url: str) -> TlsResult | None:
    """Inspect the TLS endpoint behind ``url``. Returns ``None`` for plain HTTP."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        return None
    host = parts.hostname or ""
    port = parts.port or 443
    if not host:
        return None
    return await asyncio.to_thread(_blocking_inspect, host, port)
