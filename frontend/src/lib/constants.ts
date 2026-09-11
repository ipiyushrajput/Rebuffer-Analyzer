/**
 * Shared constants.
 *
 * MSN_GAP_TOLERANCE lives here, not inline, because the Sequence Ladder chart, the findings
 * feed and the backend rule must all agree on when a spread stops being ordinary poll skew.
 */

/** Media sequence spread below this is ordinary poll skew across renditions. */
export const MSN_GAP_TOLERANCE = 5

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

/** Colour is never the only signal: every severity also carries an icon and a label. */
export const SEVERITY_STYLE: Record<
  Severity,
  { text: string; bg: string; border: string; icon: string; label: string }
> = {
  CRITICAL: {
    text: 'text-critical',
    bg: 'bg-red-50',
    border: 'border-critical',
    icon: '■',
    label: 'Critical',
  },
  ERROR: { text: 'text-error', bg: 'bg-orange-50', border: 'border-error', icon: '▲', label: 'Error' },
  WARN: { text: 'text-warn', bg: 'bg-amber-50', border: 'border-warn', icon: '◆', label: 'Warning' },
  INFO: { text: 'text-info', bg: 'bg-blue-50', border: 'border-info', icon: '●', label: 'Info' },
  PASS: { text: 'text-pass', bg: 'bg-green-50', border: 'border-pass', icon: '✓', label: 'Passed' },
}

export const OWNER_LABEL: Record<string, string> = {
  CONTENT_PROVIDER: 'Content provider',
  PACKAGER: 'Packager',
  CDN: 'CDN',
  SSAI: 'SSAI vendor',
  SAMSUNG_PLAYER: 'Samsung player / device team',
  NETWORK: 'Network',
}

export const CHECK_SETS = [
  { id: 'video_quality', label: 'Video quality detectors (black / freeze)' },
  { id: 'dolby_hdr', label: 'Dolby audio and HDR' },
  { id: 'captions', label: 'Captions (CEA-608 / 708)' },
  { id: 'scte35_inband', label: 'SCTE-35 in-band' },
  { id: 'subtitles', label: 'Subtitles' },
] as const

export const UA_PROFILES = [
  { id: 'tizen5', label: 'Tizen 5.0 (TV Plus default)' },
  { id: 'tizen4', label: 'Tizen 4.0' },
  { id: 'tizen6', label: 'Tizen 6.0' },
  { id: 'tizen7', label: 'Tizen 7.0' },
  { id: 'desktop', label: 'Desktop Chrome (for comparison)' },
] as const

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
  { id: 'STRICT', label: 'Strict — any time at zero buffer counts' },
  { id: 'NORMAL', label: 'Normal — summed outage above the threshold' },
  { id: 'OUTAGE_ONLY', label: 'Outage only — no segment downloadable' },
] as const

/** Player metrics come from the analyzer's network path, not a TV's. Say so everywhere. */
export const PLAYER_METRICS_NOTE = 'Measured from the analyzer host'
