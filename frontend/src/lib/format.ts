/** Formatting helpers. Timestamps show local time with UTC on hover (§8.4). */

export function localTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleTimeString([], { hour12: false })
}

export function localDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString([], { hour12: false })
}

export function utcTitle(iso: string | null | undefined): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return `${date.toISOString()} (UTC)`
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  if (minutes < 60) return `${minutes}m ${rest}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

export function kbps(bitsPerSecond: number | null | undefined): string {
  if (!bitsPerSecond) return '—'
  if (bitsPerSecond >= 1_000_000) return `${(bitsPerSecond / 1_000_000).toFixed(2)} Mbit/s`
  return `${Math.round(bitsPerSecond / 1000)} kbit/s`
}

export function bytes(value: number | null | undefined): string {
  if (value == null) return '—'
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(2)} MB`
  if (value >= 1024) return `${(value / 1024).toFixed(1)} kB`
  return `${value} B`
}

export function ratio(value: number | null | undefined, digits = 3): string {
  if (value == null) return '—'
  return value.toFixed(digits)
}

export function ms(value: number | null | undefined): string {
  if (value == null) return '—'
  return `${Math.round(value)} ms`
}

/** Mask long token values for display. The report appendix carries the full URL. */
export function maskUrl(url: string): string {
  try {
    const parsed = new URL(url)
    const sensitive = ['hdnts', 'hdnea', 'token', 'auth', 'sig', 'signature', 'aws.sessionid']
    parsed.searchParams.forEach((value, key) => {
      if (value.length > 12 && sensitive.includes(key.toLowerCase())) {
        parsed.searchParams.set(key, `${value.slice(0, 4)}…${value.slice(-4)}`)
      }
    })
    return parsed.toString()
  } catch {
    return url
  }
}
