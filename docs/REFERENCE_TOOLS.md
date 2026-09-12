# Reference tools — coverage matrix

Five public stream analysers were studied before RBA's rule catalogue was written. This
document maps every capability each one exposes onto the RBA module or rule that covers it,
so a reader who already uses one of those tools can find the equivalent check here.

Behaviour and mechanisms were replicated. No code, text, or branding was copied.

Sources used: the tools' own documentation pages, their published error catalogues, and a
public test stream run through each. Rule identifiers below are declared in
`backend/app/analysis/rules/catalogue.py` and listed in [RULES.md](RULES.md).

---

## 1. HLSAnalyzer.com

Cloud HLS/DASH validator and 24×7 monitor. Downloads every playlist and every segment and
logs delivery characteristics.

### Error and warning catalogue

| Their code | Their check | RBA rule | Notes |
|---|---|---|---|
| EC-1000 | Media playlist has become a master playlist | `MED-020`, `MST-021` | Raised on the media playlist and on the master re-poll |
| EC-1001 | Unusually large jump in media playlist progression | `SEQ-003` | |
| EC-1002 | Media sequence wraparound | `SEQ-005` | |
| EC-1003 | Negatively increasing media sequence | `SEQ-002`, `CDN-008` | RBA splits the packager case from the stale-edge case |
| EC-1004 | Extra player buffer accumulation | `MED-015`, `VPB-003` | |
| EC-1005 | Negative segment duration | `MED-007` | |
| EC-1006 | Segment duration > 150 % of target duration | `MED-005` | Threshold `segment_extinf_max_ratio_to_td` |
| EC-1007 | Unusually large segment duration (> 120 s) | `MED-006` | Threshold `segment_extinf_absolute_max_s` |
| EC-1008 | Invalid target or segment duration | `MED-001`, `MED-007` | |
| EC-2000 | BANDWIDTH not specified | `MST-006` | |
| EC-2001 | Measured peak bitrate exceeds specified bitrate | `MST-007`, `MST-008`, `SEG-015`, `CDN-007` | RBA reports rolling peak, mean, and per-segment peak separately |
| EC-2002 | Could not download the segment | `SEG-018`, `HTTP-005` | |
| EC-2003 | Playlist and stream mismatch | `SEG-007`, `MST-025` | |
| EC-2004 | Could not download the playlist | `MED-021`, `MST-014` | |
| EC-2005 | Additional HLS conformance errors | `MST-001`…`MST-005`, `MED-*` | RBA names the specific conformance rule instead of one bucket |
| WA-1001 | Rebuffering error | `VPB-001`, `VPB-002`, `PLY-001` | |
| WA-1002 | Slow segment download time | `SEG-017`, `CDN-006` | |
| WA-1003 | Media sequence number increment mismatch | `SEQ-001` | |
| WA-2000 | Master playlist feature not supported | `MST-002`, `MST-028` | |

### Mechanisms

| Capability | RBA module / rule |
|---|---|
| Virtual player buffer: segments arriving slower than real time drain a hypothetical buffer; zero is a rebuffer | `analysis/vpb.py`, `VPB-001`…`VPB-004` |
| Three alert sensitivity modes (any-zero / summed outage / total outage) | `Thresholds.vpb_mode` = `STRICT` \| `NORMAL` \| `OUTAGE_ONLY` |
| Media playlist state machine: Unknown → Live / Stalled / HTTPError / LiveEnd / VOD | `analysis/rules/media_playlist.py` `PlaylistStateMachine`, `MED-014` |
| Master playlist re-polled every 20 s | `collectors/master_poller.py`, `master_repoll_interval_s`, `MST-020` |
| Outage / clear hysteresis before an incident opens and closes | `analysis/correlate.py` `IncidentTracker`, `incident_open_s` / `incident_clear_s` |
| SCTE-35 extraction from TS and from playlist tags, with cue-duration summaries | `hls/scte35.py`, `analysis/rules/ads.py`, `ADS-007`…`ADS-009` |
| CEA-608/708 caption extraction | `analysis/rules/subtitles.py`, `SUB-006`, `SUB-007` |
| PAT / PMT / PID inspection | `media/ts.py`, `SEG-004` |
| CSV batch import | `bulk/parsers.py`, Bulk tab |
| Per-stream estimated playback vs outage time | Report §2 executive verdict, `VPB-004` |
| Downloads every playlist and every segment | `collectors/playlist_poller.py`, `collectors/segment_sampler.py` |

---

## 2. Qosifire (Softvelum)

An agent that behaves like a playback client against HLS (TS, fMP4/CMAF, audio-only).

| Their capability | RBA module / rule |
|---|---|
| Per-rendition buffer: content added on download, consumed at playback speed | `analysis/vpb.py`, one `VirtualPlayerBuffer` per sampled rung |
| "Buffer too short" | `VPB-002` |
| "Buffer too long" | `VPB-003`, `MED-015` |
| "Hostname resolved" / "Failed to resolve" | `net/dns.py`, `NET-900`, `NET-002` |
| "Redirect" | `net/fetcher.py` hop log, `HTTP-001`, `HTTP-003` |
| "Connected to host" / "Failed to connect" | `net/fetcher.py` timing split, event feed |
| "Bad playlist" / "Bad chunklist" | `MED-021`, `MST-014` |
| "Request timeout" (5 s) | `HTTP-011`, `Thresholds.request_timeout_s` |
| "Gap in chunklist" | `MED-012`, `SEQ-003` |
| "Wrong media sequence" (newer chunklist carries an older MSN) | `CDN-008`, `SEQ-002` |
| "Chunklist params changed" (a chunk's URL changed for the same position) | `MED-016` |
| "Bad chunk" | `SEG-001`, `SEG-006`, `SEG-018` |
| "Bad init segment" | `SEG-013`, `MED-009` |
| Stream status Idle / Syncing / Online / Offline | Session status pill: `IDLE` / `RESOLVING` / `RUNNING` / `STOPPED` |
| Event feed, reverse chronological, with a Details drawer | Realtime tab → Event feed panel |
| Flow view: every chunklist and chunk download per rendition, click any moment to see the chunklist as it was | `playlist_snapshots` table + `GET /api/jobs/{id}/snapshots?variant=&at=` (time travel) |
| Freeze the live chunklist view | Manifest viewer pause/resume — display freezes, analysis continues |
| Downloads Gantt per rendition | Realtime chart 13 |
| Traffic (bytes/s) and bandwidth charts | Realtime chart 14 |
| Audio vs video PTS delta per chunk, red above 1 s | `AV-005`, `av_pts_delta_critical_ms`, Realtime chart 10 |
| Configurable User-Agent per stream | `Thresholds`-adjacent `ua_profile` job option, `config.USER_AGENT_PROFILES` |

---

## 3. Dolby Stream Validator

Conformance validator for Dolby Digital Plus (E-AC-3), AC-4, and Dolby Vision in HLS/DASH.

| Their capability | RBA module / rule |
|---|---|
| Cross-level conformance: manifest vs container vs elementary stream | `MST-025`, `analysis/rules/master.py` `cross_level_check` |
| Dolby codec declared vs carried | `MST-022`, `AUD-008` |
| CHANNELS attribute vs actual channel count | `MST-023` |
| VIDEO-RANGE / SUPPLEMENTAL-CODECS vs bitstream metadata | `MST-024` |
| A/V segment alignment (AC-4) | `AUD-009`, `VID-007` |
| Baseline vs optional check sets, selectable | Job option `check_sets`: baseline plus `video_quality`, `dolby_hdr`, `captions`, `scte35_inband`, `subtitles` |
| Duration limit for live streams | Aging duration presets and Realtime stop |
| Select streams — validate a subset of renditions | Job option `renditions` |
| Record — save referenced segments as a ZIP | Job option `record_evidence`, `GET /api/jobs/{id}/evidence.zip` |
| Demux — elementary streams ZIP | Evidence bundle includes per-track elementary payloads when `record_evidence` is on |
| Clear-key KID/KEY input for encrypted content | Job option `clear_keys` (memory only, never persisted or printed), `SEG-016`, `MST-018` |
| Summary tab plus per-rendition detail tabs with media info | Report §2 and §7; Realtime bottom-panel Ladder table |
| Green/red progress per rendition | Bulk progress table and Aging job list |

---

## 4. Akamai Stream Validator

Validates an HLS stream against the HLS standard, alongside reference hls.js / dash.js
players.

| Their capability | RBA module / rule |
|---|---|
| RFC 8216 conformance | `MST-001`, `MST-002`, `MED-001`, `MED-007`, `MED-011` |
| Apple HLS Authoring Specification rule set | `MST-028`, `MED-002`, `MED-003`, `MST-010`…`MST-013` |
| Reference-player check: play the stream and report player events next to validation results | Realtime tab hls.js player via `/api/proxy`, `PLY-001`…`PLY-006` |
| Level switches, buffer health, dropped frames, fatal errors | `samples_player` table, Realtime charts 1–3, `PLY-002`, `PLY-004`, `PLY-006` |

---

## 5. THEOplayer Inspect Stream

Browser-based inspector with plain-language error cards.

| Their capability | RBA module / rule |
|---|---|
| CORS check (`Access-Control-Allow-Origin` missing) | `CDN-005` |
| Segment decryption failure | `SEG-016` |
| Non-2xx/3xx download failures | `HTTP-004`…`HTTP-008`, `SEG-018` |
| Live playlist without ENDLIST must span ≥ 3 × TARGETDURATION | `MED-003` |
| Segment size exceeds declared BANDWIDTH | `SEG-015` |
| PAT/PMT present in every TS segment | `SEG-004` |
| Packed/raw audio must carry an ID3 timestamp | `SEG-014` |
| Titled error card with a one-line explanation and a fix | `FindingCard` component and the report findings table: every finding renders title, evidence, root cause, fix |

---

## What RBA adds beyond all five

| Capability | Module / rule |
|---|---|
| Tizen-specific decoder rules: cross-rung `max_num_ref_frames` mismatch forcing a DPB realloc | `VID-006` |
| Cross-variant discontinuity mismatch treated as CRITICAL for Tizen's shared counter | `SEQ-009`, `SEQ-011`, `ADS-004` |
| AAAA published without IPv6 transit, reported separately from A-record reachability | `NET-001` |
| First-listed rung is the startup rung on Tizen | `MST-010` |
| Legacy Tizen cipher availability at the edge | `TLS-004` |
| Deterministic owner attribution across ORIGIN → CDN → SSAI with a layer diff | `analysis/attribution.py` |
| Stall-to-cause correlation: every player and simulated stall names the event that caused it | `analysis/correlate.py` |
| SSAI ad-splice conformance across the whole ladder | `ADS-001`…`ADS-010` |
| Full SPS/DPB cross-rung analysis from a real exp-Golomb decoder | `media/h264_sps.py`, `media/hevc_sps.py`, `VID-006` |
| Escalation-ready per-owner report blocks | `reports/email_block.py`, report §3 |
| Rebuffer Risk Score with a documented formula | `analysis/verdict.py`, report appendix |

---

## Out of scope, with the reason

| Capability | Reason |
|---|---|
| DASH/MPD validation (HLSAnalyzer EC-4xxx, EC-5xxx, EC-6xxx) | TV Plus linear channels on the Tizen player are delivered as HLS. The rule model is container-agnostic, so DASH rules can be added to the same registry when a DASH channel enters the fleet. |
| Alert delivery by email or HTTP callback | RBA produces a report for forwarding; alert routing is handled by the team's existing monitoring. |
| 30-day caption and SCTE-35 archival as a product feature | Captions and cues are recorded per job and retained under `sample_retention_days`; they are not offered as a standalone archive. |
| Subscription, quota and API-key errors | RBA is an internal tool with no subscription tier. |
| Dolby Vision profile-by-profile certification | RBA reports the declared vs carried Dolby Vision profile (`MST-024`). Full certification stays with the Dolby validator. |
