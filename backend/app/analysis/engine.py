"""Analysis engine.

One `AnalysisSession` runs a whole channel analysis: it resolves the URLs, parses the
master, starts a poller per rendition and a sampler per rung, evaluates every rule as
measurements arrive, and produces findings, incidents, causal chains and one verdict.

Realtime, Aging and Bulk all run this same engine. Only the duration, the sampling policy
and where the events are delivered differ.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.analysis import attribution, av_pairing, correlate, vpb
from app.analysis import evidence as evidence_store
from app.analysis.collectors.playlist_poller import PlaylistPoller, PollTarget, Snapshot
from app.analysis.collectors.segment_sampler import (
    RungSampling,
    SampledSegment,
    SegmentSampler,
    choose_full_rungs,
)
from app.analysis.layout import LadderLayout
from app.analysis.layout import detect as detect_layout
from app.analysis.rules import ads as ads_rules
from app.analysis.rules import catalogue as R
from app.analysis.rules import master as master_rules
from app.analysis.rules import media_playlist as media_rules
from app.analysis.rules import segments as segment_rules
from app.analysis.rules import sequence as sequence_rules
from app.analysis.rules import subtitles as subtitle_rules
from app.analysis.rules import transport as transport_rules
from app.analysis.rules.base import Finding, FindingCollector, Severity, StreamLayer
from app.analysis.verdict import Verdict
from app.analysis.verdict import build as build_verdict
from app.config import (
    DEFAULT_UA_PROFILE,
    TIZEN_USER_AGENT,
    USER_AGENT_PROFILES,
    Thresholds,
    get_settings,
    get_thresholds,
)
from app.drm.context import DrmContext
from app.drm.detect import DrmInfo, describe_keys
from app.drm.settings import DrmSettings
from app.hls import scte35
from app.hls.playlist import MasterPlaylist, MediaPlaylist, is_master, parse_master, parse_media
from app.hls.uri import host_of
from app.media import ffprobe
from app.net import dns as dns_module
from app.net import tls_inspect
from app.net.fetcher import Fetcher

logger = logging.getLogger(__name__)

EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]

LAYER_FIELDS = (
    ("playback_url", StreamLayer.PLAYBACK),
    ("origin_url", StreamLayer.ORIGIN),
    ("cdn_url", StreamLayer.CDN),
    ("ssai_url", StreamLayer.SSAI),
)

DEFAULT_CHECK_SETS = ("baseline",)
OPTIONAL_CHECK_SETS = ("video_quality", "dolby_hdr", "captions", "scte35_inband", "subtitles")


@dataclass
class SessionOptions:
    """Everything a job can configure (§1.3)."""

    duration_s: float = 300.0
    # Resolved by the caller from the Settings default; this is the fallback for a
    # `SessionOptions` built directly, as the CLI and the tests do.
    ua_profile: str = DEFAULT_UA_PROFILE
    check_sets: tuple[str, ...] = DEFAULT_CHECK_SETS
    renditions: tuple[str, ...] | None = None
    record_evidence: bool = False
    clear_keys: dict[str, str] = field(default_factory=dict)
    vpb_mode: str | None = None
    nth_segment_sampling: int | None = None
    max_segment_samples: int = 5000
    snapshot_mode: bool = False

    def enabled(self, name: str) -> bool:
        return name in self.check_sets

    def public(self) -> dict[str, Any]:
        """Job options minus anything that must never be persisted or printed."""
        return {
            "duration_s": self.duration_s,
            "ua_profile": self.ua_profile,
            # The profile id alone does not say what went out on the wire: a reader of an
            # escalation has to be able to see the exact string the CDN and the packager
            # answered, because both can serve differently per User-Agent.
            "user_agent": USER_AGENT_PROFILES.get(self.ua_profile, TIZEN_USER_AGENT),
            "check_sets": list(self.check_sets),
            "renditions": list(self.renditions) if self.renditions else None,
            "record_evidence": self.record_evidence,
            "vpb_mode": self.vpb_mode,
            "nth_segment_sampling": self.nth_segment_sampling,
            "snapshot_mode": self.snapshot_mode,
            "clear_keys_supplied": bool(self.clear_keys),
        }


def _ordinal(value: int) -> str:
    """1st, 2nd, 3rd, 4th. A rule sentence that reads "every 3th segment" is not finished."""
    teens = 10 <= value % 100 <= 20
    suffix = "th" if teens else {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


@dataclass(slots=True)
class SamplingReport:
    """Why one poll of one rung sampled the number of segments it did.

    A session that measures no segment used to look identical to one that was never asked
    to. Every poll now leaves this behind, so "0 segments checked" always carries the
    measurement that produced the nothing.
    """

    variant: str
    at: str = ""
    reason: str = ""
    # The most recent poll.
    listed: int = 0
    eligible: int = 0
    fetched: int = 0
    # The life of the session, so a rung that is sampling normally does not read as idle
    # just because its latest poll listed nothing new.
    polls: int = 0
    total_fetched: int = 0

    def begin_poll(self, at: str, listed: int) -> None:
        self.at = at
        self.reason = ""
        self.listed = listed
        self.eligible = 0
        self.fetched = 0
        self.polls += 1

    def record_fetch(self) -> None:
        self.fetched += 1
        self.total_fetched += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "at": self.at,
            "reason": self.reason,
            "listed": self.listed,
            "eligible": self.eligible,
            "fetched": self.fetched,
            "polls": self.polls,
            "total_fetched": self.total_fetched,
        }


@dataclass
class LayerContext:
    """Per-layer state: the master, the renditions, and everything measured on them."""

    layer: StreamLayer
    url: str
    master: MasterPlaylist | None = None
    master_final_url: str = ""
    targets: dict[str, PollTarget] = field(default_factory=dict)
    pollers: list[PlaylistPoller] = field(default_factory=list)
    samplers: dict[str, SegmentSampler] = field(default_factory=dict)
    state_machines: dict[str, media_rules.PlaylistStateMachine] = field(default_factory=dict)
    histories: dict[str, segment_rules.RungHistory] = field(default_factory=dict)
    buffers: dict[str, vpb.VirtualPlayerBuffer] = field(default_factory=dict)
    bandwidth_by_variant: dict[str, int | None] = field(default_factory=dict)
    target_duration_by_variant: dict[str, float] = field(default_factory=dict)
    # How each rung is packaged. None for a single-rendition channel, which has no master to
    # read a layout off.
    layout: LadderLayout | None = None
    # One per (video rung, audio rendition) pair on a demuxed ladder, keyed by the video
    # rung. A muxed ladder builds none: its skew comes off the segment itself.
    pairings: dict[str, av_pairing.AvPairing] = field(default_factory=dict)
    encrypted: bool = False
    content_host: str = ""
    segment_count: int = 0
    playlist_count: int = 0


@dataclass
class AnalysisResult:
    findings: list[Finding]
    incidents: list[correlate.Incident]
    chains: list[correlate.CausalChain]
    verdict: Verdict
    layer_diffs: list[attribution.LayerDiff]
    vpb_results: dict[str, vpb.VpbResult]
    started_at: dt.datetime
    ended_at: dt.datetime
    thresholds: Thresholds
    options: dict[str, Any]
    ladder: list[dict[str, Any]] = field(default_factory=list)
    redirect_chains: list[dict[str, Any]] = field(default_factory=list)
    event_log: list[dict[str, Any]] = field(default_factory=list)
    owner_summary: dict[str, int] = field(default_factory=dict)
    # What the run's protection amounted to, per rendition. Empty for a clear channel. Never
    # key material: what the system was, which identifier, whether a key was obtained, and
    # how many segments were decrypted.
    drm: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.as_dict(),
            "findings": [f.as_dict() for f in self.findings],
            "incidents": [i.as_dict() for i in self.incidents],
            "chains": [c.as_dict() for c in self.chains],
            "layer_diffs": [d.as_dict() for d in self.layer_diffs],
            "vpb": {k: v.as_dict() for k, v in self.vpb_results.items()},
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "thresholds": self.thresholds.model_dump(mode="json"),
            "options": self.options,
            "ladder": self.ladder,
            "redirect_chains": self.redirect_chains,
            "event_log": self.event_log[-500:],
            "owner_summary": self.owner_summary,
            "drm": self.drm,
        }


class AnalysisSession:
    """Runs one channel analysis to completion."""

    def __init__(
        self,
        *,
        session_id: str,
        playback_url: str,
        origin_url: str | None = None,
        cdn_url: str | None = None,
        ssai_url: str | None = None,
        channel_name: str | None = None,
        options: SessionOptions | None = None,
        thresholds: Thresholds | None = None,
        drm: DrmSettings | None = None,
        on_event: EventHandler | None = None,
    ) -> None:
        self.session_id = session_id
        self.channel_name = channel_name or "(unnamed channel)"
        self.urls = {
            "playback_url": playback_url,
            "origin_url": origin_url,
            "cdn_url": cdn_url,
            "ssai_url": ssai_url,
        }
        self.options = options or SessionOptions()
        # Thresholds are captured at start so a report always names the values its findings
        # were measured against, even if Settings changes mid-job.
        self.thresholds = thresholds or get_thresholds()
        # Captured at start for the same reason the thresholds are: a report states the
        # configuration its findings were produced under.
        self.drm_settings = drm or DrmSettings()
        # Built the moment a playlist declares protection, and shared by every layer and
        # every rendition so one key request serves the whole run.
        self.drm: DrmContext | None = None
        self.on_event = on_event

        self.collector = FindingCollector()
        self.incidents = correlate.IncidentTracker(self.thresholds)
        # The media an evidence bundle is built from. Built only for a run that was asked to
        # record evidence; a run that was not holds no segment bytes at all.
        self.evidence: evidence_store.EvidenceStore | None = (
            evidence_store.EvidenceStore(
                max_bytes=self.thresholds.evidence_max_bytes,
                per_rendition=self.thresholds.evidence_segments_per_rendition,
            )
            if self.options.record_evidence
            else None
        )
        self.layers: dict[StreamLayer, LayerContext] = {}
        self.event_log: list[dict[str, Any]] = []
        self.stalls: list[correlate.StallEvent] = []
        self.player_events: list[dict[str, Any]] = []
        self.redirect_chains: list[dict[str, Any]] = []

        # The latest sampling outcome per rung. Read by `sampling_state`, which is what the
        # UI shows in place of an unexplained zero.
        self.sampling_reports: dict[str, SamplingReport] = {}

        self.started_at = dt.datetime.now(dt.UTC)
        self.ended_at: dt.datetime | None = None
        self._stop = asyncio.Event()
        self._fetcher: Fetcher | None = None
        self._sample_budget = self.options.max_segment_samples
        self._last_ladder_sweep = self.started_at
        self._last_master_poll = self.started_at
        self._segment_queue: asyncio.Queue[tuple[LayerContext, str, Any]] = asyncio.Queue()

    # -- lifecycle -------------------------------------------------------------

    @property
    def elapsed_s(self) -> float:
        return (dt.datetime.now(dt.UTC) - self.started_at).total_seconds()

    @property
    def progress(self) -> float:
        if self.options.duration_s <= 0:
            return 0.0
        return min(1.0, self.elapsed_s / self.options.duration_s)

    @property
    def segments_sampled(self) -> int:
        return sum(context.segment_count for context in self.layers.values())

    def sampling_state(self) -> dict[str, Any]:
        """What segment sampling has done, and — when it has done nothing — why.

        `reason` is empty whenever any segment has been measured. It is only filled in for a
        session that has sampled nothing, which is the case an operator cannot otherwise
        explain from the screen.
        """
        sampled = self.segments_sampled
        reason = ""
        if sampled == 0:
            crashed = [
                poller
                for context in self.layers.values()
                for poller in context.pollers
                if poller.failures
            ]
            if crashed:
                # A detector raising is the analyzer's own defect. It is named first,
                # because no explanation about the stream would be true.
                reason = (
                    f"{len(crashed)} playlist handler(s) failed inside the analyzer, so "
                    f"sampling never ran. The last failure was on {crashed[0].target.variant}: "
                    f"{crashed[0].last_failure.strip().splitlines()[-1]}"
                )
            elif not any(context.targets for context in self.layers.values()):
                reason = "No rendition was resolved from the master playlist, so none is polled."
            else:
                for report in self.sampling_reports.values():
                    if report.reason:
                        reason = report.reason
                        break
                if not reason:
                    reason = "No playlist poll has reached the segment sampler yet."
        return {
            "segments_sampled": sampled,
            "reason": reason,
            "by_variant": [r.as_dict() for r in self.sampling_reports.values()],
        }

    def stop(self) -> None:
        self._stop.set()

    async def _emit(self, kind: str, data: dict[str, Any]) -> None:
        entry = {"type": kind, "ts": dt.datetime.now(dt.UTC).isoformat(), **data}
        if kind in ("event", "finding", "status"):
            self.event_log.append(entry)
            if len(self.event_log) > 5000:
                del self.event_log[:2500]
        if self.on_event is not None:
            with contextlib.suppress(Exception):
                await self.on_event(kind, entry)

    async def _record(self, findings: list[Finding] | Finding | None) -> None:
        if findings is None:
            return
        items = findings if isinstance(findings, list) else [findings]
        for finding in items:
            merged = self.collector.add(finding)
            if merged is not None and merged.count == 1:
                await self._emit("finding", {"data": merged.as_dict()})

    async def run(self) -> AnalysisResult:
        settings = get_settings()
        self._fetcher = Fetcher(
            ua_profile=self.options.ua_profile,
            timeout_s=self.thresholds.request_timeout_s,
            per_host_connections=settings.rba_per_host_connections,
        )
        try:
            await self._emit("status", {"state": "RESOLVING", "channel": self.channel_name})
            await self._bootstrap_layers()
            await self._emit("status", {"state": "RUNNING", "channel": self.channel_name})
            await self._collect()
        finally:
            await self._shutdown_pollers()
            if self._fetcher is not None:
                await self._fetcher.aclose()
        return await self._finalise()

    # -- bootstrap -------------------------------------------------------------

    async def _bootstrap_layers(self) -> None:
        assert self._fetcher is not None
        for field_name, layer in LAYER_FIELDS:
            url = self.urls.get(field_name)
            if not url:
                continue
            context = LayerContext(layer=layer, url=url)
            self.layers[layer] = context
            await self._bootstrap_layer(context)

        if len(self.layers) == 1:
            await self._record(
                R.SKIP_NO_COMPARISON_URL.raise_finding(
                    "Only the playback URL was supplied, so each finding is attributed from its "
                    "own layer and the response headers measured on it.",
                    evidence={"layers": [layer.value for layer in self.layers]},
                )
            )

        if not ffprobe.binaries().available:
            await self._record(
                R.SKIP_FFPROBE.raise_finding(
                    "ffprobe and ffmpeg are not installed on this analyzer host, so the "
                    "decode-error and quality detectors produced no measurement. Every other "
                    "check ran on the pure-Python parsers.",
                    evidence={"ffprobe": None, "ffmpeg": None},
                )
            )

    async def _bootstrap_layer(self, context: LayerContext) -> None:
        assert self._fetcher is not None
        layer = context.layer

        dns_result = await dns_module.resolve(context.url)
        await self._record(
            transport_rules.check_dns(dns_result, layer=layer, thresholds=self.thresholds)
        )
        await self._emit(
            "event", {"kind": "dns", "layer": layer.value, "data": dns_result.as_dict()}
        )

        tls_result = await tls_inspect.inspect(context.url)
        await self._record(transport_rules.check_tls(tls_result, layer=layer))
        if tls_result:
            await self._emit(
                "event", {"kind": "tls", "layer": layer.value, "data": tls_result.as_dict()}
            )

        result = await self._fetcher.fetch(context.url)
        context.playlist_count += 1
        self.redirect_chains.append({"layer": layer.value, **result.as_dict()})
        await self._record(
            transport_rules.check_http(
                result, layer=layer, thresholds=self.thresholds, kind="playlist"
            )
        )
        await self._emit(
            "event",
            {"kind": "resolve", "layer": layer.value, "data": result.as_dict()},
        )

        if result.status == 403:
            await self._record(
                R.SKIP_GEOBLOCKED.raise_finding(
                    f"{context.url} returned HTTP 403 to this analyzer host, so no check behind "
                    "that response produced a measurement.",
                    evidence={"url": context.url, "headers": result.cdn_fingerprint()},
                    stream_layer=layer,
                )
            )
            return
        if not result.ok:
            return

        context.master_final_url = result.final_url
        context.content_host = host_of(result.final_url)
        text = result.text

        if not is_master(text):
            # A single-rendition channel: the playback URL is itself a media playlist.
            media = parse_media(text, result.final_url)
            context.targets["media"] = PollTarget(variant="media", url=context.url, kind="video")
            context.target_duration_by_variant["media"] = media.target_duration or 6.0
            context.bandwidth_by_variant["media"] = None
            context.encrypted = media.is_encrypted
            context.samplers["media"] = SegmentSampler(
                RungSampling(variant="media", full=True),
                self._fetcher,
                encrypted=media.is_encrypted,
                drm=self._ensure_drm(describe_keys(media.keys), context)
                if media.is_encrypted
                else None,
            )
            return

        master = parse_master(text, result.final_url)
        context.master = master
        context.encrypted = master.is_encrypted
        # Which rungs carry their audio and which point at a rendition for it. Read once,
        # here, and consulted by every check that asks what a segment is supposed to contain.
        context.layout = detect_layout(master)
        drm = (
            self._ensure_drm(describe_keys(master.session_keys), context)
            if master.is_encrypted
            else None
        )
        await self._record(
            master_rules.check_master(master, layer=layer, thresholds=self.thresholds)
        )
        await self._record(
            transport_rules.check_relative_uri_host_shift(
                playlist_url=context.url,
                final_url=result.final_url,
                child_uris=[v.uri for v in master.variants],
                variant="master",
                layer=layer,
            )
        )
        await self._emit(
            "event",
            {
                "kind": "master",
                "layer": layer.value,
                "data": {
                    "url": result.final_url,
                    "variants": [
                        {
                            "id": v.variant_id,
                            "bandwidth": v.bandwidth,
                            "resolution": v.resolution,
                            "frame_rate": v.frame_rate,
                            "codecs": v.codecs,
                            "uri": v.resolved_uri,
                        }
                        for v in master.variants
                    ],
                    "renditions": [
                        {
                            "id": r.variant_id,
                            "type": r.type,
                            "group": r.group_id,
                            "uri": r.resolved_uri,
                        }
                        for r in master.renditions
                    ],
                },
            },
        )

        selected = self._select_variants(master)
        full_rungs = choose_full_rungs([v.variant_id for v in selected])
        nth = self.options.nth_segment_sampling or self.thresholds.nth_segment_sampling_other_rungs

        for variant in selected:
            context.targets[variant.variant_id] = PollTarget(
                variant=variant.variant_id, url=variant.resolved_uri, kind="video"
            )
            context.bandwidth_by_variant[variant.variant_id] = variant.bandwidth
            context.samplers[variant.variant_id] = SegmentSampler(
                RungSampling(
                    variant=variant.variant_id,
                    full=variant.variant_id in full_rungs,
                    nth=nth,
                ),
                self._fetcher,
                encrypted=context.encrypted,
                drm=drm,
            )

        for rendition in master.renditions:
            if not rendition.resolved_uri:
                continue
            if rendition.type == "SUBTITLES" and not self.options.enabled("subtitles"):
                # Subtitle playlists are still polled: MED-013 needs them.
                pass
            kind = "audio" if rendition.type == "AUDIO" else "subtitles"
            context.targets[rendition.variant_id] = PollTarget(
                variant=rendition.variant_id, url=rendition.resolved_uri, kind=kind
            )
            context.bandwidth_by_variant[rendition.variant_id] = None
            context.samplers[rendition.variant_id] = SegmentSampler(
                RungSampling(variant=rendition.variant_id, full=kind == "audio", nth=nth),
                self._fetcher,
                encrypted=context.encrypted,
                drm=drm,
            )

        self._build_pairings(context, selected)

    def _build_pairings(self, context: LayerContext, selected: list[Any]) -> None:
        """One pairing per demuxed rung, against the audio rendition it is polled with.

        A rung whose audio group resolves to several renditions — a channel with an English
        and a Spanish track — is paired against the first the master lists, which is the one
        the ladder presents by default. Nothing is paired for a muxed rung: its skew is read
        off its own segments, as it always was.
        """
        if context.layout is None:
            return
        polled = set(context.targets)
        for variant in selected:
            layout = context.layout.get(variant.variant_id)
            if layout is None or not layout.demuxed:
                continue
            audio = next((a for a in layout.audio_variants if a in polled), None)
            if audio is None:
                continue
            context.pairings[variant.variant_id] = av_pairing.AvPairing(
                video_variant=variant.variant_id,
                audio_variant=audio,
                defer_refreshes=self.thresholds.av_pair_defer_refreshes,
                min_overlap_fraction=self.thresholds.av_pair_min_overlap_fraction,
            )

    def _ensure_drm(self, declared: DrmInfo, context: LayerContext) -> DrmContext | None:
        """The run's DRM context, built the first time a playlist declares protection.

        One context per run, not per layer: the playback, CDN, origin and SSAI URLs carry the
        same content under the same keys, so a key requested for one serves all four and the
        key server is asked once.
        """
        if not self.drm_settings.enabled:
            return None
        if self.drm is None:
            self.drm = DrmContext(
                declared=declared,
                credentials=self.drm_settings.credentials,
                content_id=self.drm_settings.cpix_content_id,
                fetcher=self._fetcher,
            )
        elif not self.drm.declared.kid and declared.kid:
            # A later playlist named the key identifier the first one left out.
            self.drm.declared.kid = declared.kid
        context.encrypted = True
        return self.drm

    def _select_variants(self, master: MasterPlaylist) -> list[Any]:
        if not self.options.renditions:
            return list(master.variants)
        wanted = set(self.options.renditions)
        chosen = [v for v in master.variants if v.variant_id in wanted]
        return chosen or list(master.variants)

    # -- collection ------------------------------------------------------------

    async def _collect(self) -> None:
        assert self._fetcher is not None
        for context in self.layers.values():
            for target in context.targets.values():
                poller = PlaylistPoller(
                    target,
                    self._fetcher,
                    on_snapshot=self._make_snapshot_handler(context),
                )
                context.pollers.append(poller)
                poller.start()

        deadline = self.started_at + dt.timedelta(seconds=self.options.duration_s)
        while not self._stop.is_set() and dt.datetime.now(dt.UTC) < deadline:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            await self._periodic()
            await self._emit(
                "metric",
                {
                    "kind": "progress",
                    "data": {
                        "progress": self.progress,
                        "elapsed_s": self.elapsed_s,
                        "findings": len(self.collector),
                        "counts": self.collector.counts(),
                        "sampling": self.sampling_state(),
                    },
                },
            )

    def _make_snapshot_handler(
        self, context: LayerContext
    ) -> Callable[[Snapshot], Awaitable[None]]:
        async def handler(snapshot: Snapshot) -> None:
            await self._on_snapshot(context, snapshot)

        return handler

    async def _on_snapshot(self, context: LayerContext, snapshot: Snapshot) -> None:
        layer = context.layer
        variant = snapshot.variant
        target = context.targets[variant]
        context.playlist_count += 1

        machine = context.state_machines.setdefault(
            variant, media_rules.PlaylistStateMachine(variant=variant)
        )
        target_duration = (
            snapshot.playlist.target_duration
            if snapshot.playlist and snapshot.playlist.target_duration
            else context.target_duration_by_variant.get(variant, 6.0)
        )
        context.target_duration_by_variant[variant] = target_duration

        await self._record(
            transport_rules.check_http(
                snapshot.result,
                layer=layer,
                thresholds=self.thresholds,
                kind="playlist",
                variant=variant,
            )
        )

        failure = media_rules.describe_fetch_failure(
            snapshot.result, snapshot.playlist, parse_error=snapshot.parse_error
        )
        transition = machine.observe(
            at=snapshot.at,
            http_ok=snapshot.result.ok,
            playlist=snapshot.playlist,
            stale_after_s=target_duration * self.thresholds.stale_playlist_factor,
            failure=failure,
        )
        if transition is not None:
            await self._record(
                media_rules.check_state_transition(transition, variant=variant, layer=layer)
            )
            await self._emit(
                "event",
                {
                    "kind": "playlist_state",
                    "layer": layer.value,
                    "variant": variant,
                    "data": {"from": transition.previous.value, "to": transition.current.value},
                },
            )

        stale = media_rules.check_freshness(
            machine=machine,
            at=snapshot.at,
            target_duration=target_duration,
            variant=variant,
            layer=layer,
            thresholds=self.thresholds,
        )
        await self._record(stale)
        self.incidents.observe(
            kind="stale_playlist",
            variant=variant,
            at=snapshot.at,
            degraded=stale is not None,
            cause_rule_ids=[R.MED_STALE.id] if stale else [],
        )

        # Freshness and the playlist state are computed here and nowhere else, so they ride
        # the snapshot: a stored sample without them would draw a flat zero on the freshness
        # chart, which claims a measurement that was never taken.
        await self._emit(
            "playlist_snapshot",
            {
                "layer": layer.value,
                "data": {
                    **snapshot.summary(),
                    "freshness_s": machine.seconds_since_new_segment(snapshot.at),
                    "state": machine.state.value,
                },
            },
        )

        if not snapshot.ok or snapshot.playlist is None:
            return

        playlist = snapshot.playlist
        await self._record(
            media_rules.check_media_playlist(
                playlist,
                variant=variant,
                layer=layer,
                thresholds=self.thresholds,
                is_subtitle=target.kind == "subtitles",
            )
        )
        await self._record(
            transport_rules.check_playlist_caching(
                snapshot.result,
                target_duration=target_duration,
                layer=layer,
                variant=variant,
                thresholds=self.thresholds,
            )
        )
        await self._record(
            ads_rules.check_cue_windows(
                playlist, variant=variant, layer=layer, thresholds=self.thresholds
            )
        )

        previous = target.previous
        if previous is not None and previous.playlist is not None:
            await self._record(
                sequence_rules.check_sequence_transition(
                    previous.playlist,
                    playlist,
                    variant=variant,
                    layer=layer,
                    at=snapshot.at,
                    cdn_headers={
                        k.lower(): v for k, v in snapshot.result.cdn_fingerprint().items()
                    },
                )
            )
            await self._record(
                media_rules.check_segment_uri_changed(
                    previous.playlist, playlist, variant=variant, layer=layer
                )
            )
            if target.kind == "subtitles":
                await self._record(
                    media_rules.check_subtitle_stuck(
                        previous.playlist, playlist, variant=variant, layer=layer
                    )
                )
            added = _added_duration(previous.playlist, playlist)
            elapsed = (snapshot.at - previous.at).total_seconds()
            await self._record(
                media_rules.check_publication_rate(
                    added_duration_s=added,
                    elapsed_wall_s=elapsed,
                    variant=variant,
                    layer=layer,
                )
            )

        await self._sample_new_segments(context, target, playlist, snapshot)
        # A refresh of the audio rendition is the event that could have published the segment
        # a deferred pair is waiting for, so the deferral clock advances here and nowhere
        # else. A video segment whose audio never arrives is given up on rather than measured
        # against whichever audio segment happens to be in the window.
        if target.kind == "audio":
            for pairing in context.pairings.values():
                if pairing.audio_variant == variant:
                    pairing.expire()
        await self._check_cross_variant(context, snapshot.at)

    async def _sample_new_segments(
        self,
        context: LayerContext,
        target: PollTarget,
        playlist: MediaPlaylist,
        snapshot: Snapshot,
    ) -> None:
        report = self.sampling_reports.setdefault(
            target.variant, SamplingReport(variant=target.variant)
        )
        report.begin_poll(snapshot.at.isoformat(), len(playlist.segments))

        sampler = context.samplers.get(target.variant)
        if sampler is None:
            report.reason = (
                f"{target.variant} is polled but has no segment sampler, so no segment of it "
                "is fetched."
            )
            return
        if self._sample_budget <= 0:
            report.reason = (
                f"The sample budget of {self.options.max_segment_samples} segment(s) is spent "
                "for this session."
            )
            return
        if not playlist.segments:
            report.reason = (
                f"{target.variant} returned a playlist that lists no segment, so there is "
                "nothing to fetch."
            )
            return

        # A ladder can declare its protection in the media playlists rather than on the
        # master, so a rendition that turns out to be protected is marked here — before its
        # initialisation segment is fetched, which is where the key is obtained.
        if playlist.is_encrypted and not sampler.encrypted:
            sampler.encrypted = True
            context.encrypted = True
            self._ensure_drm(describe_keys(playlist.keys), context)
            sampler.drm = self.drm

        if playlist.map_uri:
            init_result = await sampler.ensure_init(playlist.map_uri)
            if init_result is not None and not init_result.ok:
                await self._record(
                    R.SEG_BAD_INIT.raise_finding(
                        f"The EXT-X-MAP target {playlist.map_uri} for {target.variant} returned "
                        + (
                            f"HTTP {init_result.status}."
                            if init_result.status
                            else f"no response: {init_result.error}."
                        ),
                        evidence={"variant": target.variant, "uri": playlist.map_uri},
                        stream_layer=context.layer,
                        variant=target.variant,
                    )
                )

        history = context.histories.setdefault(
            target.variant, segment_rules.RungHistory(variant=target.variant)
        )
        buffer = context.buffers.get(target.variant)
        if buffer is None:
            buffer = vpb.VirtualPlayerBuffer(
                target.variant,
                target_duration=context.target_duration_by_variant.get(target.variant, 6.0),
                thresholds=self.thresholds,
                mode=_vpb_mode(self.options.vpb_mode),
                # The device configures its queue for the channel, so every rung of one
                # ladder is modelled with the profile the tallest rung earns.
                top_height=_top_height(context),
            )
            context.buffers[target.variant] = buffer

        for segment in playlist.segments:
            if self._sample_budget <= 0 or self._stop.is_set():
                break
            if not sampler.sampling.should_fetch(segment.msn):
                continue
            report.eligible += 1
            self._sample_budget -= 1

            sample = await sampler.fetch_segment(
                msn=segment.msn,
                uri=segment.resolved_uri,
                declared_duration=segment.duration,
                discontinuity_before=segment.discontinuity_before,
                byterange=segment.byterange,
            )
            context.segment_count += 1
            report.record_fetch()
            await self._on_segment(context, target, sample, buffer, history, snapshot)

        if report.fetched == 0:
            policy = (
                "every segment"
                if sampler.sampling.full
                else f"every {_ordinal(sampler.sampling.nth)} segment"
            )
            report.reason = (
                f"{target.variant} lists {report.listed} segment(s), all of which this "
                f"session has already sampled. The rung samples {policy}."
            )

    async def _on_segment(
        self,
        context: LayerContext,
        target: PollTarget,
        sample: SampledSegment,
        buffer: vpb.VirtualPlayerBuffer,
        history: segment_rules.RungHistory,
        snapshot: Snapshot,
    ) -> None:
        layer = context.layer
        variant = sample.variant

        await self._record(
            transport_rules.check_http(
                sample.result,
                layer=layer,
                thresholds=self.thresholds,
                kind="segment",
                variant=variant,
                listed_in_playlist=True,
            )
        )

        buffer.feed(
            vpb.SegmentDelivery(
                msn=sample.msn,
                completed_at=sample.completed_at,
                duration_s=sample.declared_duration or 0.0,
                available=sample.available,
                download_ms=sample.download_ms,
                uri=sample.uri,
                # The byte cap is half the buffer model, so the measured size travels with it.
                bytes=sample.result.bytes_received,
            )
        )

        self.incidents.observe(
            kind="segment_delivery",
            variant=variant,
            at=sample.completed_at,
            degraded=not sample.available,
            cause_rule_ids=[R.SEG_DOWNLOAD_FAIL.id] if not sample.available else [],
        )

        if not sample.available:
            await self._emit("segment_result", {"layer": layer.value, "data": sample.as_dict()})
            return

        await self._record(
            transport_rules.check_download_ratio(
                download_ms=sample.download_ms,
                declared_duration=sample.declared_duration,
                url=sample.uri,
                variant=variant,
                layer=layer,
                thresholds=self.thresholds,
            )
        )
        await self._record(
            transport_rules.check_throughput(
                measured_bps=sample.result.throughput_bps,
                declared_bandwidth=context.bandwidth_by_variant.get(variant),
                url=sample.uri,
                variant=variant,
                layer=layer,
            )
        )
        await self._record(
            segment_rules.check_segment(
                sample.analysis,
                variant=variant,
                layer=layer,
                thresholds=self.thresholds,
                declared_bandwidth=context.bandwidth_by_variant.get(variant),
                is_audio_only=target.kind == "audio",
                layout=context.layout.get(variant) if context.layout else None,
                at=sample.completed_at,
            )
        )
        await self._pair_across_renditions(context, target, sample)
        await self._record(
            segment_rules.check_segment_pair(
                history,
                sample.analysis,
                layer=layer,
                thresholds=self.thresholds,
                discontinuity_before=sample.discontinuity_before,
                at=sample.completed_at,
            )
        )

        if sample.analysis.container == "webvtt":
            await self._record(
                subtitle_rules.check_webvtt(
                    sample.analysis.raw.get("webvtt_head", ""),
                    variant=variant,
                    uri=sample.uri,
                    layer=layer,
                )
            )

        if self.options.enabled("scte35_inband") and sample.analysis.scte35:
            windows = scte35.extract_cue_windows(snapshot.playlist) if snapshot.playlist else []
            inband = [
                scte35.SpliceInfo(
                    command_type=item.get("command_type", -1),
                    command_name=item.get("command", ""),
                    out_of_network=item.get("out_of_network"),
                    break_duration_s=item.get("break_duration_s"),
                    segmentation_type_id=item.get("segmentation_type_id"),
                )
                for item in sample.analysis.scte35
            ]
            await self._record(
                ads_rules.check_cue_agreement(
                    inband=inband,
                    playlist_windows=windows,
                    variant=variant,
                    layer=layer,
                    at=sample.completed_at,
                )
            )

        if (
            self.options.enabled("video_quality")
            and ffprobe.binaries().available
            and not sample.analysis.encrypted
        ):
            await self._run_quality_detectors(sample, variant=variant, layer=layer)

        self._keep_evidence(target, sample)

        await self._emit("segment_result", {"layer": layer.value, "data": sample.as_dict()})

    def _keep_evidence(self, target: PollTarget, sample: SampledSegment) -> None:
        """Hold this segment's bytes for the bundle, if this run was asked to record any.

        A protected segment is kept as it was served and, when a key was obtained, as it was
        decrypted. Nothing here touches the key: `SampledSegment.decoded_body` is what the
        decrypt already produced for the bitstream rules, and it is reused rather than
        decrypting a second time.
        """
        if self.evidence is None or not sample.available:
            return
        self.evidence.add(
            evidence_store.EvidenceSegment(
                variant=target.variant,
                kind=target.kind,
                msn=sample.msn,
                uri=sample.uri,
                at=sample.completed_at,
                raw=sample.result.body,
                decrypted=sample.decoded_body,
                drm_reason=sample.drm_reason,
                # Sampled while something was already wrong, so the last thing evicted.
                pinned=self.incidents.anything_open,
            )
        )

    async def _pair_across_renditions(
        self, context: LayerContext, target: PollTarget, sample: SampledSegment
    ) -> None:
        """Feed this segment into every pairing it belongs to and measure what that unlocks.

        A video segment goes to its own rung's pairing; an audio segment goes to every rung
        that takes its audio from that rendition, because one audio rendition usually serves
        the whole ladder.
        """
        if not context.pairings:
            return
        variant = target.variant
        analysis = sample.analysis
        segment = av_pairing.TrackSegment(
            variant=variant,
            msn=analysis.msn,
            uri=analysis.uri,
            first_pts=(
                analysis.audio_first_pts if target.kind == "audio" else analysis.video_first_pts
            ),
            last_pts=(
                analysis.audio_last_pts if target.kind == "audio" else analysis.video_last_pts
            ),
            at=sample.completed_at,
        )

        touched: list[av_pairing.AvPairing] = []
        if target.kind == "audio":
            for pairing in context.pairings.values():
                if pairing.audio_variant == variant:
                    pairing.add_audio(segment)
                    touched.append(pairing)
        else:
            own = context.pairings.get(variant)
            if own is not None:
                own.add_video(segment)
                touched.append(own)

        for pairing in touched:
            await self._record(
                segment_rules.check_cross_rendition_av(
                    pairing,
                    layer=context.layer,
                    thresholds=self.thresholds,
                    at=sample.completed_at,
                )
            )
            pairing.trim()

    async def _run_quality_detectors(
        self, sample: SampledSegment, *, variant: str, layer: StreamLayer
    ) -> None:
        # The decrypted payload where the segment was protected, and the segment itself
        # otherwise. Handing a decoder an encrypted payload produces decode errors that are
        # the protection, not the channel.
        body = sample.decodable_body
        try:
            errors = await ffprobe.decode_errors(body)
        except (TimeoutError, ffprobe.FfprobeUnavailable):
            return
        if errors:
            await self._record(
                R.VID_DECODE_ERROR.raise_finding(
                    f"Decoding segment {sample.msn} on {variant} produced {len(errors)} error "
                    f"line(s), the first being: {errors[0]}",
                    evidence={"variant": variant, "msn": sample.msn, "errors": errors[:5]},
                    stream_layer=layer,
                    variant=variant,
                    at=sample.completed_at,
                )
            )
        try:
            detections = await ffprobe.detect_black_and_freeze(body)
        except (TimeoutError, ffprobe.FfprobeUnavailable):
            return
        if detections["black"]:
            await self._record(
                R.VID_BLACK.raise_finding(
                    f"Segment {sample.msn} on {variant} contains "
                    f"{len(detections['black'])} black interval(s), the first lasting "
                    f"{detections['black'][0].get('black_duration', 0):.2f} s.",
                    evidence={"variant": variant, "msn": sample.msn, "black": detections["black"]},
                    stream_layer=layer,
                    variant=variant,
                    at=sample.completed_at,
                )
            )
        if detections["freeze"]:
            await self._record(
                R.VID_FROZEN.raise_finding(
                    f"Segment {sample.msn} on {variant} contains "
                    f"{len(detections['freeze'])} frozen interval(s).",
                    evidence={
                        "variant": variant,
                        "msn": sample.msn,
                        "freeze": detections["freeze"],
                    },
                    stream_layer=layer,
                    variant=variant,
                    at=sample.completed_at,
                )
            )

    async def _check_cross_variant(self, context: LayerContext, at: dt.datetime) -> None:
        snapshots: list[sequence_rules.VariantSnapshot] = []
        for variant, target in context.targets.items():
            latest = target.latest
            if latest and latest.playlist:
                snapshots.append(
                    sequence_rules.VariantSnapshot(
                        variant=variant, playlist=latest.playlist, at=latest.at, kind=target.kind
                    )
                )
        if len(snapshots) < 2:
            return
        findings = sequence_rules.check_cross_variant(
            snapshots, layer=context.layer, thresholds=self.thresholds, at=at
        )
        await self._record(findings)
        self.incidents.observe(
            kind="cross_variant",
            variant=None,
            at=at,
            degraded=any(f.severity.rank >= 3 for f in findings),
            cause_rule_ids=[f.rule.id for f in findings],
        )

    async def _periodic(self) -> None:
        now = dt.datetime.now(dt.UTC)

        if (
            now - self._last_master_poll
        ).total_seconds() >= self.thresholds.master_repoll_interval_s:
            self._last_master_poll = now
            await self._repoll_masters()

        if (
            now - self._last_ladder_sweep
        ).total_seconds() >= self.thresholds.ladder_sweep_interval_s:
            self._last_ladder_sweep = now
            await self._ladder_sweep()

    async def _repoll_masters(self) -> None:
        assert self._fetcher is not None
        for context in self.layers.values():
            if context.master is None:
                continue
            result = await self._fetcher.fetch(context.url)
            if not result.ok:
                continue
            text = result.text
            if not is_master(text):
                await self._record(
                    R.MST_MEDIA_BECAME_MASTER.raise_finding(
                        f"{context.url} no longer returns a master playlist.",
                        evidence={"url": context.url},
                        stream_layer=context.layer,
                    )
                )
                continue
            current = parse_master(text, result.final_url)
            await self._record(
                master_rules.check_master_changed(
                    context.master, current, layer=context.layer, thresholds=self.thresholds
                )
            )
            context.master = current

    async def _ladder_sweep(self) -> None:
        """Compare the same MSN across every rung, and the ladder's DPB footprints."""
        for context in self.layers.values():
            sps_by_variant = {
                variant: history.last.sps
                for variant, history in context.histories.items()
                if history.last and history.last.sps
            }
            if len(sps_by_variant) >= 2:
                await self._record(
                    segment_rules.check_dpb_across_rungs(sps_by_variant, layer=context.layer)
                )
            await self._record(
                segment_rules.check_keyframe_alignment(context.histories, layer=context.layer)
            )

            if context.master:
                for variant in context.master.variants:
                    history = context.histories.get(variant.variant_id)
                    if history is None or history.last is None:
                        continue
                    await self._record(
                        master_rules.check_cross_level(
                            variant=variant,
                            measured={
                                "sps": history.last.sps,
                                "aac_config": history.last.aac_config,
                                "audio_codec": history.last.audio_codec,
                            },
                            layer=context.layer,
                        )
                    )

    async def _shutdown_pollers(self) -> None:
        for context in self.layers.values():
            await asyncio.gather(
                *(poller.stop() for poller in context.pollers), return_exceptions=True
            )

    # -- player telemetry ------------------------------------------------------

    async def ingest_player_event(self, payload: dict[str, Any]) -> None:
        """Player telemetry arriving over the WebSocket (Realtime only)."""
        self.player_events.append(payload)
        kind = payload.get("event", "")
        at = _parse_ts(payload.get("ts")) or dt.datetime.now(dt.UTC)

        if kind == "stall_end":
            duration = float(payload.get("stall_duration_s") or 0.0)
            started = at - dt.timedelta(seconds=duration)
            self.stalls.append(
                correlate.StallEvent(
                    started_at=started,
                    ended_at=at,
                    variant=str(payload.get("variant") or ""),
                    source="player",
                    duration_s=duration,
                )
            )
            await self._record(
                R.PLY_STALL.raise_finding(
                    f"The player stalled for {duration:.1f} s at {started.isoformat()} on "
                    f"{payload.get('variant') or 'the selected rung'}.",
                    evidence=payload,
                    variant=str(payload.get("variant") or "") or None,
                    at=started,
                )
            )
        elif kind == "error" and payload.get("fatal"):
            await self._record(
                R.PLY_FATAL.raise_finding(
                    f"The player reported a fatal error: {payload.get('details')} "
                    f"(type {payload.get('error_type')}).",
                    evidence=payload,
                    at=at,
                )
            )
        elif kind == "dropped_frames":
            dropped = int(payload.get("dropped") or 0)
            if dropped > 0:
                await self._record(
                    R.PLY_DROPPED_FRAMES.raise_finding(
                        f"The player dropped {dropped} frame(s) by {at.isoformat()}.",
                        evidence=payload,
                        at=at,
                    )
                )

        await self._emit("player_event", {"data": payload})

    # -- finalisation ----------------------------------------------------------

    async def _finalise(self) -> AnalysisResult:
        self.ended_at = dt.datetime.now(dt.UTC)
        window = (self.ended_at - self.started_at).total_seconds()

        vpb_results: dict[str, vpb.VpbResult] = {}
        for context in self.layers.values():
            for variant, buffer in context.buffers.items():
                result = buffer.finish(self.ended_at)
                key = f"{context.layer.value}:{variant}" if len(self.layers) > 1 else variant
                vpb_results[key] = result
                await self._record(
                    vpb.findings_for(result, layer=context.layer, thresholds=self.thresholds)
                )
                for stall in result.counted_stalls:
                    self.stalls.append(
                        correlate.StallEvent(
                            started_at=stall.started_at,
                            ended_at=stall.ended_at,
                            variant=variant,
                            source="vpb",
                            duration_s=stall.duration_s,
                        )
                    )

        await self._record(self._player_ratio_finding())
        await self._check_demuxed_coverage()

        self.incidents.close_all(self.ended_at)
        findings = self.collector.all()
        findings, diffs = attribution.attribute(findings, layers_analysed=set(self.layers.keys()))

        target_duration = _representative_target_duration(self.layers)
        chains = correlate.correlate(
            self.stalls, findings, target_duration=target_duration, thresholds=self.thresholds
        )
        self.incidents.attach_chains(chains)

        worst_variant, worst_ratio = _worst_ratio(vpb_results, self.player_events)
        playlists = sum(context.playlist_count for context in self.layers.values())
        segments = sum(context.segment_count for context in self.layers.values())

        verdict = build_verdict(
            findings=findings,
            chains=chains,
            incidents=self.incidents.incidents,
            thresholds=self.thresholds,
            window_seconds=window,
            playlists_checked=playlists,
            segments_checked=segments,
            measured_ratio=worst_ratio,
            worst_variant=worst_variant,
        )

        if verdict.status.name == "NO_STREAM_SIDE_DEFECT":
            await self._record(
                R.NO_DEFECT.raise_finding(verdict.headline, evidence={"window_s": window})
            )
            findings = self.collector.all()

        await self._emit("verdict", {"data": verdict.as_dict()})

        return AnalysisResult(
            findings=findings,
            incidents=self.incidents.incidents,
            chains=chains,
            verdict=verdict,
            layer_diffs=diffs,
            vpb_results=vpb_results,
            started_at=self.started_at,
            ended_at=self.ended_at,
            thresholds=self.thresholds,
            options=self.options.public(),
            ladder=self._ladder_table(),
            redirect_chains=self.redirect_chains,
            event_log=self.event_log,
            owner_summary=attribution.owner_summary(findings),
            drm=self.drm.summary() if self.drm is not None else {},
        )

    async def _check_demuxed_coverage(self) -> None:
        """AUD-006 and AUD-007, once per demuxed rung, over the whole window.

        Both are questions about the window rather than about one segment — whether any
        audio segment covers a video segment's range, and whether the two totals drifted —
        so they are asked once, at the end, on everything that was sampled. The detector has
        been in the rule set since the beginning and nothing ever called it, so `AUD-006`
        and `AUD-007` were declared and unreachable.
        """
        for context in self.layers.values():
            for pairing in context.pairings.values():
                video_ranges, audio_ranges = pairing.ranges()
                await self._record(
                    segment_rules.check_demuxed_audio_coverage(
                        video_ranges=video_ranges,
                        audio_ranges=audio_ranges,
                        variant=pairing.video_variant,
                        layer=context.layer,
                    )
                )

    def _player_ratio_finding(self) -> Finding | None:
        stalls = [e for e in self.player_events if e.get("event") == "stall_end"]
        if not stalls:
            return None
        stall_s = sum(float(e.get("stall_duration_s") or 0.0) for e in stalls)
        playing = max(0.0, self.elapsed_s - stall_s)
        total = playing + stall_s
        if total <= 0:
            return None
        ratio = stall_s / total
        if ratio <= self.thresholds.rebuffer_ratio_threshold:
            return None
        return R.PLY_REBUFFER_RATIO.raise_finding(
            f"The player stalled for {stall_s:.1f} s across {len(stalls)} stall(s) in "
            f"{total:.1f} s of presentation time, a rebuffering ratio of {ratio:.3f} against a "
            f"threshold of {self.thresholds.rebuffer_ratio_threshold}.",
            evidence={
                "stall_count": len(stalls),
                "stall_s": stall_s,
                "playing_s": playing,
                "ratio": ratio,
            },
        )

    def _ladder_table(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for context in self.layers.values():
            if context.master is None:
                continue
            for variant in context.master.variants:
                history = context.histories.get(variant.variant_id)
                measured = history.last if history else None
                sps = (measured.sps if measured else None) or {}
                layout = context.layout.get(variant.variant_id) if context.layout else None
                rows.append(
                    {
                        "layer": context.layer.value,
                        "variant": variant.variant_id,
                        # Muxed or demuxed, and which rendition carries the audio. A reader
                        # of the ladder has to be able to tell a rung whose segments hold no
                        # audio by design from one that is missing it.
                        "layout": layout.as_dict() if layout else None,
                        "declared": {
                            "bandwidth": variant.bandwidth,
                            "average_bandwidth": variant.average_bandwidth,
                            "resolution": variant.resolution,
                            "frame_rate": variant.frame_rate,
                            "codecs": variant.codecs,
                            "video_range": variant.video_range,
                        },
                        "measured": {
                            "resolution": sps.get("resolution"),
                            "profile": sps.get("profile"),
                            "level": sps.get("level"),
                            "max_num_ref_frames": sps.get("max_num_ref_frames"),
                            "frame_rate": sps.get("frame_rate"),
                            "scan_type": sps.get("scan_type"),
                            "peak_kbps": (
                                (measured.measured_bitrate_bps or 0) / 1000 if measured else None
                            ),
                            "audio_codec": measured.audio_codec if measured else None,
                        },
                    }
                )
        return rows


def _added_duration(previous: MediaPlaylist, current: MediaPlaylist) -> float:
    known = {s.uri for s in previous.segments}
    return sum(s.duration for s in current.segments if s.uri not in known and s.duration > 0)


def _representative_target_duration(layers: dict[StreamLayer, LayerContext]) -> float:
    for context in layers.values():
        for value in context.target_duration_by_variant.values():
            if value:
                return value
    return 6.0


def _worst_ratio(
    vpb_results: dict[str, vpb.VpbResult], player_events: list[dict[str, Any]]
) -> tuple[str | None, float | None]:
    """The highest measured rebuffering ratio, and which rung produced it."""
    if not vpb_results:
        return None, None
    worst_key = max(vpb_results, key=lambda k: vpb_results[k].rebuffer_ratio)
    del player_events
    return vpb_results[worst_key].variant, vpb_results[worst_key].rebuffer_ratio


def _top_height(context: LayerContext) -> int | None:
    """The tallest rung the ladder offers, which decides the buffering profile."""
    if context.master is None:
        return None
    heights = [v.height for v in context.master.variants if v.height]
    return max(heights) if heights else None


def _vpb_mode(value: str | None) -> Any:
    from app.config import VpbMode

    if not value:
        return None
    try:
        return VpbMode(value.upper())
    except ValueError:
        return None


def _parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value / 1000.0, dt.UTC)
    try:
        moment = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def severity_counts(findings: list[Finding]) -> dict[str, int]:
    counts = {severity.value: 0 for severity in Severity}
    for finding in findings:
        counts[finding.severity.value] += 1
    return counts
