"""A request as a command a CDN or packager engineer can run themselves."""

from __future__ import annotations

import shlex


def curl_command(url: str, user_agent: str = "") -> str:
    """The request exactly as the analyzer sent it, for a CDN or packager to reproduce.

    `-k` because the analyzer does not verify TLS (it reports the chain instead), `-L` off on
    purpose: the analyzer follows redirects itself and records each hop, and `-D -` prints the
    headers each hop answered with.
    """
    parts = ["curl", "-sS", "-k", "-D", "-", "-o", "/dev/null"]
    if user_agent:
        parts += ["-A", user_agent]
    parts.append(url)
    return " ".join(shlex.quote(part) for part in parts)


__all__ = ["curl_command"]
