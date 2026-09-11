"""Transport detectors: DNS, TLS, HTTP status and timing, CDN behaviour.

Each function measures, compares against a threshold from `app.config`, and raises the
declared rule with the measurement in the evidence. Nothing here writes prose of its own.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from app.analysis.rules import catalogue as R
from app.analysis.rules.base import Finding, Severity, StreamLayer
from app.config import Thresholds
from app.hls.uri import host_of, normalize_session_tokens
from app.net.dns import DnsResult
from app.net.fetcher import FetchResult
from app.net.tls_inspect import TlsResult

PLAYLIST_CONTENT_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
)

MAX_CNAME_DEPTH = 3
EXPIRY_WARNING_DAYS = 14
MAX_REDIRECT_HOPS = 2


def check_dns(result: DnsResult, *, layer: StreamLayer, thresholds: Thresholds) -> list[Finding]:
    findings: list[Finding] = []
    evidence: dict[str, Any] = result.as_dict()

    if result.error:
        findings.append(
            R.NET_RESOLVE_FAIL.raise_finding(
                f"DNS returns no address for {result.host}. {result.error}",
                evidence=evidence,
                stream_layer=layer,
            )
        )
        return findings

    if result.aaaa_records and result.ipv6_reachable is False:
        findings.append(
            R.NET_AAAA_NO_IPV6.raise_finding(
                f"{result.host} publishes {len(result.aaaa_records)} AAAA record(s) "
                f"(first {result.aaaa_records[0]}) and no TCP connection to that address "
                f"completes, while the A record {result.a_records[0] if result.a_records else ''} "
                "does.",
                evidence=evidence,
                stream_layer=layer,
            )
        )

    if result.cname_depth > MAX_CNAME_DEPTH:
        findings.append(
            R.NET_CNAME_DEPTH.raise_finding(
                f"{result.host} resolves through {result.cname_depth} CNAME hops: "
                f"{' -> '.join(result.cname_chain)}. The configured maximum is "
                f"{MAX_CNAME_DEPTH}.",
                evidence=evidence,
                stream_layer=layer,
            )
        )

    if result.resolve_ms > thresholds.ttfb_budget_ms:
        findings.append(
            R.NET_RESOLVE_SLOW.raise_finding(
                f"{result.host} resolves in {result.resolve_ms:.0f} ms against a budget of "
                f"{thresholds.ttfb_budget_ms} ms.",
                evidence=evidence,
                stream_layer=layer,
            )
        )

    if not findings:
        findings.append(
            R.NET_RESOLVED.raise_finding(
                f"{result.host} resolves to {len(result.a_records)} A record(s) in "
                f"{result.resolve_ms:.0f} ms and accepts a TCP connection.",
                evidence=evidence,
                stream_layer=layer,
            )
        )
    return findings


def check_tls(result: TlsResult | None, *, layer: StreamLayer) -> list[Finding]:
    if result is None:
        return []
    findings: list[Finding] = []
    evidence = result.as_dict()

    if result.chain_complete is False:
        findings.append(
            R.TLS_CHAIN_INCOMPLETE.raise_finding(
                f"{result.host}:{result.port} sends {result.chain_length} certificate(s) and the "
                f"chain does not verify against the system trust store. Issuer: {result.issuer}.",
                evidence=evidence,
                stream_layer=layer,
            )
        )

    if result.days_to_expiry is not None and result.days_to_expiry <= EXPIRY_WARNING_DAYS:
        severity = Severity.CRITICAL if result.days_to_expiry <= 0 else Severity.ERROR
        findings.append(
            R.TLS_EXPIRED.raise_finding(
                f"The certificate for {result.host} expires on {result.not_after}, "
                f"{result.days_to_expiry} day(s) from now.",
                evidence=evidence,
                stream_layer=layer,
                severity=severity,
            )
        )

    if result.san_matches_host is False:
        findings.append(
            R.TLS_SAN_MISMATCH.raise_finding(
                f"The certificate SAN list for {result.host} is {result.san} and does not "
                f"cover {result.host}.",
                evidence=evidence,
                stream_layer=layer,
            )
        )

    if result.legacy_cipher_offered and not any(result.legacy_cipher_offered.values()):
        findings.append(
            R.TLS_NO_LEGACY_CIPHER.raise_finding(
                f"{result.host}:{result.port} accepts none of "
                f"{sorted(result.legacy_cipher_offered)}. The negotiated suite with a current "
                f"client is {result.negotiated_cipher}.",
                evidence=evidence,
                stream_layer=layer,
            )
        )

    version = result.negotiated_version or ""
    if version and version not in ("TLSv1.2", "TLSv1.3"):
        findings.append(
            R.TLS_VERSION_LOW.raise_finding(
                f"{result.host}:{result.port} negotiates {version}.",
                evidence=evidence,
                stream_layer=layer,
            )
        )

    if not findings:
        findings.append(
            R.TLS_OK.raise_finding(
                f"{result.host}:{result.port} negotiates {result.negotiated_version} with "
                f"{result.negotiated_cipher}; the chain verifies and the certificate is valid "
                f"for {result.days_to_expiry} more day(s).",
                evidence=evidence,
                stream_layer=layer,
            )
        )
    return findings


def check_http(
    result: FetchResult,
    *,
    layer: StreamLayer,
    thresholds: Thresholds,
    kind: str = "playlist",
    variant: str | None = None,
    listed_in_playlist: bool = False,
) -> list[Finding]:
    """Status, redirect and header checks for one fetch."""
    findings: list[Finding] = []
    evidence: dict[str, Any] = {
        "url": result.requested_url,
        "final_url": result.final_url,
        "status": result.status,
        "kind": kind,
        "timings": result.timings.as_dict(),
        "headers": result.cdn_fingerprint(),
        "bytes": result.bytes_received,
    }

    if result.error and "Timeout" in result.error:
        phase = _slowest_phase(result)
        findings.append(
            R.HTTP_TIMEOUT.raise_finding(
                f"{kind} request to {result.requested_url} did not complete within "
                f"{thresholds.request_timeout_s:.0f} s. The {phase} phase consumed the time.",
                evidence={**evidence, "error": result.error},
                stream_layer=layer,
                variant=variant,
            )
        )
        return findings

    if result.error:
        findings.append(
            (R.SEG_DOWNLOAD_FAIL if kind == "segment" else R.MED_DOWNLOAD_FAIL).raise_finding(
                f"{kind} request to {result.requested_url} failed: {result.error}",
                evidence={**evidence, "error": result.error},
                stream_layer=layer,
                variant=variant,
            )
        )
        return findings

    status_rule = {
        403: R.HTTP_403,
        410: R.HTTP_410,
        429: R.HTTP_429,
    }.get(result.status)
    if result.status == 404:
        # A 404 on a resource the playlist still lists is the rebuffer case; a 404 on the
        # playlist itself is a delivery failure for the whole rung.
        status_rule = R.HTTP_404_LISTED if listed_in_playlist else R.MED_DOWNLOAD_FAIL
    elif 500 <= result.status < 600:
        status_rule = R.HTTP_5XX

    if status_rule is not None:
        findings.append(
            status_rule.raise_finding(
                f"{kind} {result.requested_url} returned HTTP {result.status}.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant,
            )
        )

    if result.redirect_count > MAX_REDIRECT_HOPS:
        findings.append(
            R.HTTP_REDIRECT_DEPTH.raise_finding(
                f"{result.requested_url} reaches media after {result.redirect_count} redirects: "
                + " -> ".join(f"{hop.status} {hop.url}" for hop in result.hops),
                evidence={**evidence, "hops": [hop.as_dict() for hop in result.hops]},
                stream_layer=layer,
                variant=variant,
            )
        )

    dropped = result.dropped_query_keys
    if dropped:
        findings.append(
            R.HTTP_QUERY_DROPPED.raise_finding(
                f"The redirect chain for {result.requested_url} drops "
                f"{sorted(dropped)} from the query string.",
                evidence={**evidence, "hops": [hop.as_dict() for hop in result.hops]},
                stream_layer=layer,
                variant=variant,
            )
        )

    if result.hops and host_of(result.requested_url) != host_of(result.final_url):
        findings.append(
            R.HTTP_HOST_CHANGE.raise_finding(
                f"{result.requested_url} redirects to {result.final_url}; child URIs resolve "
                f"against {host_of(result.final_url)}.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant,
            )
        )

    content_type = (result.header("content-type") or "").split(";")[0].strip().lower()
    if kind == "playlist" and result.ok and content_type not in PLAYLIST_CONTENT_TYPES:
        findings.append(
            R.HTTP_CONTENT_TYPE.raise_finding(
                f"{result.final_url} is served as Content-Type: {content_type or '(absent)'}. "
                "The HLS media types are application/vnd.apple.mpegurl and "
                "application/x-mpegURL.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant,
            )
        )

    declared_length = result.header("content-length")
    if declared_length and declared_length.isdigit() and result.ok:
        declared = int(declared_length)
        if declared != result.bytes_received:
            findings.append(
                R.HTTP_TRUNCATED.raise_finding(
                    f"{result.final_url} declares Content-Length {declared} and delivered "
                    f"{result.bytes_received} bytes.",
                    evidence=evidence,
                    stream_layer=layer,
                    variant=variant,
                )
            )

    encoding = result.header("content-encoding")
    if kind == "segment" and encoding:
        findings.append(
            R.HTTP_ENCODING.raise_finding(
                f"{result.final_url} is served with Content-Encoding: {encoding}.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant,
            )
        )

    ttfb = result.timings.ttfb_ms
    if ttfb is not None and ttfb > thresholds.ttfb_budget_ms:
        findings.append(
            R.HTTP_TTFB_BUDGET.raise_finding(
                f"{result.final_url} returned its first byte after {ttfb:.0f} ms against a "
                f"budget of {thresholds.ttfb_budget_ms} ms.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant,
            )
        )

    if result.header("access-control-allow-origin") is None and result.ok:
        findings.append(
            R.CDN_NO_CORS.raise_finding(
                f"{result.final_url} carries no Access-Control-Allow-Origin header.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant,
            )
        )

    connection = (result.header("connection") or "").lower()
    if "close" in connection:
        findings.append(
            R.HTTP_NO_KEEPALIVE.raise_finding(
                f"{result.final_url} responds with Connection: close.",
                evidence=evidence,
                stream_layer=layer,
                variant=variant,
            )
        )

    return findings


def _slowest_phase(result: FetchResult) -> str:
    phases = {
        "connect": result.timings.connect_ms or 0.0,
        "TLS handshake": result.timings.tls_ms or 0.0,
        "time to first byte": result.timings.ttfb_ms or 0.0,
    }
    return max(phases, key=lambda k: phases[k])


def check_playlist_caching(
    result: FetchResult,
    *,
    target_duration: float | None,
    layer: StreamLayer,
    variant: str,
    thresholds: Thresholds,
) -> list[Finding]:
    """Cache-Control and Age on a live media playlist."""
    if not target_duration or target_duration <= 0:
        return []
    findings: list[Finding] = []
    evidence = {
        "url": result.final_url,
        "headers": result.cdn_fingerprint(),
        "target_duration": target_duration,
    }

    cache_control = (result.header("cache-control") or "").lower()
    max_age = _max_age(cache_control)
    limit = target_duration * thresholds.playlist_cache_max_age_factor
    if max_age is not None and max_age > limit:
        findings.append(
            R.CDN_CACHE_MAX_AGE.raise_finding(
                f"{result.final_url} is cacheable for {max_age:.0f} s against a target duration "
                f"of {target_duration:.0f} s, which allows {limit:.1f} s.",
                evidence={**evidence, "max_age": max_age},
                stream_layer=layer,
                variant=variant,
            )
        )

    age_header = result.header("age")
    if age_header and age_header.strip().isdigit():
        age = float(age_header)
        if age > target_duration:
            findings.append(
                R.CDN_AGE_HIGH.raise_finding(
                    f"{result.final_url} was served with Age: {age:.0f} s against a target "
                    f"duration of {target_duration:.0f} s"
                    + (
                        f", with X-Cache: {result.header('x-cache')}"
                        if result.header("x-cache")
                        else ""
                    )
                    + ".",
                    evidence={**evidence, "age": age},
                    stream_layer=layer,
                    variant=variant,
                )
            )
    return findings


def _max_age(cache_control: str) -> float | None:
    for part in cache_control.split(","):
        part = part.strip()
        if part.startswith("s-maxage=") or part.startswith("max-age="):
            value = part.split("=", 1)[1]
            try:
                return float(value)
            except ValueError:
                return None
    return None


def check_download_ratio(
    *,
    download_ms: float,
    declared_duration: float | None,
    url: str,
    variant: str,
    layer: StreamLayer,
    thresholds: Thresholds,
) -> Finding | None:
    """Download time divided by playback duration — the direct rebuffer predictor."""
    if not declared_duration or declared_duration <= 0:
        return None
    ratio = (download_ms / 1000.0) / declared_duration
    evidence = {
        "url": url,
        "download_ms": download_ms,
        "extinf": declared_duration,
        "ratio": ratio,
    }
    if ratio >= thresholds.download_ratio_error:
        return R.SEG_SLOW.raise_finding(
            f"{url} took {download_ms / 1000:.2f} s to download against an EXTINF of "
            f"{declared_duration:.2f} s, a ratio of {ratio:.2f}.",
            evidence=evidence,
            stream_layer=layer,
            variant=variant,
        )
    if ratio >= thresholds.download_ratio_warn:
        return R.CDN_DOWNLOAD_RATIO.raise_finding(
            f"{url} took {download_ms / 1000:.2f} s to download against an EXTINF of "
            f"{declared_duration:.2f} s, a ratio of {ratio:.2f}.",
            evidence=evidence,
            stream_layer=layer,
            variant=variant,
            severity=Severity.WARN,
        )
    return None


def check_throughput(
    *,
    measured_bps: float,
    declared_bandwidth: int | None,
    url: str,
    variant: str,
    layer: StreamLayer,
) -> Finding | None:
    if not declared_bandwidth or measured_bps <= 0:
        return None
    if measured_bps >= declared_bandwidth:
        return None
    return R.CDN_THROUGHPUT_LOW.raise_finding(
        f"{url} was delivered at {measured_bps / 1000:.0f} kbit/s while the rung declares "
        f"BANDWIDTH={declared_bandwidth // 1000} kbit/s.",
        evidence={
            "url": url,
            "measured_bps": measured_bps,
            "declared_bandwidth": declared_bandwidth,
        },
        stream_layer=layer,
        variant=variant,
    )


def check_pop_consistency(
    samples: list[tuple[FetchResult, int]],
    *,
    variant: str,
    layer: StreamLayer,
    thresholds: Thresholds,
) -> Finding | None:
    """Compare repeated fetches of the same playlist for an edge-tier split.

    SSAI packagers mint a new session identifier per request, so the URLs are normalised
    before comparison; without that step every SSAI channel reports a false split.
    """
    if len(samples) < 2:
        return None
    normalised = {normalize_session_tokens(result.final_url) for result, _ in samples}
    msns = [msn for _, msn in samples]
    spread = max(msns) - min(msns)
    if spread < thresholds.cross_variant_msn_error_spread:
        return None
    return R.CDN_POP_SPLIT.raise_finding(
        f"{len(samples)} fetches of the same playlist returned media sequence numbers "
        f"{msns}, a spread of {spread} against a tolerance of "
        f"{thresholds.cross_variant_msn_error_spread}.",
        evidence={
            "msns": msns,
            "spread": spread,
            "normalised_urls": sorted(normalised),
            "headers": [result.cdn_fingerprint() for result, _ in samples],
        },
        stream_layer=layer,
        variant=variant,
    )


def check_cdn_behind_origin(
    *, cdn_msn: int, origin_msn: int, variant: str, cdn_headers: dict[str, str]
) -> Finding | None:
    if cdn_msn >= origin_msn:
        return None
    return R.CDN_BEHIND_ORIGIN.raise_finding(
        f"The CDN serves media sequence {cdn_msn} while the origin serves {origin_msn}, "
        f"{origin_msn - cdn_msn} segment(s) behind.",
        evidence={"cdn_msn": cdn_msn, "origin_msn": origin_msn, "cdn_headers": cdn_headers},
        stream_layer=StreamLayer.CDN,
        variant=variant,
    )


def check_relative_uri_host_shift(
    *, playlist_url: str, final_url: str, child_uris: list[str], variant: str, layer: StreamLayer
) -> Finding | None:
    """A relative child that lands on a different host after the redirect."""
    if host_of(playlist_url) == host_of(final_url):
        return None
    relative = [uri for uri in child_uris if "://" not in uri]
    if not relative:
        return None
    return R.HTTP_URI_HOST_SHIFT.raise_finding(
        f"{playlist_url} redirects to {final_url} and lists {len(relative)} relative child "
        f"URI(s), so they resolve against {urlsplit(final_url).netloc} rather than "
        f"{urlsplit(playlist_url).netloc}.",
        evidence={
            "playlist_url": playlist_url,
            "final_url": final_url,
            "relative_examples": relative[:5],
        },
        stream_layer=layer,
        variant=variant,
    )
