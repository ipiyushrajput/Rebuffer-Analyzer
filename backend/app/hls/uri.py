"""URI resolution.

Child playlist and segment URIs resolve against the **final post-redirect URL**. Resolving
against the originally requested URL is the bug that makes an analyzer fetch the wrong host
after a 302, so this is the only resolution helper in the codebase.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from app.net.fetcher import strip_component_suffix


def resolve(base_final_url: str, uri: str) -> str:
    """Resolve ``uri`` against the final URL of the playlist that referenced it."""
    uri = strip_component_suffix(uri.strip())
    if not uri:
        return uri
    if "://" in uri:
        return uri
    return urljoin(base_final_url, uri)


def propagate_query(parent_url: str, child_url: str, keys: set[str] | None = None) -> str:
    """Copy the parent's query parameters onto a child URI that carries none.

    This models what a naive player does with tokenised URLs: a relative child inherits the
    parent's query string. RBA uses it to prove whether a channel depends on that behaviour,
    never to change how a URL is fetched for a real check.
    """
    parent_q = dict(parse_qsl(urlsplit(parent_url).query, keep_blank_values=True))
    if keys is not None:
        parent_q = {k: v for k, v in parent_q.items() if k in keys}
    if not parent_q:
        return child_url
    parts = urlsplit(child_url)
    child_q = dict(parse_qsl(parts.query, keep_blank_values=True))
    merged = {**parent_q, **child_q}
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(merged, doseq=True), parts.fragment)
    )


def same_host(a: str, b: str) -> bool:
    return (urlsplit(a).hostname or "").lower() == (urlsplit(b).hostname or "").lower()


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


# SSAI packagers mint a new session UUID on every master request. Comparing two CDN
# responses without normalising these first produces false "split brain" findings, so the
# session-bearing parameters are blanked before comparison.
SESSION_PARAM_NAMES = frozenset(
    {
        "aws.sessionid",
        "sessionid",
        "session_id",
        "session",
        "sid",
        "aws.sessionId",
        "hdnts",
        "hdnea",
        "token",
        "auth",
        "akamai_token",
        "cdnsession",
    }
)


def normalize_session_tokens(url: str) -> str:
    """Blank session-bearing parameters and path UUIDs so two URLs can be compared."""
    import re

    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    normalised = [
        (k, "<session>") if k.lower() in {n.lower() for n in SESSION_PARAM_NAMES} else (k, v)
        for k, v in q
    ]
    path = re.sub(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        "<uuid>",
        parts.path,
    )
    return urlunsplit(
        (parts.scheme, parts.netloc, path, urlencode(normalised, doseq=True), parts.fragment)
    )
