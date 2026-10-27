# False-positive audit

Every rule in `docs/RULES.md` was reviewed for one question: **can it fire on a healthy
stream because of how the analyzer measures**, rather than because of the stream? The
answers fall into three groups:

1. **Fixed.** The analyzer's own behaviour produced the finding. The cause and the fix are
   listed, and each fix carries a test that reproduces the old false positive and a test
   that the real defect still fires.
2. **Soft cases.** The rule measures correctly, but what it measures depends on where the
   analyzer runs or on a product policy. Each one names the condition and the recommended
   handling. None was changed in this round, because each needs a decision from the team.
3. **Not affected.** The rule reads a structural fact of one response (a tag, a header, a
   box, a status) that does not depend on timing, sampling or the analyzer host.

---

## 1. Fixed

### Polling and cross-variant comparison

| Rule | What fired on a healthy stream | Cause in the analyzer | Fix |
|---|---|---|---|
| SEQ-008 MSN spread across variants | Spread of a few segments between rungs | Each rendition was polled on its own loop, and the cross-variant check ran after *every* poll using the other renditions' last snapshots, which were seconds old. Segment sampling ran inside each loop, so the loops drifted further apart. | `LadderPoller` requests every rendition of a layer in the same **round**. The cross-variant checks run once per round, on that round's snapshots only. A finding is recorded only after `cross_variant_confirm_rounds` (default 2) consecutive rounds. Evidence carries `round`, `confirmed_rounds` and `round_spread_ms`. |
| SEQ-009 DSN differs across variants | The reported case: `polled_at` 06:51:06.5 to 06:51:09.3, DSN 65388 vs 65389 | Same as above. | Same as above. |
| SEQ-011 discontinuity positions | Tags at different positions while rungs had slid differently | Same as above. | Same as above. |
| AV-003 audio/video DSN | Raised together with SEQ-009 | Same as above. | Same as above. |
| SEQ-012 PDT across variants | A difference of exactly one segment duration | Compared each rendition's *first* segment PDT, so a window that had slid by one segment differed by a whole segment. | PDT is compared at one MSN every rendition lists, carried forward from the last PDT tag, against `cross_variant_pdt_tolerance_s`. |
| SEQ-010 durations differ for the same MSN | 6.000 vs 6.006 s | Exact equality was required. | Within `cross_variant_duration_tolerance_s` (default 0.1 s). |
| SEQ-007 DSN advanced by more than one | A break with two discontinuities rolling off between polls | Only the header delta was checked. | Allows the discontinuity tags that left the window (and one folded into the counter from the new first segment). Fires only when the windows overlap and the delta exceeds that. |
| SEQ-003 MSN jumped beyond the window | After a late poll | The time between the polls was not considered. | A jump that `elapsed / target duration + 1` explains is a late poll, not a jump. |
| SEQ-001 MSN delta ≠ segments rolled off | Every poll on a CDN that re-signs URIs | Segments were matched by full URI including the per-response token. | Matched by path (`app/hls/uri.py::uri_path`). |
| MED-016 URI changed for the same MSN | Every poll on a CDN that re-signs URIs | Same as above. | Same as above. |
| MED-015 published faster than real time | Nearly every refresh | One 6 s segment landing between two polls 3 s apart reads as 2× real time. | Measured over a window of at least 3 target durations, with one segment of quantisation allowed. |

### Sampling, buffer model and timing

| Rule | What fired | Cause | Fix |
|---|---|---|---|
| VPB-001 / VPB-002 / VPB-004 on a middle rung | Simulated rebuffering on ladders with more than 3 rungs | Rungs sampled every Nth segment fed the buffer model a third of their media. | The model plays back only renditions sampled in full, video or audio. Subtitles and Nth-sampled rungs are not modelled. |
| VPB-001 / VPB-002 / VPB-004 on long aging runs | A long stall at the end of a run | After the 5,000-segment sample budget was spent (about 2 h on a 4-rendition ladder), the model drained until the run ended. | The model ends at the moment the budget ran out. |
| VPB-003 buffer exceeded the maximum | At the start of every run | The first poll fetches the whole live window at once; a player joining at the live edge never requests it. | The first poll's backlog is capped silently, and only later deliveries count as overfill. |
| VID-007 keyframes misaligned across rungs | Any ladder with an Nth-sampled rung | A sampled MSN on one rung was compared with an unsampled MSN on another. | Compared only at MSNs every rung sampled and read (`RungHistory.video_msns`). |
| SEG-007 measured vs declared duration | TS segments with B-frames, and packed audio | Duration was the last PES's PTS minus the first, missing the last frame and the B-frame reorder (67–133 ms short on a correct segment). | Duration is the smallest-to-largest PTS span plus the typical frame interval. fMP4 already summed sample durations. |
| SEG-005 tiny segment | Every WebVTT segment, and low-bitrate audio | The size threshold is for video. | Not applied to WebVTT or audio-only renditions. |

### Demuxed and protected channels

| Rule | What fired | Cause | Fix |
|---|---|---|---|
| AUD-007 A/V durations drift apart | DRM/CMAF channels in step | Summed every sampled video duration against every sampled audio duration. The two are sampled independently: Nth sampling, a 200-entry index trim and per-track decrypt failures change one total and not the other. No muxing happens in analysis; pairs are matched by sequence number. | Drift is the change in A/V skew across a run of matched pairs (split at timestamp resets), against `av_drift_min_s` / `av_drift_fraction`. |
| AUD-006 no audio covers a video segment | The newest video segment at the end of a run; DRM audio that was not decrypted | The coverage check included video still waiting for its audio, video from before audio sampling began, and audio fetched but not decrypted. | All three are excluded; a missing (404) audio segment still fires. |
| SUB-001 WebVTT does not parse | Every subtitle segment on a DRM channel | The subtitle rendition inherited the ladder's "encrypted" flag, so its text was never read and the check ran on `''`. INFO-001 also fired for a track with no encryption. | A body carrying the `WEBVTT` signature is clear text: never decrypted, always read. The check runs only on text that was read. |

### HTTP and network

| Rule | What fired | Cause | Fix |
|---|---|---|---|
| HTTP-002 redirect drops query parameters | Every CDN redirect to a self-signed edge URL | Fired whenever a key was dropped, even when the redirected request succeeded. | Fires only when the redirected request is refused. |
| HTTP-003 redirect changes host | Every redirected segment | The rule is about child URIs resolving against the final host; a segment has none. | Playlists only. |
| HTTP-010 body shorter than Content-Length | Every gzip-encoded playlist | Content-Length (encoded size) was compared with the *decoded* body. | Compared with the bytes on the wire (`FetchResult.wire_bytes`), and only when fewer arrived. |
| NET-001 AAAA without IPv6 transit | Every dual-stack host, from an analyzer without IPv6 | The probe ran from a host with no IPv6 route. | The IPv6 probe runs only when the host has an IPv6 route; otherwise the result states that it was not measured. |
| ADS-005 delivered break ≠ declared | Every correctly signalled break | `EXT-X-CUE-IN` precedes the first segment *after* the break, and that segment was counted (+1 segment). | The CUE-IN segment is not counted. |
| ADS-010 CUE-OUT without CUE-IN | Every break still running at the live edge | An open break was reported as unbalanced. | Fires only once the open break has delivered more than its declared duration plus one segment. |
| ADS-006 ad segment over target duration | 6.006 s under `TARGETDURATION:6` | Raw EXTINF was compared with the integer target. | RFC 8216: the rounded EXTINF is compared. The CUE-IN segment is no longer counted as part of the break. |

**Segment redirects (verified, not a defect).** Segments are followed through 301/302/303/307/308
manually:
- Every hop is recorded.
- A relative `Location` resolves against its own hop.
- The `Range` header travels with every hop.
- The bytes analysed are the final response's.

`tests/test_segment_redirects.py` covers a segment moved to another path and another host,
and a byte-range segment.

---

## 2. Soft cases: correct measurement, but host- or policy-dependent

| Rule(s) | Condition under which it reads as a stream defect | Recommendation |
|---|---|---|
| CDN-006, SEG-017 download time > duration; CDN-007 throughput below BANDWIDTH; HTTP-012 TTFB; HTTP-011 timeout; NET-004 DNS latency; ADS-001 ads slower | Measured from the analyzer host. With up to `RBA_MAX_CONCURRENT_JOBS` (20) runs sharing the host's downlink, the host's own bandwidth is the bottleneck. | Keep concurrency in line with the host's bandwidth. INFO-007 reports when the analyzer itself fell behind. A host-bandwidth meter that suppresses delivery findings while the host is saturated is the next step. |
| MED-004 stale playlist; CDN-002 Age > TD | A late poll reads as a stale playlist. | INFO-007 now reports analyzer lateness. Consider gating MED-004 on the self-check. |
| CDN-005 Access-Control-Allow-Origin absent | Matters only for browser players; Tizen's native player does not use CORS. | Downgrade to INFO, or apply it only to channels distributed to web. |
| MST-026 frame rate other than 29.97/30 | 25/50 fps is correct in PAL regions. | Make the allowed frame rates a setting per country. |
| MST-027 HEVC, VID-012 interlaced, MST-019 no I-frame playlist, MST-028 no INDEPENDENT-SEGMENTS | Product policy, not a defect of the stream. | Keep as WARN/INFO, or reassign severity in Settings → Rule catalogue. |
| MED-017 no PROGRAM-DATE-TIME | Audio and subtitle playlists commonly omit it. | Apply to video renditions only. |
| SUB-002 no X-TIMESTAMP-MAP | RFC 8216 allows it to be absent: cues then map to timestamp 0. | Fire only when the media timeline does not start near 0. |
| SUB-004 overlapping cues | Overlapping cues are valid WebVTT (two speakers at once). | Fire only on duplicate cues with the same text. |
| VID-010 black frames, VID-011 frozen frames, AUD-005 silence | Slates, static graphics and silent programming. | Require a minimum duration, and exclude ad slates. |
| NET-001 (residual) | A host that has an IPv6 route but whose firewall drops IPv6 egress. | Add a setting that disables IPv6 probing on such hosts. |
| CDN-004, CDN-008, SEQ-002 MSN windows differ / go backwards | DNS round-robin across CDN edges. This is a real CDN inconsistency a player also sees, but it depends on which edge the analyzer reached. | Keep. The evidence carries the CDN headers that name the edge. |
| PLY-001 to PLY-006 | Reported by the browser preview player, on the operator's network, not by a Tizen device. | Read them as the operator's playback, not the device's. |
| VPB-* (residual) | Delivery timing is measured from the analyzer host (see the first row). | As in the first row. |
| SEG-016 decryption failed | Declared but never raised: a decrypt that fails is reported through INFO-001 with its reason. | Raise it only for a key that was obtained and does not decrypt. |
| VID-001 (false *negative*) | For fMP4, "starts with a keyframe" is inferred from the init segment or `styp`, not read from the first sample's flags. | Read the first sample's `sample_is_non_sync_sample` flag from `trun`/`tfhd`. |

---

## 3. Not affected

These rules read one response's structure (tags, attributes, headers, boxes, status codes)
and do not depend on poll timing, sampling coverage or the analyzer host:

- **Master:** MST-001 to MST-006, MST-009, MST-010 to MST-018, MST-020 to MST-025, MST-029. MST-020 compares declared attributes, never URIs.
- **Media playlist:** MED-001, MED-002, MED-003, MED-005 to MED-014, MED-018 to MED-021.
- **Segment:** SEG-001 to SEG-004, SEG-006, SEG-008 to SEG-015, SEG-018. SEG-008 to SEG-011 are already gated on consecutive MSNs, and INFO-005 reports skipped boundaries.
- **Audio:** AUD-001 to AUD-004, AUD-008 to AUD-010.
- **A/V:** AV-001, AV-002, AV-004, AV-005. On demuxed rungs they run on sequence-matched pairs (INFO-006 when matched by time).
- **Video:** VID-002 to VID-006, VID-008, VID-009.
- **TLS and HTTP status:** TLS-001 to TLS-005, NET-002, NET-003, HTTP-004 to HTTP-009, HTTP-013 to HTTP-015.
- **Ads and subtitles:** ADS-002, ADS-003, ADS-007, ADS-008, ADS-009; SUB-003, SUB-005, SUB-006, SUB-007.
- **Coverage statements:** INFO-001 to INFO-008 and every `*-900` PASS rule.
