/**
 * WebSocket message types.
 *
 * The mirror of `backend/app/ws/schemas.py`. Both sides are kept in step by hand; a message
 * whose `type` this file does not know is ignored rather than rendered.
 */

export type ServerMessageType =
  | 'status'
  | 'playlist_snapshot'
  | 'segment_result'
  | 'metric'
  | 'player_event'
  | 'finding'
  | 'verdict'
  | 'event'
  | 'error'

export type StreamLayer = 'PLAYBACK' | 'ORIGIN' | 'CDN' | 'SSAI'

export interface ServerMessage<T = Record<string, unknown>> {
  type: ServerMessageType
  session_id: string
  ts: string
  layer?: StreamLayer | null
  variant_id?: string | null
  data: T
}

export interface FindingData {
  rule_id: string
  title: string
  layer: string
  stream_layer: StreamLayer
  severity: 'CRITICAL' | 'ERROR' | 'WARN' | 'INFO' | 'PASS'
  owner: string
  owner_label: string
  variant: string | null
  detail: string
  root_cause: string
  fix: string
  rebuffer_impact: 'direct' | 'indirect' | 'none'
  count: number
  first_seen: string
  last_seen: string
  evidence: Record<string, unknown>[]
  layer_presence: Record<string, boolean>
  reference: string
}

export interface PlaylistSnapshotData {
  at: string
  variant: string
  url: string
  status: number
  msn: number | null
  last_msn: number | null
  dsn: number | null
  segments: number
  window_s: number
  target_duration: number | null
  ttfb_ms: number | null
  total_ms: number
  bytes: number
  headers: Record<string, string>
}

export interface SegmentResultData {
  variant: string
  msn: number
  uri: string
  at: string
  status: number
  bytes: number
  download_ms: number
  ttfb_ms: number | null
  declared_duration: number | null
  actual_duration: number | null
  measured_kbps: number | null
  av_skew_ms: number | null
  container: string
  starts_with_keyframe: boolean | null
}

/**
 * What segment sampling has done, and — when it has done nothing — why.
 *
 * `reason` is empty whenever any segment has been measured. It is filled in only for a
 * session that sampled nothing, which is the case the screen could not otherwise explain.
 */
export interface SamplingState {
  segments_sampled: number
  reason: string
  by_variant: {
    variant: string
    at: string
    reason: string
    /** The most recent poll. */
    listed: number
    eligible: number
    fetched: number
    /** The life of the session. */
    polls: number
    total_fetched: number
  }[]
}

export interface VerdictData {
  status: string
  headline: string
  risk_score: number
  owner: string | null
  owner_label: string | null
  required_fix: string | null
  measured_rebuffer_ratio: number | null
  worst_variant: string | null
  incident_count: number
  incident_seconds: number
  counts: Record<string, number>
  window_seconds: number
  playlists_checked: number
  segments_checked: number
  primary: (FindingData & { rank_score: number; stall_correlations: number }) | null
  contributing: (FindingData & { rank_score: number })[]
  risk_formula: string
}

export interface EventData {
  kind: string
  [key: string]: unknown
}

/** Telemetry the player sends back on the same socket. */
export interface PlayerEventOut {
  event:
    | 'level_switched'
    | 'frag_loaded'
    | 'buffer'
    | 'stall_start'
    | 'stall_end'
    | 'error'
    | 'dropped_frames'
    | 'startup'
    /*
     * The licence lifecycle in the browser: the key session opening, a licence loading, the
     * first fragment decrypting, or the page having no secure context to do EME in at all.
     * `details` names which. A DRM failure is then evidence in the run rather than a console
     * line nobody kept.
     */
    | 'drm'
  ts?: string
  variant?: string
  level?: number
  /** The rendition's own bitrate, sent only when the player switches rung. */
  bitrate?: number
  /**
   * What the player measured the network doing, in bits per second. A different quantity
   * from `bitrate`: on a small segment off a nearby CDN hls.js estimates in gigabits, which
   * is not a rung anyone is playing.
   */
  bandwidth_bps?: number
  buffer_s?: number
  stall_duration_s?: number
  dropped?: number
  total_frames?: number
  error_type?: string
  details?: string
  fatal?: boolean
  startup_ms?: number
}

export interface ClientMessage {
  type: 'player_event' | 'ping' | 'subscribe'
  data: Record<string, unknown>
}
