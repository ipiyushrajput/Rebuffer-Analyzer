/**
 * Shared constants.
 *
 * MSN_GAP_TOLERANCE lives here, not inline, because the Sequence Ladder chart, the findings
 * feed and the backend rule must all agree on when a spread stops being ordinary poll skew.
 */

/** Media sequence spread below this is ordinary poll skew across renditions. */
export const MSN_GAP_TOLERANCE = 5

/**
 * The statuses a job never leaves.
 *
 * Polling exists to watch something change. A job in one of these states will not change
 * again, so a tab showing only finished work asks the backend nothing until the operator
 * returns to it or a run completes and invalidates the query.
 */
export const TERMINAL_STATUSES = ['COMPLETED', 'FAILED', 'CANCELLED'] as const

export function isTerminal(status: string | undefined | null): boolean {
  return status != null && (TERMINAL_STATUSES as readonly string[]).includes(status)
}

/** How often a tab re-reads work that is actually in flight. */
export const LIVE_POLL_MS = 4000

export const REBUFFER_RATIO_THRESHOLD_DEFAULT = 0.25

export const SEVERITIES = ['CRITICAL', 'ERROR', 'WARN', 'INFO', 'PASS'] as const
export type Severity = (typeof SEVERITIES)[number]

export const SEVERITY_RANK: Record<Severity, number> = {
  CRITICAL: 4,
  ERROR: 3,
  WARN: 2,
  INFO: 1,
  PASS: 0,
}

/**
 * Severity runs along the brand spectrum — green clean, blue informational, purple warning,
 * pink error, pink filled critical. Colour is never the only signal: every severity also
 * carries its own word, and findings additionally carry a coloured rail on the card edge.
 */
export const SEVERITY_STYLE: Record<
  Severity,
  { chip: string; text: string; rail: string; dot: string; label: string }
> = {
  CRITICAL: {
    chip: 'border-pink-300 bg-pink-500 text-white',
    text: 'text-pink-600',
    rail: 'bg-pink-500',
    dot: '#FF2D55',
    label: 'Critical',
  },
  ERROR: {
    chip: 'border-pink-200 bg-pink-50 text-pink-600',
    text: 'text-pink-600',
    rail: 'bg-pink-300',
    dot: '#E01142',
    label: 'Error',
  },
  WARN: {
    chip: 'border-violet-200 bg-violet-50 text-violet-500',
    text: 'text-violet-500',
    rail: 'bg-violet-300',
    dot: '#7B2CBF',
    label: 'Warning',
  },
  INFO: {
    chip: 'border-brand-200 bg-brand-50 text-brand-600',
    text: 'text-brand-600',
    rail: 'bg-brand-300',
    dot: '#1428A0',
    label: 'Info',
  },
  PASS: {
    chip: 'border-clean-100 bg-clean-50 text-clean-600',
    text: 'text-clean-600',
    rail: 'bg-clean-500',
    dot: '#12864C',
    label: 'Passed',
  },
}

export const OWNER_LABEL: Record<string, string> = {
  CONTENT_PROVIDER: 'Content provider',
  PACKAGER: 'Packager',
  CDN: 'CDN',
  SSAI: 'SSAI vendor',
  SAMSUNG_PLAYER: 'Samsung player',
  NETWORK: 'Network',
}

export const CHECK_SETS = [
  { id: 'video_quality', label: 'Video quality detectors (black / freeze)' },
  { id: 'dolby_hdr', label: 'Dolby audio and HDR' },
  { id: 'captions', label: 'Captions (CEA-608 / 708)' },
  { id: 'scte35_inband', label: 'SCTE-35 in-band' },
  { id: 'subtitles', label: 'Subtitles' },
] as const

/**
 * The User-Agent profiles come from `GET /api/settings`, not from here.
 *
 * This file used to carry its own copy, and it drifted: it listed five profiles with labels
 * that no longer matched the strings the backend actually sent. One list, served by the side
 * that makes the requests.
 *
 * @see useUaProfiles in `src/lib/uaProfiles.ts`
 */

export const AGING_PRESETS = [
  { minutes: 15, label: '15 min' },
  { minutes: 30, label: '30 min' },
  { minutes: 60, label: '1 h' },
  { minutes: 180, label: '3 h' },
  { minutes: 360, label: '6 h' },
  { minutes: 720, label: '12 h' },
  { minutes: 1440, label: '24 h' },
] as const

export const VPB_MODES = [
  { id: 'STRICT', label: 'Strict — any time at zero buffer' },
  { id: 'NORMAL', label: 'Normal — summed outage' },
  { id: 'OUTAGE_ONLY', label: 'Outage only — no segment downloadable' },
] as const

/** Player metrics come from the analyzer's network path, not a TV's. Say so everywhere. */
export const PLAYER_METRICS_NOTE = 'Analyzer host'
