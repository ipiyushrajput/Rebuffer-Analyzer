"""The rule catalogue.

Every check RBA can report is declared here, once, with its owner, its root-cause sentence
and its fix. Detector modules measure and then raise these declarations; they never invent
wording. `docs/RULES.md` is generated from this file.

`reference` names the equivalent check in the market tools studied in
`docs/REFERENCE_TOOLS.md`, so a reader of either document can cross-navigate.
"""

from __future__ import annotations

from app.analysis.rules.base import Owner, RebufferImpact, Rule, Severity, registry

C = Severity.CRITICAL
E = Severity.ERROR
W = Severity.WARN
I = Severity.INFO  # noqa: E741 — reads as a severity column in the declarations below.
P = Severity.PASS

DIRECT = RebufferImpact.DIRECT
INDIRECT = RebufferImpact.INDIRECT
NONE = RebufferImpact.NONE


def _r(
    rule_id: str,
    layer: str,
    severity: Severity,
    owner: Owner,
    title: str,
    root_cause: str,
    fix: str,
    impact: RebufferImpact = NONE,
    reference: str = "",
    thresholds: tuple[str, ...] = (),
) -> Rule:
    return registry.declare(
        id=rule_id,
        layer=layer,
        severity=severity,
        owner=owner,
        title=title,
        root_cause=root_cause,
        fix=fix,
        rebuffer_impact=impact,
        reference=reference,
        thresholds=thresholds,
    )


# ---------------------------------------------------------------------------
# A. Transport, DNS, TLS, HTTP, CDN
# ---------------------------------------------------------------------------

NET_AAAA_NO_IPV6 = _r(
    "NET-001",
    "dns",
    E,
    Owner.CDN,
    "AAAA record published without IPv6 transit",
    "The edge publishes an AAAA record that does not accept a TCP connection, so a TV that "
    "prefers IPv6 spends its connection timeout before falling back to IPv4.",
    "Withdraw the AAAA record for this hostname, or provide IPv6 transit to the edge that "
    "serves it.",
    DIRECT,
    "Tizen Compat Audit DNS module",
)
NET_RESOLVE_FAIL = _r(
    "NET-002",
    "dns",
    C,
    Owner.CDN,
    "Hostname does not resolve",
    "DNS returns no address for the playback hostname, so no request reaches the edge.",
    "Publish an A record for this hostname or correct the hostname in the channel configuration.",
    DIRECT,
    "Qosifire 'Failed to resolve'",
)
NET_CNAME_DEPTH = _r(
    "NET-003",
    "dns",
    W,
    Owner.CDN,
    "CNAME chain is deeper than three hops",
    "Each CNAME hop adds a resolver round trip to every cold start on the device.",
    "Flatten the CNAME chain to at most two hops.",
    INDIRECT,
    "Tizen Compat Audit DNS module",
)
NET_RESOLVE_SLOW = _r(
    "NET-004",
    "dns",
    W,
    Owner.NETWORK,
    "DNS resolution exceeds the latency budget",
    "Resolution time adds to startup on every cold playback start.",
    "Lower the record TTL pressure on the authoritative servers or move the zone to a faster "
    "provider.",
    INDIRECT,
    "Tizen Compat Audit LATENCY module",
    ("ttfb_budget_ms",),
)
NET_RESOLVED = _r(
    "NET-900",
    "dns",
    P,
    Owner.CDN,
    "Hostname resolves and answers",
    "DNS returns an address that accepts a TCP connection.",
    "No action.",
    NONE,
    "Qosifire 'Hostname resolved'",
)

TLS_CHAIN_INCOMPLETE = _r(
    "TLS-001",
    "tls",
    E,
    Owner.CDN,
    "Certificate chain is incomplete",
    "The edge sends a leaf certificate without the intermediate, so a device whose trust "
    "store lacks that intermediate rejects the handshake.",
    "Configure the edge to send the full certificate chain including every intermediate.",
    DIRECT,
    "Tizen Compat Audit TLS module",
)
TLS_EXPIRED = _r(
    "TLS-002",
    "tls",
    C,
    Owner.CDN,
    "Certificate is expired or expires within 14 days",
    "An expired certificate stops every device that verifies the chain.",
    "Renew and deploy the certificate on every edge serving this hostname.",
    DIRECT,
    "Tizen Compat Audit TLS module",
)
TLS_SAN_MISMATCH = _r(
    "TLS-003",
    "tls",
    E,
    Owner.CDN,
    "Certificate SAN list does not cover the requested hostname",
    "The hostname the player requests is absent from the certificate, so a verifying device "
    "rejects the connection.",
    "Add the hostname to the certificate SAN list.",
    DIRECT,
    "Tizen Compat Audit TLS module",
)
TLS_NO_LEGACY_CIPHER = _r(
    "TLS-004",
    "tls",
    E,
    Owner.CDN,
    "Edge offers no cipher a legacy Tizen client negotiates",
    "The edge rejects every cipher suite the 2016-2018 Tizen TLS stack offers, so those "
    "devices fail the handshake.",
    "Re-enable ECDHE-RSA-AES128-SHA on the edge for this hostname.",
    DIRECT,
    "Tizen Compat Audit TLS module",
)
TLS_VERSION_LOW = _r(
    "TLS-005",
    "tls",
    W,
    Owner.CDN,
    "Negotiated TLS version is below 1.2",
    "The connection negotiates a protocol version that is withdrawn from current device firmware.",
    "Enable TLS 1.2 and TLS 1.3 on the edge.",
    NONE,
    "Tizen Compat Audit TLS module",
)
TLS_OK = _r(
    "TLS-900",
    "tls",
    P,
    Owner.CDN,
    "TLS endpoint is valid",
    "The chain is complete, the certificate covers the hostname, and it is inside its "
    "validity window.",
    "No action.",
    NONE,
)

HTTP_REDIRECT_DEPTH = _r(
    "HTTP-001",
    "http",
    W,
    Owner.CDN,
    "Redirect chain is longer than two hops",
    "Each hop adds a full request round trip before the first byte of media arrives.",
    "Collapse the redirect chain so the playback URL reaches media in at most one hop.",
    INDIRECT,
    "Tizen Compat Audit HTTP module; Qosifire 'Redirect'",
)
HTTP_QUERY_DROPPED = _r(
    "HTTP-002",
    "http",
    C,
    Owner.CDN,
    "Redirect drops query parameters carried by the playback URL",
    "The redirect target omits parameters the origin requires, so the request that follows "
    "the redirect is rejected.",
    "Preserve the full query string across the redirect.",
    DIRECT,
    "Tizen Compat Audit URI module",
)
HTTP_HOST_CHANGE = _r(
    "HTTP-003",
    "http",
    I,
    Owner.CDN,
    "Redirect changes scheme or host",
    "The playback URL and the URL that serves media are on different hosts, so every child "
    "URI resolves against the post-redirect host.",
    "No action; the analyzer resolves against the final URL.",
    NONE,
)
HTTP_403 = _r(
    "HTTP-004",
    "http",
    C,
    Owner.CDN,
    "Request returns HTTP 403",
    "The edge refuses the request, which stops delivery for every device presenting the same "
    "token or originating in the same region.",
    "Reissue the access token for this channel or correct the geo policy on the edge.",
    DIRECT,
    "THEOplayer non-2xx download failure",
)
HTTP_404_LISTED = _r(
    "HTTP-005",
    "http",
    C,
    Owner.CDN,
    "Segment listed in the playlist returns HTTP 404",
    "The playlist advertises a segment the edge does not hold, so the player has nothing to "
    "decode for that position on the timeline.",
    "Publish the segment to the edge before it is listed, or remove it from the playlist "
    "until it is present.",
    DIRECT,
    "HLSAnalyzer EC-2002; Qosifire 'Bad chunk'",
)
HTTP_410 = _r(
    "HTTP-006",
    "http",
    E,
    Owner.CDN,
    "Request returns HTTP 410",
    "The edge reports the resource permanently removed while the playlist still lists it.",
    "Remove the resource from the playlist at the same time it is withdrawn from the edge.",
    DIRECT,
)
HTTP_429 = _r(
    "HTTP-007",
    "http",
    E,
    Owner.CDN,
    "Request returns HTTP 429",
    "The edge rate-limits the client, so segment delivery stops until the limit window clears.",
    "Raise the per-client rate limit for the TV Plus player profile on this edge.",
    DIRECT,
)
HTTP_5XX = _r(
    "HTTP-008",
    "http",
    C,
    Owner.CDN,
    "Request returns a 5xx status",
    "The edge or origin fails to produce the resource, so delivery stops for that position.",
    "Investigate the edge error log for this path and restore the origin response.",
    DIRECT,
    "HLSAnalyzer EC-2004",
)
HTTP_CONTENT_TYPE = _r(
    "HTTP-009",
    "http",
    W,
    Owner.CDN,
    "Playlist is served with a Content-Type that is not an HLS media type",
    "The response declares a type other than application/vnd.apple.mpegurl or "
    "application/x-mpegURL, which a strict client rejects before parsing.",
    "Set Content-Type: application/vnd.apple.mpegurl on playlist responses.",
    INDIRECT,
    "Tizen Compat Audit HTTP module",
)
HTTP_TRUNCATED = _r(
    "HTTP-010",
    "http",
    C,
    Owner.CDN,
    "Response body is shorter than the declared Content-Length",
    "The edge closes the connection before sending the whole segment, so the player decodes "
    "a partial segment or discards it.",
    "Investigate the edge connection reset for this path.",
    DIRECT,
)
HTTP_TIMEOUT = _r(
    "HTTP-011",
    "http",
    E,
    Owner.CDN,
    "Request does not complete within the request timeout",
    "The request exceeds the client timeout, so the player treats the resource as "
    "unavailable and retries.",
    "Reduce time to first byte on this path at the edge.",
    DIRECT,
    "Qosifire 'Request timeout' (5 s)",
    ("request_timeout_s",),
)
HTTP_TTFB_BUDGET = _r(
    "HTTP-012",
    "http",
    W,
    Owner.CDN,
    "Time to first byte exceeds the budget",
    "The edge takes longer than the budget to start the response, which consumes buffer the "
    "player needs for the next segment.",
    "Investigate edge cache misses and origin latency for this path.",
    INDIRECT,
    "Tizen Compat Audit LATENCY module",
    ("ttfb_budget_ms",),
)
HTTP_NO_KEEPALIVE = _r(
    "HTTP-013",
    "http",
    W,
    Owner.CDN,
    "Connection reuse is disabled",
    "The edge closes the connection after each response, so every segment pays a fresh TCP "
    "and TLS handshake.",
    "Enable HTTP keep-alive on the edge for this hostname.",
    INDIRECT,
    "Tizen Compat Audit HTTP module",
)
HTTP_ENCODING = _r(
    "HTTP-014",
    "http",
    W,
    Owner.CDN,
    "Segment is served with a Content-Encoding",
    "The segment body is transfer-encoded, which a legacy device decoder does not unwrap.",
    "Serve segment bodies without Content-Encoding.",
    INDIRECT,
    "Tizen Compat Audit HTTP module",
)
HTTP_URI_HOST_SHIFT = _r(
    "HTTP-015",
    "http",
    E,
    Owner.PACKAGER,
    "Relative child URI resolves to a different host after the redirect",
    "The playlist uses relative URIs and the redirect moves the base to another host, so the "
    "children are fetched from a host that does not hold them.",
    "Publish absolute child URIs, or stop redirecting the playlist across hosts.",
    DIRECT,
    "Tizen Compat Audit URI module",
)
HTTP_OK = _r(
    "HTTP-900",
    "http",
    P,
    Owner.CDN,
    "Every request returns a success status",
    "No playlist, init segment or media segment request returned a non-success status during "
    "the analysis window.",
    "No action.",
    NONE,
)

CDN_CACHE_MAX_AGE = _r(
    "CDN-001",
    "cdn",
    E,
    Owner.CDN,
    "Live media playlist carries a cache lifetime longer than half the target duration",
    "The edge holds the playlist past the point where a new segment exists, so a player "
    "refreshing on schedule receives a playlist without the segment it needs.",
    "Set max-age on live media playlists to at most half the target duration.",
    DIRECT,
    "Tizen Compat Audit CDN module",
    ("playlist_cache_max_age_factor",),
)
CDN_AGE_HIGH = _r(
    "CDN-002",
    "cdn",
    E,
    Owner.CDN,
    "Playlist response Age exceeds the target duration",
    "The edge serves a cached playlist older than one segment, so the segment list the player "
    "receives is behind the live edge.",
    "Reduce the edge cache lifetime for live media playlists on this path.",
    DIRECT,
    "HLSAnalyzer stalled-playlist detection",
    ("stale_playlist_factor",),
)
CDN_BEHIND_ORIGIN = _r(
    "CDN-003",
    "cdn",
    E,
    Owner.CDN,
    "CDN serves an older media sequence number than the origin",
    "The edge holds a stale playlist while the origin has already published newer segments, "
    "so the player runs out of media the origin is already serving.",
    "Purge the stale edge object and lower the cache lifetime for this path.",
    DIRECT,
    "Qosifire 'Wrong media sequence'",
)
CDN_POP_SPLIT = _r(
    "CDN-004",
    "cdn",
    E,
    Owner.CDN,
    "Repeated fetches return media sequence windows that differ beyond the tolerance",
    "Edge nodes behind the same hostname hold different playlist versions, so which node a "
    "device reaches decides whether it stalls.",
    "Synchronise the edge tier for this path or shorten the object lifetime so nodes converge.",
    DIRECT,
    "Tizen Compat Audit CDN module",
    ("cross_variant_msn_error_spread",),
)
CDN_NO_CORS = _r(
    "CDN-005",
    "cdn",
    W,
    Owner.CDN,
    "Access-Control-Allow-Origin is absent",
    "The response carries no CORS header, so a browser-based player cannot read it.",
    "Add Access-Control-Allow-Origin to playlist and segment responses.",
    NONE,
    "THEOplayer CORS check",
)
CDN_DOWNLOAD_RATIO = _r(
    "CDN-006",
    "cdn",
    E,
    Owner.CDN,
    "Segment download time exceeds its playback duration",
    "A segment takes longer to arrive than it takes to play, so the player buffer drains "
    "while the segment is still in flight.",
    "Raise delivery throughput for this rung at the edge, or lower the rung bitrate.",
    DIRECT,
    "HLSAnalyzer WA-1002; Qosifire 'Buffer too short'",
    ("download_ratio_warn", "download_ratio_error"),
)
CDN_THROUGHPUT_LOW = _r(
    "CDN-007",
    "cdn",
    W,
    Owner.CDN,
    "Measured delivery throughput is below the rung's declared bandwidth",
    "The edge delivers this rung more slowly than the rung's own bitrate, so sustained "
    "playback at this rung consumes buffer.",
    "Raise delivery throughput for this rung at the edge.",
    DIRECT,
    "HLSAnalyzer EC-2001",
)
CDN_WRONG_MSN = _r(
    "CDN-008",
    "cdn",
    E,
    Owner.CDN,
    "A later fetch returns an older media sequence number than an earlier fetch",
    "One edge node serves a playlist version older than the one already delivered, which "
    "moves the player backwards on the timeline.",
    "Purge the stale edge object and align the cache lifetime across the edge tier.",
    DIRECT,
    "Qosifire 'Wrong media sequence'; HLSAnalyzer EC-1003",
)

# ---------------------------------------------------------------------------
# B. Master playlist
# ---------------------------------------------------------------------------

MST_NO_EXTM3U = _r(
    "MST-001",
    "master",
    C,
    Owner.PACKAGER,
    "Playlist does not start with #EXTM3U",
    "The first line is not the required tag, so a conforming player rejects the playlist.",
    "Emit #EXTM3U as the first line of the playlist.",
    DIRECT,
    "Akamai Stream Validator (RFC 8216)",
)
MST_VERSION_LOW = _r(
    "MST-002",
    "master",
    W,
    Owner.PACKAGER,
    "EXT-X-VERSION is lower than the tags used require",
    "The declared version does not cover a tag present in the playlist, so a player honouring "
    "the declaration rejects that tag.",
    "Raise EXT-X-VERSION to the value the tags in use require.",
    NONE,
    "Akamai Stream Validator (RFC 8216)",
)
MST_NO_RESOLUTION = _r(
    "MST-003",
    "master",
    E,
    Owner.PACKAGER,
    "EXT-X-STREAM-INF carries no RESOLUTION",
    "The rung declares no resolution, so the player selects rungs without knowing the picture "
    "size it commits the decoder to.",
    "Add RESOLUTION to every EXT-X-STREAM-INF.",
    INDIRECT,
    "MT Compatibility Check; HLSAnalyzer EC-2005",
)
MST_NO_FRAMERATE = _r(
    "MST-004",
    "master",
    E,
    Owner.PACKAGER,
    "EXT-X-STREAM-INF carries no FRAME-RATE",
    "The rung declares no frame rate, so the player cannot confirm the rung matches the "
    "display mode before switching to it.",
    "Add FRAME-RATE to every EXT-X-STREAM-INF.",
    INDIRECT,
    "MT Compatibility Check",
)
MST_NO_CODECS = _r(
    "MST-005",
    "master",
    E,
    Owner.PACKAGER,
    "EXT-X-STREAM-INF carries no CODECS",
    "The rung declares no codec string, so the player commits to a rung before it knows "
    "whether the device decodes it.",
    "Add CODECS to every EXT-X-STREAM-INF.",
    INDIRECT,
    "MT Compatibility Check; THEOplayer",
)
MST_NO_BANDWIDTH = _r(
    "MST-006",
    "master",
    E,
    Owner.PACKAGER,
    "EXT-X-STREAM-INF carries no BANDWIDTH",
    "The rung declares no bitrate, so ABR selects it without a throughput comparison.",
    "Add BANDWIDTH to every EXT-X-STREAM-INF.",
    DIRECT,
    "HLSAnalyzer EC-2000",
)
MST_PEAK_OVER_BANDWIDTH = _r(
    "MST-007",
    "master",
    E,
    Owner.PACKAGER,
    "Measured peak segment bitrate exceeds the declared BANDWIDTH",
    "The rung delivers more bits than it declares, so ABR selects it on a connection that "
    "cannot sustain the real bitrate and the buffer drains.",
    "Set BANDWIDTH to the measured peak bitrate of the rung.",
    DIRECT,
    "HLSAnalyzer EC-2001; THEOplayer segment-size check",
    ("bandwidth_overshoot_tolerance",),
)
MST_MEAN_OVER_AVG_BANDWIDTH = _r(
    "MST-008",
    "master",
    W,
    Owner.PACKAGER,
    "Measured mean bitrate exceeds the declared AVERAGE-BANDWIDTH",
    "The rung's sustained bitrate is above its declaration, so bandwidth planning based on "
    "the manifest underestimates what the rung costs.",
    "Set AVERAGE-BANDWIDTH to the measured mean bitrate of the rung.",
    INDIRECT,
    "HLSAnalyzer EC-2001",
    ("bandwidth_overshoot_tolerance",),
)
MST_CODECS_MISMATCH = _r(
    "MST-009",
    "master",
    E,
    Owner.PACKAGER,
    "Declared CODECS does not match the elementary stream",
    "The manifest declares a codec string the bitstream does not match, so the device selects "
    "a decoder that cannot decode the rung.",
    "Set CODECS to the profile, level and audio object type the encoder actually produces.",
    DIRECT,
    "Dolby Stream Validator cross-level conformance; Tizen Compat Audit CODEC module",
)
MST_FIRST_RUNG_HIGH = _r(
    "MST-010",
    "master",
    E,
    Owner.PACKAGER,
    "First listed rung is not the lowest bitrate rung",
    "Tizen starts playback on the first EXT-X-STREAM-INF in the master playlist, so a high "
    "first rung forces startup at a bitrate the connection has not been measured against.",
    "List the lowest bitrate rung first in the master playlist.",
    DIRECT,
    "Known Tizen startup behaviour",
)
MST_LOWEST_RUNG_HIGH = _r(
    "MST-011",
    "master",
    W,
    Owner.CONTENT_PROVIDER,
    "Lowest rung bitrate is above the recovery floor",
    "The ladder has no rung low enough to recover on a constrained connection, so a "
    "downswitch does not stop the buffer draining.",
    "Add a rung at or below the configured recovery floor.",
    DIRECT,
    "Tizen Compat Audit MASTER module",
    ("lowest_rung_max_kbps",),
)
MST_RUNG_STEP = _r(
    "MST-012",
    "master",
    W,
    Owner.CONTENT_PROVIDER,
    "Bitrate step between adjacent rungs is larger than the configured ratio",
    "The gap between neighbouring rungs forces ABR to jump more bitrate than the measured "
    "throughput change justifies.",
    "Insert an intermediate rung so adjacent rungs stay within the configured ratio.",
    INDIRECT,
    "Tizen Compat Audit MASTER module",
    ("max_adjacent_rung_ratio",),
)
MST_DUPLICATE_RUNG = _r(
    "MST-013",
    "master",
    W,
    Owner.PACKAGER,
    "Two rungs declare the same bandwidth and resolution",
    "Duplicate rungs give ABR two identical choices and waste a switch opportunity.",
    "Remove the duplicate EXT-X-STREAM-INF entry.",
    NONE,
)
MST_RUNG_UNREACHABLE = _r(
    "MST-014",
    "master",
    C,
    Owner.PACKAGER,
    "Rung listed in the master playlist does not return a media playlist",
    "The master advertises a rung whose media playlist is unavailable, so ABR switching to it "
    "stops playback.",
    "Remove the rung from the master playlist or publish its media playlist.",
    DIRECT,
    "HLSAnalyzer EC-2004",
)
MST_MIXED_MUX = _r(
    "MST-015",
    "master",
    E,
    Owner.PACKAGER,
    "Master playlist mixes muxed and demuxed variants",
    "Some rungs carry audio inside the video segments and others reference a separate audio "
    "rendition, so an ABR switch between them changes the track layout mid-playback.",
    "Publish the whole ladder either muxed or demuxed.",
    DIRECT,
    "HLS AV Doctor ladder audit",
)
MST_AUDIO_GROUP_MISSING = _r(
    "MST-016",
    "master",
    C,
    Owner.PACKAGER,
    "Variant references an AUDIO group that the master playlist does not define",
    "The rung names an audio group with no matching EXT-X-MEDIA entry, so the player has no "
    "audio to pair with that rung.",
    "Add the EXT-X-MEDIA entry for the referenced group, or correct the AUDIO attribute.",
    DIRECT,
    "Tizen Compat Audit MASTER module",
)
MST_AUDIO_GROUP_INCONSISTENT = _r(
    "MST-017",
    "master",
    E,
    Owner.PACKAGER,
    "Audio renditions in one group declare different codecs or channel counts",
    "Renditions in a group are interchangeable during playback, so a differing codec or "
    "channel count reconfigures the audio decoder on every switch.",
    "Encode every rendition in a group with the same codec, sample rate and channel count.",
    DIRECT,
    "Dolby Stream Validator cross-level conformance",
)
MST_DRM = _r(
    "MST-018",
    "master",
    I,
    Owner.CONTENT_PROVIDER,
    "Stream is encrypted",
    "The playlist declares encryption, so bitstream checks run only on segments the supplied "
    "keys decrypt.",
    "Supply clear KID and KEY pairs in the job options to analyse the bitstream.",
    NONE,
    "Dolby Stream Validator clear-key input",
)
MST_NO_IFRAME = _r(
    "MST-019",
    "master",
    I,
    Owner.PACKAGER,
    "Master playlist declares no I-frame playlist",
    "The ladder carries no EXT-X-I-FRAME-STREAM-INF, so trick play has no dedicated track.",
    "Publish an I-frame playlist for trick-play support.",
    NONE,
)
MST_CHANGED = _r(
    "MST-020",
    "master",
    E,
    Owner.PACKAGER,
    "Master playlist changed during playback",
    "A variant was added, removed, or had its attributes rewritten while the session was "
    "running, so the ladder the player loaded no longer matches the ladder being served.",
    "Keep the master playlist stable for the life of the channel.",
    DIRECT,
    "HLSAnalyzer master re-poll (20 s)",
    ("master_repoll_interval_s",),
)
MST_MEDIA_BECAME_MASTER = _r(
    "MST-021",
    "master",
    C,
    Owner.PACKAGER,
    "A media playlist URI now returns a master playlist",
    "A URI the player holds as a media playlist returns EXT-X-STREAM-INF entries, so the "
    "player parses a ladder where it expects segments.",
    "Restore the media playlist at this URI.",
    DIRECT,
    "HLSAnalyzer EC-1000",
)
MST_DOLBY_MISMATCH = _r(
    "MST-022",
    "master",
    E,
    Owner.PACKAGER,
    "Declared Dolby codec does not match the audio elementary stream",
    "The manifest declares ec-3, ac-3 or ac-4 and the elementary stream carries a different "
    "codec, so the device selects the wrong audio decoder.",
    "Correct the CODECS attribute to the codec the encoder produces.",
    DIRECT,
    "Dolby Stream Validator cross-level conformance",
)
MST_CHANNELS_MISMATCH = _r(
    "MST-023",
    "master",
    E,
    Owner.PACKAGER,
    "CHANNELS attribute does not match the audio channel count",
    "The rendition declares a channel count the elementary stream does not carry, so "
    "downmixing on the device is configured from a wrong value.",
    "Set CHANNELS to the channel count the elementary stream carries.",
    INDIRECT,
    "Dolby Stream Validator cross-level conformance",
)
MST_VIDEO_RANGE_MISMATCH = _r(
    "MST-024",
    "master",
    E,
    Owner.PACKAGER,
    "VIDEO-RANGE does not match the transfer characteristics in the bitstream",
    "The manifest declares a dynamic range the bitstream does not carry, so the display is "
    "driven in the wrong mode.",
    "Set VIDEO-RANGE to SDR, PQ or HLG according to the transfer characteristics encoded.",
    NONE,
    "Dolby Stream Validator cross-level conformance",
)
MST_CROSS_LEVEL = _r(
    "MST-025",
    "master",
    E,
    Owner.PACKAGER,
    "Manifest, container and elementary stream disagree for a rendition",
    "The three levels declare different values for the same property, so a player that trusts "
    "the manifest configures the decoder for media it does not receive.",
    "Align the manifest attributes, container metadata and elementary stream values.",
    DIRECT,
    "Dolby Stream Validator cross-level conformance",
)
MST_FRAMERATE_UNSUPPORTED = _r(
    "MST-026",
    "master",
    W,
    Owner.CONTENT_PROVIDER,
    "Rung declares a frame rate other than 29.97 or 30",
    "The rung runs at a frame rate outside the profile this fleet is validated against.",
    "Re-encode the rung at 29.97 or 30 frames per second.",
    INDIRECT,
    "MT Compatibility Check",
)
MST_HEVC = _r(
    "MST-027",
    "master",
    W,
    Owner.CONTENT_PROVIDER,
    "Rung is encoded in HEVC",
    "The rung uses HEVC, which part of the deployed Tizen fleet decodes only in hardware "
    "profiles narrower than H.264.",
    "Publish an H.264 rung alongside the HEVC rung.",
    INDIRECT,
    "MT Compatibility Check",
)
MST_NO_INDEPENDENT_SEGMENTS = _r(
    "MST-028",
    "master",
    W,
    Owner.PACKAGER,
    "EXT-X-INDEPENDENT-SEGMENTS is absent",
    "The playlist does not declare that each segment starts independently, so a player cannot "
    "assume a switch point at a segment boundary.",
    "Add EXT-X-INDEPENDENT-SEGMENTS to the master playlist.",
    NONE,
    "Akamai Stream Validator (Apple HLS Authoring Specification)",
)
MST_OK = _r(
    "MST-900",
    "master",
    P,
    Owner.PACKAGER,
    "Master playlist is conformant",
    "Every EXT-X-STREAM-INF carries BANDWIDTH, RESOLUTION, FRAME-RATE and CODECS, and every "
    "referenced rendition group is defined.",
    "No action.",
    NONE,
)

# ---------------------------------------------------------------------------
# C. Media playlists and sequence numbers
# ---------------------------------------------------------------------------

MED_TARGETDURATION = _r(
    "MED-001",
    "media_playlist",
    E,
    Owner.PACKAGER,
    "A segment duration rounds above EXT-X-TARGETDURATION",
    "The playlist declares a target duration smaller than a segment it lists, so a player "
    "sizing its buffer from the declaration under-buffers.",
    "Raise EXT-X-TARGETDURATION to the rounded maximum segment duration.",
    DIRECT,
    "Akamai Stream Validator (RFC 8216); HLSAnalyzer EC-1008",
)
MED_EXTINF_VARIANCE = _r(
    "MED-002",
    "media_playlist",
    W,
    Owner.PACKAGER,
    "Segment durations vary beyond the configured tolerance",
    "Segment length varies inside the rung, so the buffer the player holds in seconds changes "
    "between segments.",
    "Produce segments of a constant duration.",
    INDIRECT,
    "Apple HLS Authoring Specification",
    ("extinf_vs_actual_tolerance_s",),
)
MED_WINDOW_SHORT = _r(
    "MED-003",
    "media_playlist",
    E,
    Owner.PACKAGER,
    "Live window is shorter than three target durations",
    "The playlist holds less media than a player needs to start and hold a buffer, so a "
    "device that falls one segment behind cannot recover.",
    "Publish at least three target durations of media in the live window.",
    DIRECT,
    "THEOplayer live-window check; Apple HLS Authoring Specification",
    ("min_live_window_multiple",),
)
MED_STALE = _r(
    "MED-004",
    "media_playlist",
    E,
    Owner.PACKAGER,
    "Live playlist is stale",
    "No new segment appeared within the stale window, so the player exhausts its buffer with "
    "no media left to fetch.",
    "Restore segment publication on this rung.",
    DIRECT,
    "HLSAnalyzer 'Stalled' state; Qosifire 'Buffer too short'",
    ("stale_playlist_factor",),
)
MED_EXTINF_OVER_TD = _r(
    "MED-005",
    "media_playlist",
    E,
    Owner.PACKAGER,
    "Segment duration exceeds 150 % of the target duration",
    "A segment is half again as long as the declared target, so a player refreshing on the "
    "target schedule misses the window in which it must fetch.",
    "Produce segments within the declared target duration.",
    DIRECT,
    "HLSAnalyzer EC-1006",
    ("segment_extinf_max_ratio_to_td",),
)
MED_EXTINF_ABSOLUTE = _r(
    "MED-006",
    "media_playlist",
    E,
    Owner.PACKAGER,
    "Segment duration exceeds the absolute maximum",
    "A single segment spans more than the configured absolute maximum, so the player commits "
    "to one fetch covering that whole span.",
    "Split the segment so no segment exceeds the configured maximum.",
    DIRECT,
    "HLSAnalyzer EC-1007",
    ("segment_extinf_absolute_max_s",),
)
MED_EXTINF_INVALID = _r(
    "MED-007",
    "media_playlist",
    C,
    Owner.PACKAGER,
    "Segment declares a duration of zero or less, or a duration that does not parse",
    "The EXTINF value is not a positive number, so the player cannot place the segment on the "
    "timeline.",
    "Emit a positive decimal duration in every EXTINF tag.",
    DIRECT,
    "HLSAnalyzer EC-1005",
)
MED_UNEXPECTED_ENDLIST = _r(
    "MED-008",
    "media_playlist",
    C,
    Owner.PACKAGER,
    "EXT-X-ENDLIST appeared on a live playlist",
    "The playlist declares the stream finished while the channel is linear, so the player "
    "stops at the end of the current window.",
    "Remove EXT-X-ENDLIST from the live media playlist.",
    DIRECT,
    "HLSAnalyzer 'LiveEnd' state",
)
MED_MAP_MISSING = _r(
    "MED-009",
    "media_playlist",
    C,
    Owner.PACKAGER,
    "fMP4 playlist carries no EXT-X-MAP",
    "The segments are fMP4 and no initialisation segment is declared, so the player has no "
    "track configuration to decode them with.",
    "Add EXT-X-MAP referencing the initialisation segment.",
    DIRECT,
    "Qosifire 'Bad init segment'",
)
MED_KEY_CHANGED = _r(
    "MED-010",
    "media_playlist",
    I,
    Owner.CONTENT_PROVIDER,
    "EXT-X-KEY changed during the analysis window",
    "The encryption key rotated inside the window, so decryption uses more than one key.",
    "No action; the change is recorded for correlation.",
    NONE,
)
MED_BYTERANGE_INVALID = _r(
    "MED-011",
    "media_playlist",
    E,
    Owner.PACKAGER,
    "EXT-X-BYTERANGE does not parse or overruns the resource",
    "The declared range is not usable, so the player requests bytes the resource does not hold.",
    "Emit byte ranges that fall inside the referenced resource.",
    DIRECT,
)
MED_GAP = _r(
    "MED-012",
    "media_playlist",
    E,
    Owner.PACKAGER,
    "Playlist declares EXT-X-GAP",
    "The packager marks a position on the timeline as having no media, so playback has "
    "nothing to decode for that duration.",
    "Restore media for the gapped position.",
    DIRECT,
    "Qosifire 'Gap in chunklist'",
)
MED_SUBTITLE_STUCK = _r(
    "MED-013",
    "media_playlist",
    E,
    Owner.PACKAGER,
    "Subtitle playlist advertises a new media sequence number with an unchanged segment list",
    "The media sequence number advances while the listed segments stay identical, so the "
    "subtitle track repeats the same cues against a moving timeline.",
    "Publish new subtitle segments as the media sequence number advances.",
    INDIRECT,
    "A-Sequence Detector web.vtt escalation",
)
MED_STATE_TRANSITION = _r(
    "MED-014",
    "media_playlist",
    E,
    Owner.PACKAGER,
    "Media playlist left the LIVE state",
    "The playlist state machine moved out of LIVE, so the rung stopped delivering new media.",
    "Restore the rung to continuous live publication.",
    DIRECT,
    "HLSAnalyzer media playlist state machine",
)
MED_BUFFER_TOO_LONG = _r(
    "MED-015",
    "media_playlist",
    W,
    Owner.PACKAGER,
    "Segments are published faster than real time",
    "The playlist adds more media per wall-clock second than one second of content, so the "
    "player drifts from the live edge and skips forward to catch up.",
    "Publish segments at the rate they are produced.",
    INDIRECT,
    "HLSAnalyzer EC-1004; Qosifire 'Buffer too long'",
    ("vpb_max_buffer_s",),
)
MED_URI_CHANGED = _r(
    "MED-016",
    "media_playlist",
    E,
    Owner.CDN,
    "Segment URI changed for an unchanged media sequence number",
    "The same position on the timeline is served under a different URI between polls, so a "
    "player that queued the earlier URI fetches a resource that no longer exists.",
    "Keep segment URIs stable for the life of a media sequence number.",
    DIRECT,
    "Qosifire 'Chunklist params changed'",
)
MED_PDT_MISSING = _r(
    "MED-017",
    "media_playlist",
    W,
    Owner.PACKAGER,
    "Playlist carries no EXT-X-PROGRAM-DATE-TIME",
    "The rung publishes no wall-clock anchor, so audio and video renditions cannot be aligned "
    "by time.",
    "Emit EXT-X-PROGRAM-DATE-TIME at the start of every playlist window.",
    INDIRECT,
    "HLS AV Doctor DemuxAnalyzer",
)
MED_PDT_NON_MONOTONIC = _r(
    "MED-018",
    "media_playlist",
    E,
    Owner.PACKAGER,
    "EXT-X-PROGRAM-DATE-TIME moves backwards",
    "The wall-clock anchor decreases across the window, so time-based alignment between "
    "renditions resolves to the wrong segment.",
    "Emit monotonically increasing program date times.",
    DIRECT,
)
MED_PDT_DRIFT = _r(
    "MED-019",
    "media_playlist",
    W,
    Owner.PACKAGER,
    "Program date time drifts from the accumulated segment durations",
    "The wall-clock anchor and the sum of EXTINF values diverge, so the two timelines the "
    "player holds disagree.",
    "Derive program date time from the media timeline.",
    INDIRECT,
)
MED_BECAME_MASTER = _r(
    "MED-020",
    "media_playlist",
    C,
    Owner.PACKAGER,
    "Media playlist became a master playlist",
    "The URI now returns EXT-X-STREAM-INF entries where segments were listed, so the player "
    "has no segments to fetch.",
    "Restore the media playlist at this URI.",
    DIRECT,
    "HLSAnalyzer EC-1000",
)
MED_DOWNLOAD_FAIL = _r(
    "MED-021",
    "media_playlist",
    C,
    Owner.CDN,
    "Media playlist download failed",
    "The playlist request did not return a body, so the player has no segment list to fetch from.",
    "Restore playlist delivery for this rung at the edge.",
    DIRECT,
    "HLSAnalyzer EC-2004; Qosifire 'Bad chunklist'",
)
MED_OK = _r(
    "MED-900",
    "media_playlist",
    P,
    Owner.PACKAGER,
    "Media playlist advanced continuously",
    "Every poll returned a playlist in the LIVE state whose media sequence number advanced by "
    "the number of segments that rolled off.",
    "No action.",
    NONE,
)

SEQ_MSN_INCREMENT = _r(
    "SEQ-001",
    "sequence",
    E,
    Owner.PACKAGER,
    "Media sequence number advanced by a different amount than the segments that rolled off",
    "The declared sequence number and the segment list disagree about how far the window "
    "moved, so the player maps segments to the wrong positions.",
    "Advance EXT-X-MEDIA-SEQUENCE by exactly the number of segments removed from the window.",
    DIRECT,
    "A-Sequence Detector; HLSAnalyzer WA-1003",
)
SEQ_MSN_BACKWARDS = _r(
    "SEQ-002",
    "sequence",
    C,
    Owner.CDN,
    "Media sequence number decreased",
    "A later playlist declares a lower sequence number than an earlier one, so the player is "
    "moved backwards on the timeline.",
    "Serve monotonically increasing media sequence numbers from every edge node.",
    DIRECT,
    "HLSAnalyzer EC-1003",
)
SEQ_MSN_JUMP = _r(
    "SEQ-003",
    "sequence",
    E,
    Owner.PACKAGER,
    "Media sequence number jumped by more than the window length",
    "The window advanced past segments that were never published, so the media for those "
    "positions is absent from the timeline.",
    "Publish every segment in sequence or hold the sequence number until media exists.",
    DIRECT,
    "HLSAnalyzer EC-1001",
)
SEQ_MSN_LIST_IDENTICAL = _r(
    "SEQ-004",
    "sequence",
    E,
    Owner.PACKAGER,
    "Media sequence number advanced with an unchanged segment list",
    "The sequence number moved while the listed segments stayed identical, so the rung "
    "advertises progress it has not made.",
    "Advance the sequence number only when the segment list changes.",
    DIRECT,
    "A-Sequence Detector",
)
SEQ_MSN_WRAP = _r(
    "SEQ-005",
    "sequence",
    C,
    Owner.PACKAGER,
    "Media sequence number reset to a low value without EXT-X-ENDLIST",
    "The counter restarted mid-stream, so the player reads a position it has already played.",
    "Keep the media sequence number monotonic for the life of the channel.",
    DIRECT,
    "HLSAnalyzer EC-1002",
)
SEQ_DSN_BACKWARDS = _r(
    "SEQ-006",
    "sequence",
    C,
    Owner.PACKAGER,
    "Discontinuity sequence number decreased",
    "The discontinuity counter moved backwards, so the player's timeline mapping for this "
    "rung no longer matches the packager's.",
    "Keep EXT-X-DISCONTINUITY-SEQUENCE monotonic.",
    DIRECT,
    "A-Sequence Detector",
)
SEQ_DSN_JUMP = _r(
    "SEQ-007",
    "sequence",
    E,
    Owner.PACKAGER,
    "Discontinuity sequence number advanced by more than one between polls",
    "More than one discontinuity was consumed between two polls, so a discontinuity boundary "
    "passed without the player observing it.",
    "Advance EXT-X-DISCONTINUITY-SEQUENCE one step at a time.",
    DIRECT,
    "A-Sequence Detector",
)
SEQ_XVAR_MSN_SPREAD = _r(
    "SEQ-008",
    "sequence",
    E,
    Owner.PACKAGER,
    "Media sequence numbers across variants differ beyond the tolerated spread",
    "The rungs are published at different points on the timeline, so an ABR switch lands the "
    "player at a position the new rung has not reached.",
    "Publish every rung of the ladder at the same live edge.",
    DIRECT,
    "A-Sequence Detector; Tizen Compat Audit XVAR module",
    ("cross_variant_msn_error_spread",),
)
SEQ_XVAR_DSN_MISMATCH = _r(
    "SEQ-009",
    "sequence",
    C,
    Owner.PACKAGER,
    "Discontinuity sequence numbers differ across variants",
    "The Tizen player holds one discontinuity counter for the whole ladder, so rungs "
    "disagreeing on that counter freeze playback at the next switch.",
    "Emit the same discontinuity count on every rung at the same timeline position.",
    DIRECT,
    "Tizen Compat Audit DISC module; past TV Plus investigation",
)
SEQ_XVAR_SEGMENT_MISMATCH = _r(
    "SEQ-010",
    "sequence",
    E,
    Owner.PACKAGER,
    "Variants list different segment counts or durations for the same media sequence number",
    "The same sequence number covers a different span of the timeline on different rungs, so "
    "an ABR switch changes the playback position.",
    "Segment every rung on the same boundaries.",
    DIRECT,
    "Tizen Compat Audit XVAR module",
)
SEQ_XVAR_DISC_POSITION = _r(
    "SEQ-011",
    "sequence",
    C,
    Owner.PACKAGER,
    "Discontinuity tags sit at different positions across variants",
    "A discontinuity falls at a different timeline position on each rung, so a switch across "
    "the boundary reconfigures the decoder at a point the player did not prepare for.",
    "Place discontinuity tags at identical timeline positions on every rung.",
    DIRECT,
    "Tizen Compat Audit DISC module",
)
SEQ_XVAR_PDT = _r(
    "SEQ-012",
    "sequence",
    W,
    Owner.PACKAGER,
    "Program date times differ across variants for the same position",
    "The rungs anchor the same media to different wall-clock times, so time-based alignment "
    "between video and audio resolves to different segments.",
    "Emit the same program date time on every rendition for the same media.",
    INDIRECT,
    "HLS AV Doctor DemuxAnalyzer",
)

# ---------------------------------------------------------------------------
# D. Segments — container and timing
# ---------------------------------------------------------------------------

SEG_CONTAINER_UNKNOWN = _r(
    "SEG-001",
    "segment",
    E,
    Owner.PACKAGER,
    "Segment container is not recognised",
    "The first bytes match no known container, so the player has nothing it can demux.",
    "Publish segments in MPEG-2 TS, fMP4 or a packed audio format.",
    DIRECT,
    "Qosifire 'Bad chunk'",
)
SEG_SYNC = _r(
    "SEG-002",
    "segment",
    C,
    Owner.PACKAGER,
    "Transport stream sync byte is absent at the start of the segment",
    "The segment does not begin on a transport packet boundary, so the demuxer cannot lock "
    "onto the stream.",
    "Emit segments aligned to 188-byte transport packets starting with 0x47.",
    DIRECT,
    "HLSAnalyzer transport stream inspection",
)
SEG_CONTINUITY = _r(
    "SEG-003",
    "segment",
    E,
    Owner.PACKAGER,
    "Transport stream continuity counter skipped",
    "A transport packet is missing from the segment, so the decoder loses part of an access unit.",
    "Investigate packet loss in the packaging or contribution path for this rung.",
    DIRECT,
    "HLSAnalyzer PAT/PMT/PID inspection",
)
SEG_NO_PAT_PMT = _r(
    "SEG-004",
    "segment",
    C,
    Owner.PACKAGER,
    "Segment carries no PAT or no PMT",
    "The segment does not describe its own program, so a player joining at this segment "
    "cannot find the elementary streams.",
    "Emit PAT and PMT at the start of every transport stream segment.",
    DIRECT,
    "THEOplayer PAT/PMT check; HLSAnalyzer",
)
SEG_TINY = _r(
    "SEG-005",
    "segment",
    W,
    Owner.PACKAGER,
    "Segment is smaller than the tiny-segment threshold",
    "The segment holds too few bytes to carry the media its EXTINF declares, so the player "
    "reaches the end of the segment before the end of its declared duration.",
    "Publish complete segments for the declared duration.",
    DIRECT,
    "Segment size analyzer",
    ("tiny_segment_bytes",),
)
SEG_ZERO = _r(
    "SEG-006",
    "segment",
    C,
    Owner.CDN,
    "Segment body is empty",
    "The edge returns a zero-byte body for a listed segment, so there is nothing to decode.",
    "Restore the segment on the edge.",
    DIRECT,
    "Qosifire 'Bad chunk'",
)
SEG_DURATION_MISMATCH = _r(
    "SEG-007",
    "segment",
    E,
    Owner.PACKAGER,
    "Measured media duration differs from the declared EXTINF",
    "The media inside the segment spans a different length than the playlist declares, so the "
    "player's buffer accounting diverges from real playback time.",
    "Declare EXTINF equal to the media duration of the segment.",
    DIRECT,
    "HLSAnalyzer EC-2003",
    ("extinf_vs_actual_tolerance_s",),
)
SEG_PTS_GAP = _r(
    "SEG-008",
    "segment",
    E,
    Owner.PACKAGER,
    "Presentation timestamps leave a gap between consecutive segments without a discontinuity",
    "The timeline jumps forward between segments and the playlist declares no discontinuity, "
    "so the player holds a buffer position for media that does not exist.",
    "Emit continuous timestamps, or mark the boundary with EXT-X-DISCONTINUITY.",
    DIRECT,
    "HLS AV Doctor",
    ("pts_gap_tolerance_ms",),
)
SEG_PTS_OVERLAP = _r(
    "SEG-009",
    "segment",
    E,
    Owner.PACKAGER,
    "Presentation timestamps overlap between consecutive segments",
    "Two consecutive segments cover the same span of the timeline, so the player decodes the "
    "same media twice.",
    "Emit consecutive, non-overlapping timestamps.",
    DIRECT,
    "HLS AV Doctor",
    ("pts_gap_tolerance_ms",),
)
SEG_PTS_RESET = _r(
    "SEG-010",
    "segment",
    C,
    Owner.PACKAGER,
    "Presentation timestamps reset without a discontinuity tag",
    "The timestamp base changed with no discontinuity declared, so the player maps the new "
    "media onto a position it has already played.",
    "Mark every timestamp base change with EXT-X-DISCONTINUITY.",
    DIRECT,
    "HLS AV Doctor AV_PTS_ROLLOVER_SPLIT handling",
)
SEG_TFDT_DISCONTINUOUS = _r(
    "SEG-011",
    "segment",
    E,
    Owner.PACKAGER,
    "fMP4 base media decode time is not continuous with the previous segment",
    "The tfdt of this segment does not follow the previous segment's samples, so the CMAF "
    "timeline has a hole.",
    "Emit continuous base media decode times across segments.",
    DIRECT,
    "Dolby Stream Validator container-level conformance",
    ("pts_gap_tolerance_ms",),
)
SEG_TIMESCALE_MISMATCH = _r(
    "SEG-012",
    "segment",
    C,
    Owner.PACKAGER,
    "Media segment timescale does not match the initialisation segment",
    "The segment declares a different timescale than the track it belongs to, so every "
    "timestamp in it resolves to the wrong wall-clock position.",
    "Emit media segments with the timescale declared in the initialisation segment.",
    DIRECT,
    "Dolby Stream Validator container-level conformance",
)
SEG_BAD_INIT = _r(
    "SEG-013",
    "segment",
    C,
    Owner.PACKAGER,
    "Initialisation segment is unreachable or does not parse",
    "The EXT-X-MAP target does not return a usable track configuration, so no media segment "
    "in the rung can be decoded.",
    "Publish a parseable initialisation segment at the EXT-X-MAP URI.",
    DIRECT,
    "Qosifire 'Bad init segment'",
)
SEG_NO_ID3 = _r(
    "SEG-014",
    "segment",
    E,
    Owner.PACKAGER,
    "Packed audio segment carries no transport stream timestamp",
    "The segment holds raw audio with no ID3 PRIV timestamp, so the player cannot place it on "
    "the playlist timeline.",
    "Prefix every packed audio segment with the "
    "com.apple.streaming.transportStreamTimestamp ID3 PRIV frame.",
    DIRECT,
    "THEOplayer packed-audio ID3 check",
)
SEG_BITRATE_OVER_DECLARED = _r(
    "SEG-015",
    "segment",
    E,
    Owner.PACKAGER,
    "Segment bitrate exceeds the rung's declared BANDWIDTH",
    "This single segment carries more bits per second than the rung declares, so ABR "
    "underestimates what the next fetch costs.",
    "Cap the encoder so no segment exceeds the declared BANDWIDTH.",
    DIRECT,
    "THEOplayer segment-size check; HLSAnalyzer EC-2001",
    ("bandwidth_overshoot_tolerance",),
)
SEG_DECRYPT_FAIL = _r(
    "SEG-016",
    "segment",
    C,
    Owner.CONTENT_PROVIDER,
    "Segment decryption failed",
    "The declared key does not decrypt the segment, so the player produces no output for it.",
    "Publish the key that matches the segments, or correct the EXT-X-KEY URI.",
    DIRECT,
    "THEOplayer decryption-failure check; Dolby clear-key input",
)
SEG_SLOW = _r(
    "SEG-017",
    "segment",
    E,
    Owner.CDN,
    "Segment download time exceeds its playback duration",
    "The segment arrives more slowly than it plays, so the buffer loses time on every fetch "
    "of this rung.",
    "Raise delivery throughput at the edge for this rung.",
    DIRECT,
    "HLSAnalyzer WA-1002",
    ("download_ratio_error",),
)
SEG_DOWNLOAD_FAIL = _r(
    "SEG-018",
    "segment",
    C,
    Owner.CDN,
    "Segment download failed",
    "The segment request produced no usable body, so the player has nothing for that position "
    "on the timeline.",
    "Restore delivery of this segment at the edge.",
    DIRECT,
    "HLSAnalyzer EC-2002",
)
SEG_OK = _r(
    "SEG-900",
    "segment",
    P,
    Owner.PACKAGER,
    "Sampled segments are well formed",
    "Every sampled segment parsed, carried the media its EXTINF declares, and continued the "
    "timestamp of the segment before it.",
    "No action.",
    NONE,
)

# ---------------------------------------------------------------------------
# E. Video bitstream
# ---------------------------------------------------------------------------

VID_NO_KEYFRAME = _r(
    "VID-001",
    "video",
    C,
    Owner.PACKAGER,
    "Segment does not start with a random access point",
    "The segment opens on a non-IDR slice, so a player joining or switching at this boundary "
    "has no picture to start decoding from.",
    "Start every segment with an IDR picture for H.264 or an IRAP picture for HEVC.",
    DIRECT,
    "Dolby Stream Validator elementary-stream conformance",
)
VID_NO_SPS = _r(
    "VID-002",
    "video",
    C,
    Owner.PACKAGER,
    "Segment carries no sequence or picture parameter set",
    "The parameter sets are absent from the segment and from the initialisation segment, so "
    "the decoder cannot be configured to decode it.",
    "Emit SPS and PPS in every segment, or carry them in the initialisation segment.",
    DIRECT,
    "HLS AV Doctor",
)
VID_NO_VPS = _r(
    "VID-003",
    "video",
    C,
    Owner.PACKAGER,
    "HEVC segment carries no video parameter set",
    "The VPS is absent, so an HEVC decoder cannot be configured for the segment.",
    "Emit VPS, SPS and PPS in every HEVC segment.",
    DIRECT,
    "HLS AV Doctor",
)
VID_SPS_CHANGED = _r(
    "VID-004",
    "video",
    C,
    Owner.PACKAGER,
    "Sequence parameter set changed within a rung",
    "The coded picture configuration changed mid-rung, so the decoder reconfigures without an "
    "ABR switch to prepare it.",
    "Keep the encoder configuration constant for the life of a rung.",
    DIRECT,
    "Dolby Stream Validator elementary-stream conformance",
)
VID_CONFIG_AT_DISC = _r(
    "VID-005",
    "video",
    E,
    Owner.SSAI,
    "Resolution, profile or level changed at a discontinuity",
    "The media after the discontinuity is coded differently from the media before it, so the "
    "decoder reallocates at the splice.",
    "Encode spliced content with the same resolution, profile and level as the channel.",
    DIRECT,
    "Past TV Plus investigation",
)
VID_DPB_MISMATCH = _r(
    "VID-006",
    "video",
    C,
    Owner.CONTENT_PROVIDER,
    "Rungs declare different reference frame counts",
    "The ladder's rungs commit the decoder to different decoded picture buffer sizes, so every "
    "ABR switch forces a buffer reallocation; the 2016 Tizen MFC decoder returns NOT_SUPPORT "
    "after such a reconfiguration.",
    "Encode every rung with the same max_num_ref_frames.",
    DIRECT,
    "Past TV Plus investigation (x264 Level 4.0 DPB auto-scaling)",
)
VID_KEYFRAME_MISALIGNED = _r(
    "VID-007",
    "video",
    E,
    Owner.CONTENT_PROVIDER,
    "Keyframes are not aligned across rungs at the same media sequence number",
    "The rungs place their random access points at different timeline positions, so an ABR "
    "switch cannot resume decoding at the switch point.",
    "Align the GOP structure across every rung of the ladder.",
    DIRECT,
    "Dolby Stream Validator A/V alignment",
)
VID_FRAMERATE_MISMATCH = _r(
    "VID-008",
    "video",
    E,
    Owner.PACKAGER,
    "Frame rate in the bitstream differs from the declared FRAME-RATE",
    "The manifest declares a frame rate the encoder did not produce, so the player drives the "
    "display at the wrong cadence.",
    "Set FRAME-RATE to the value in the sequence parameter set VUI timing.",
    INDIRECT,
    "Tizen Compat Audit CODEC module",
)
VID_DECODE_ERROR = _r(
    "VID-009",
    "video",
    C,
    Owner.PACKAGER,
    "Decoder reports errors on the segment",
    "Decoding the segment produces errors, so the picture breaks up or the decoder stops.",
    "Re-encode the affected segments.",
    DIRECT,
    "Akamai reference-player check",
)
VID_BLACK = _r(
    "VID-010",
    "video",
    W,
    Owner.CONTENT_PROVIDER,
    "Segment contains black frames",
    "The picture is black for a measurable span, so the channel shows no content for that "
    "duration.",
    "Restore programme content on the contribution feed.",
    NONE,
    "HLSAnalyzer video quality detectors",
)
VID_FROZEN = _r(
    "VID-011",
    "video",
    E,
    Owner.CONTENT_PROVIDER,
    "Segment contains frozen frames",
    "The picture does not change for a measurable span, so the channel shows a still image "
    "where motion is expected.",
    "Restore the contribution feed for this channel.",
    NONE,
    "HLSAnalyzer video quality detectors",
)
VID_INTERLACED = _r(
    "VID-012",
    "video",
    W,
    Owner.CONTENT_PROVIDER,
    "Rung is interlaced",
    "The rung codes fields rather than frames, so the device deinterlaces before display.",
    "Encode the ladder progressive.",
    NONE,
    "Tizen Compat Audit CODEC module",
)
VID_OK = _r(
    "VID-900",
    "video",
    P,
    Owner.PACKAGER,
    "Video bitstream is conformant",
    "Every sampled segment starts on a random access point, carries its parameter sets, and "
    "keeps one coded configuration across the rung.",
    "No action.",
    NONE,
)

# ---------------------------------------------------------------------------
# F. Audio
# ---------------------------------------------------------------------------

AUD_CONFIG_CHANGED = _r(
    "AUD-001",
    "audio",
    C,
    Owner.PACKAGER,
    "AAC configuration changed between segments",
    "The audio object type, sample rate or channel configuration changed from one segment to "
    "the next, so the device reconfigures the audio decoder mid-playback.",
    "Keep the AAC encoder configuration constant for the life of the rendition.",
    DIRECT,
    "HLS AV Doctor AAC config change detection",
)
AUD_CODEC_MISMATCH = _r(
    "AUD-002",
    "audio",
    E,
    Owner.PACKAGER,
    "Declared audio codec does not match the audio object type in the bitstream",
    "The manifest declares one AAC profile and the bitstream carries another, so the device "
    "selects a decoder profile the stream does not use.",
    "Set the CODECS attribute to the audio object type the encoder produces.",
    DIRECT,
    "Tizen Compat Audit CODEC module",
)
AUD_MISSING_IN_MUXED = _r(
    "AUD-003",
    "audio",
    C,
    Owner.PACKAGER,
    "Muxed segment carries no audio elementary stream",
    "The segment holds video with no audio, so playback runs silent for that duration on a "
    "muxed rung.",
    "Mux audio into every segment of this rung.",
    DIRECT,
    "HLS AV Doctor",
)
AUD_SAMPLE_GAP = _r(
    "AUD-004",
    "audio",
    E,
    Owner.PACKAGER,
    "Audio samples leave a gap on the timeline",
    "The audio track has fewer samples than its duration requires, so audio and video drift "
    "apart across the segment.",
    "Emit continuous audio samples for the full segment duration.",
    DIRECT,
    "HLS AV Doctor",
)
AUD_SILENCE = _r(
    "AUD-005",
    "audio",
    W,
    Owner.CONTENT_PROVIDER,
    "Audio is silent for a measurable span",
    "The audio track carries no signal, so the channel plays without sound for that duration.",
    "Restore audio on the contribution feed.",
    NONE,
)
AUD_SEGMENT_MISSING = _r(
    "AUD-006",
    "audio",
    C,
    Owner.PACKAGER,
    "No audio segment covers a video segment's time range",
    "The demuxed audio rendition has no segment spanning the video's timeline position, so "
    "playback has no audio to present with that video.",
    "Publish audio segments covering the same timeline as the video rungs.",
    DIRECT,
    "HLS AV Doctor AUDIO_SEGMENT_MISSING",
)
AUD_DURATION_DRIFT = _r(
    "AUD-007",
    "audio",
    E,
    Owner.PACKAGER,
    "Audio and video segment durations drift apart",
    "The accumulated audio duration diverges from the accumulated video duration, so the two "
    "tracks separate over the length of the window.",
    "Segment audio and video on the same boundaries.",
    DIRECT,
    "HLS AV Doctor DemuxAnalyzer",
)
AUD_AC3_INCONSISTENT = _r(
    "AUD-008",
    "audio",
    C,
    Owner.PACKAGER,
    "AC-3 or E-AC-3 parameters changed between segments",
    "The sample rate, channel mode or bitrate of the Dolby elementary stream changed mid-"
    "rendition, so the device reconfigures the audio decoder.",
    "Keep the Dolby encoder configuration constant for the life of the rendition.",
    DIRECT,
    "Dolby Stream Validator elementary-stream conformance",
)
AUD_AC4_ALIGNMENT = _r(
    "AUD-009",
    "audio",
    E,
    Owner.PACKAGER,
    "AC-4 frames do not align with the segment boundary",
    "AC-4 frames cross the segment boundary, so a player joining at that boundary starts "
    "inside an audio frame.",
    "Align AC-4 frame boundaries with segment boundaries.",
    DIRECT,
    "Dolby Stream Validator AC-4 segment alignment",
)
AUD_OK = _r(
    "AUD-900",
    "audio",
    P,
    Owner.PACKAGER,
    "Audio configuration is constant and complete",
    "Every sampled segment carries audio with the same configuration as the segment before it.",
    "No action.",
    NONE,
)

# ---------------------------------------------------------------------------
# G. A/V synchronisation
# ---------------------------------------------------------------------------

AV_SKEW = _r(
    "AV-001",
    "av_sync",
    E,
    Owner.PACKAGER,
    "Audio and video start timestamps differ beyond the tolerance",
    "The audio track starts at a different timeline position than the video in the same "
    "segment, so the two are presented out of step.",
    "Align the audio and video presentation timestamps at every segment boundary.",
    INDIRECT,
    "Qosifire audio-vs-video PTS delta",
    ("av_skew_normal_ms", "av_skew_error_ms"),
)
AV_SKEW_DRIFT = _r(
    "AV-002",
    "av_sync",
    E,
    Owner.PACKAGER,
    "Audio and video skew grows across consecutive segments",
    "The offset between the tracks increases segment by segment, so the two drift further "
    "apart the longer playback runs.",
    "Lock the audio and video clocks to the same reference in the encoder.",
    INDIRECT,
    "HLS AV Doctor",
    ("av_skew_error_ms",),
)
AV_DSN_MISMATCH = _r(
    "AV-003",
    "av_sync",
    C,
    Owner.PACKAGER,
    "Audio and video playlists declare different discontinuity sequence numbers",
    "The two renditions disagree on how many discontinuities have passed, so the player's "
    "single discontinuity counter cannot serve both.",
    "Emit the same discontinuity count on the audio and video playlists.",
    DIRECT,
    "HLS AV Doctor AV_DISC_SEQ_MISMATCH",
)
AV_ROLLOVER_SPLIT = _r(
    "AV-004",
    "av_sync",
    C,
    Owner.PACKAGER,
    "Presentation timestamp rollover landed in one track and not the other",
    "The 33-bit counter wrapped for one track while the other continued, so the two tracks "
    "resolve to timeline positions a full counter period apart.",
    "Wrap both tracks at the same timestamp, or reset both with a discontinuity.",
    DIRECT,
    "HLS AV Doctor AV_PTS_ROLLOVER_SPLIT",
)
AV_DELTA_CRITICAL = _r(
    "AV-005",
    "av_sync",
    C,
    Owner.PACKAGER,
    "Audio and video timestamps differ by more than one second in a segment",
    "The tracks are more than a second apart inside one segment, so lip sync is lost for the "
    "whole segment.",
    "Align the audio and video presentation timestamps in the packager.",
    DIRECT,
    "Qosifire audio-vs-video PTS delta (red above 1 s)",
    ("av_pts_delta_critical_ms",),
)
AV_OK = _r(
    "AV-900",
    "av_sync",
    P,
    Owner.PACKAGER,
    "Audio and video stay in step",
    "Skew stayed inside the normal tolerance on every sampled segment and did not grow across "
    "the window.",
    "No action.",
    NONE,
)

# ---------------------------------------------------------------------------
# H. Subtitles and captions
# ---------------------------------------------------------------------------

SUB_PARSE = _r(
    "SUB-001",
    "subtitles",
    E,
    Owner.PACKAGER,
    "WebVTT segment does not parse",
    "The segment does not start with the WEBVTT signature or its cue block is malformed, so "
    "the player discards the subtitle track.",
    "Emit conformant WebVTT segments.",
    NONE,
    "Tizen Compat Audit SUBTITLE module",
)
SUB_NO_TIMESTAMP_MAP = _r(
    "SUB-002",
    "subtitles",
    E,
    Owner.PACKAGER,
    "WebVTT segment carries no X-TIMESTAMP-MAP",
    "The subtitle segment has no mapping to the media timeline, so its cues resolve to the "
    "wrong position.",
    "Emit X-TIMESTAMP-MAP in every WebVTT segment.",
    NONE,
    "Tizen Compat Audit SUBTITLE module",
)
SUB_CONTROL_CHARS = _r(
    "SUB-003",
    "subtitles",
    W,
    Owner.PACKAGER,
    "WebVTT segment contains C0 control characters",
    "Control characters in the cue text stop a strict parser at that cue.",
    "Strip C0 control characters from subtitle text.",
    NONE,
    "Tizen Compat Audit SUBTITLE module",
)
SUB_CUE_OVERLAP = _r(
    "SUB-004",
    "subtitles",
    W,
    Owner.PACKAGER,
    "Subtitle cues overlap in time",
    "Two cues claim the same span, so the renderer shows them on top of each other.",
    "Emit non-overlapping cue timings.",
    NONE,
)
SUB_404 = _r(
    "SUB-005",
    "subtitles",
    E,
    Owner.CDN,
    "Subtitle segment returns a non-success status",
    "A listed subtitle segment is unavailable, so the subtitle track stops at that position.",
    "Restore the subtitle segment at the edge.",
    NONE,
)
SUB_CEA_ABSENT = _r(
    "SUB-006",
    "subtitles",
    E,
    Owner.PACKAGER,
    "In-band captions are absent from a rung that declares them",
    "The rung declares CLOSED-CAPTIONS but carries no CEA-608 or CEA-708 data, so captions "
    "disappear when ABR selects that rung.",
    "Carry the same caption service on every rung of the ladder.",
    NONE,
    "HLSAnalyzer CEA-608/708 extraction",
)
SUB_CC_ATTR_MISMATCH = _r(
    "SUB-007",
    "subtitles",
    W,
    Owner.PACKAGER,
    "CLOSED-CAPTIONS attribute does not match the captions carried",
    "The manifest declares a caption service the bitstream does not carry.",
    "Set CLOSED-CAPTIONS to the service the rung actually carries, or to NONE.",
    NONE,
    "HLSAnalyzer CEA-608/708 extraction",
)
SUB_OK = _r(
    "SUB-901",
    "subtitles",
    P,
    Owner.PACKAGER,
    "Subtitle renditions are well formed",
    "Every sampled subtitle segment parsed and carried a timestamp map.",
    "No action.",
    NONE,
)

# ---------------------------------------------------------------------------
# I. SSAI and ads
# ---------------------------------------------------------------------------

ADS_HOST_SLOW = _r(
    "ADS-001",
    "ads",
    E,
    Owner.SSAI,
    "Ad segments are delivered more slowly than content segments",
    "The host serving ad creatives responds more slowly than the content host, so the buffer "
    "drains across every ad break.",
    "Serve ad creatives from an edge with the same delivery profile as the content.",
    DIRECT,
)
ADS_CREATIVE_MISMATCH = _r(
    "ADS-002",
    "ads",
    C,
    Owner.SSAI,
    "Ad creative is encoded differently from the content rung",
    "The creative's resolution, profile or audio configuration differs from the rung it is "
    "spliced into, so the decoder reconfigures at the splice.",
    "Transcode creatives to match the channel ladder before splicing.",
    DIRECT,
    "Past TV Plus investigation",
)
ADS_NO_DISCONTINUITY = _r(
    "ADS-003",
    "ads",
    C,
    Owner.SSAI,
    "Ad boundary carries no discontinuity tag on every rung",
    "The splice changes the encoding without EXT-X-DISCONTINUITY on each rung, so the player "
    "decodes new media with the previous configuration.",
    "Emit EXT-X-DISCONTINUITY at every ad in and ad out on every rung.",
    DIRECT,
    "Past TV Plus investigation",
)
ADS_DSN_DIVERGENCE = _r(
    "ADS-004",
    "ads",
    C,
    Owner.SSAI,
    "Discontinuity counts diverge across rungs during or after an ad break",
    "The rungs leave the break with different discontinuity counts, so the Tizen player's "
    "shared counter freezes at the next switch.",
    "Emit the same number of discontinuities on every rung across the break.",
    DIRECT,
    "Past TV Plus investigation",
)
ADS_DURATION_MISMATCH = _r(
    "ADS-005",
    "ads",
    E,
    Owner.SSAI,
    "Delivered ad break duration differs from the declared duration",
    "The segments spliced into the break sum to a different duration than the cue declares, so "
    "the timeline shifts by the difference at every break.",
    "Deliver ad content matching the declared break duration.",
    DIRECT,
)
ADS_EXTINF_OVER_TD = _r(
    "ADS-006",
    "ads",
    E,
    Owner.SSAI,
    "Ad segment duration exceeds the channel target duration",
    "A spliced segment is longer than the target duration the rung declares, so the player's "
    "refresh schedule misses it.",
    "Segment ad creatives to the channel's segment duration.",
    DIRECT,
)
ADS_CUE_MISSING_IN_MANIFEST = _r(
    "ADS-007",
    "ads",
    E,
    Owner.SSAI,
    "In-band SCTE-35 cue has no matching playlist tag",
    "The transport stream signals a splice the playlist does not advertise, so the player "
    "enters the break with no notice.",
    "Emit EXT-X-CUE-OUT or EXT-X-DATERANGE for every in-band splice.",
    INDIRECT,
    "HLSAnalyzer SCTE-35 extraction",
)
ADS_CUE_MISSING_INBAND = _r(
    "ADS-008",
    "ads",
    W,
    Owner.SSAI,
    "Playlist cue has no matching in-band SCTE-35 signal",
    "The playlist advertises a splice the transport stream does not signal, so downstream "
    "consumers reading the bitstream see no break.",
    "Carry SCTE-35 in-band for every playlist cue.",
    NONE,
    "HLSAnalyzer SCTE-35 extraction",
)
ADS_CUE_DURATION = _r(
    "ADS-009",
    "ads",
    E,
    Owner.SSAI,
    "SCTE-35 break duration differs from the playlist cue duration",
    "The in-band signal and the manifest cue declare different break lengths, so the splice "
    "point and the return point disagree.",
    "Derive the playlist cue duration from the SCTE-35 break duration.",
    INDIRECT,
    "HLSAnalyzer SCTE-35 cue summaries",
)
ADS_CUE_UNBALANCED = _r(
    "ADS-010",
    "ads",
    E,
    Owner.SSAI,
    "EXT-X-CUE-OUT has no matching EXT-X-CUE-IN",
    "A break opened and did not close inside the window, so the player stays in ad state past "
    "the end of the break.",
    "Emit EXT-X-CUE-IN at the end of every break.",
    DIRECT,
)
ADS_OK = _r(
    "ADS-900",
    "ads",
    P,
    Owner.SSAI,
    "Ad breaks are signalled consistently",
    "Every break opened and closed with matching cues, carried a discontinuity on every rung, "
    "and delivered its declared duration.",
    "No action.",
    NONE,
)

# ---------------------------------------------------------------------------
# J. Player telemetry and the Virtual Player Buffer
# ---------------------------------------------------------------------------

PLY_REBUFFER_RATIO = _r(
    "PLY-001",
    "player",
    C,
    Owner.SAMSUNG_PLAYER,
    "Measured rebuffering ratio is above the threshold",
    "Stall time divided by total presentation time is above the configured threshold, which "
    "is the condition that flags this channel as a rebuffering channel.",
    "Resolve the stream-side defects listed in this report; where none is listed, escalate to "
    "the Samsung player and device investigation.",
    DIRECT,
    "HLSAnalyzer rebuffering measurement",
    ("rebuffer_ratio_threshold",),
)
PLY_FATAL = _r(
    "PLY-002",
    "player",
    C,
    Owner.SAMSUNG_PLAYER,
    "Player reported a fatal error",
    "The player stopped with a fatal error, so playback ended.",
    "Attach the player error detail in this report to the player investigation.",
    DIRECT,
    "Akamai reference-player check",
)
PLY_STALL = _r(
    "PLY-003",
    "player",
    E,
    Owner.SAMSUNG_PLAYER,
    "Player stalled",
    "The player ran out of buffered media and waited for data.",
    "Resolve the correlated stream-side defect named in the incident chain.",
    DIRECT,
    "Akamai reference-player check",
)
PLY_DOWNSWITCHES = _r(
    "PLY-004",
    "player",
    W,
    Owner.CDN,
    "Player performed repeated downswitches",
    "ABR moved down the ladder repeatedly, so measured throughput did not sustain the rung "
    "the player had selected.",
    "Raise delivery throughput at the edge, or lower the rung bitrates.",
    INDIRECT,
)
PLY_STARTUP_SLOW = _r(
    "PLY-005",
    "player",
    W,
    Owner.CDN,
    "Startup time exceeds the budget",
    "The player took longer than the budget to present the first frame.",
    "Reduce time to first byte on the master playlist and first segment path.",
    INDIRECT,
    "HLSAnalyzer estimated playback time",
    ("ttfb_budget_ms",),
)
PLY_DROPPED_FRAMES = _r(
    "PLY-006",
    "player",
    W,
    Owner.SAMSUNG_PLAYER,
    "Player dropped frames",
    "The renderer discarded decoded frames, so motion is not presented at the encoded cadence.",
    "Attach the dropped-frame counts in this report to the player investigation.",
    NONE,
)

VPB_RATIO = _r(
    "VPB-001",
    "virtual_buffer",
    C,
    Owner.CDN,
    "Simulated rebuffering ratio is above the threshold",
    "Modelling a Tizen-like player against the measured delivery timings produces a "
    "rebuffering ratio above the configured threshold on this rung.",
    "Resolve the delivery defects correlated with the simulated stalls in this report.",
    DIRECT,
    "HLSAnalyzer virtual buffer; Qosifire per-rendition buffer",
    ("rebuffer_ratio_threshold", "vpb_mode"),
)
VPB_STALL = _r(
    "VPB-002",
    "virtual_buffer",
    E,
    Owner.CDN,
    "Simulated player buffer reached zero",
    "Segments on this rung arrived more slowly than they play, so a player buffer modelled on "
    "the measured timings emptied.",
    "Resolve the correlated delivery defect named in the incident chain.",
    DIRECT,
    "HLSAnalyzer virtual buffer; Qosifire 'Buffer too short'",
    ("vpb_startup_buffer_td_multiple", "vpb_rebuffer_resume_td_multiple"),
)
VPB_BUFFER_LONG = _r(
    "VPB-003",
    "virtual_buffer",
    W,
    Owner.PACKAGER,
    "Simulated player buffer exceeded the maximum a player holds",
    "Media arrived faster than real time for long enough to exceed the buffer a player holds, "
    "so a device discards media or skips forward.",
    "Publish segments at the rate they are produced.",
    INDIRECT,
    "Qosifire 'Buffer too long'; HLSAnalyzer EC-1004",
    ("vpb_max_buffer_s",),
)
VPB_OUTAGE = _r(
    "VPB-004",
    "virtual_buffer",
    C,
    Owner.CDN,
    "No segment on this rung was downloadable for a continuous period",
    "Every segment request in the period failed or timed out, so the rung delivered no media "
    "at all.",
    "Restore delivery for this rung at the edge.",
    DIRECT,
    "HLSAnalyzer outage mode",
    ("vpb_outage_threshold_s",),
)
VPB_OK = _r(
    "VPB-900",
    "virtual_buffer",
    P,
    Owner.CDN,
    "Simulated player buffer never emptied",
    "A player buffer modelled on the measured delivery timings stayed above zero for the whole "
    "analysis window on every sampled rung.",
    "No action.",
    NONE,
)

# ---------------------------------------------------------------------------
# Checks that could not run — stated definitely, never inferred (§5.4)
# ---------------------------------------------------------------------------

SKIP_ENCRYPTED = _r(
    "INFO-001",
    "coverage",
    I,
    Owner.CONTENT_PROVIDER,
    "Bitstream checks did not run on encrypted segments",
    "The segments are encrypted and no matching key was supplied, so the video, audio and A/V "
    "rules had no readable payload to measure.",
    "Supply clear KID and KEY pairs in the job options to include the bitstream checks.",
    NONE,
)
SKIP_FFPROBE = _r(
    "INFO-002",
    "coverage",
    I,
    Owner.SAMSUNG_PLAYER,
    "ffprobe checks did not run",
    "ffprobe is not installed on the analyzer host, so the decode-error and quality detectors "
    "produced no measurement.",
    "Install ffmpeg and ffprobe on the analyzer host.",
    NONE,
)
SKIP_GEOBLOCKED = _r(
    "INFO-003",
    "coverage",
    I,
    Owner.CDN,
    "Checks did not run on a geo-restricted resource",
    "The edge returned HTTP 403 for the analyzer's region, so the checks behind that resource "
    "produced no measurement.",
    "Allow the analyzer host's address range, or supply a URL valid for it.",
    NONE,
)
SKIP_NO_COMPARISON_URL = _r(
    "INFO-004",
    "coverage",
    I,
    Owner.CDN,
    "Layer attribution used header evidence rather than a layer comparison",
    "Only the playback URL was supplied, so each defect is attributed from its own layer and "
    "the response headers that prove it.",
    "Supply the origin, CDN and SSAI URLs to pin each defect to the first layer it appears on.",
    NONE,
)
NO_DEFECT = _r(
    "INFO-900",
    "coverage",
    P,
    Owner.SAMSUNG_PLAYER,
    "No stream-side defect detected",
    "Every check that ran returned a clean measurement across the analysis window.",
    "Escalate to the Samsung player and device investigation with this report attached.",
    NONE,
)


def all_rules() -> list[Rule]:
    return registry.all()
