/**
 * A request as a command a CDN or packager engineer can run themselves.
 *
 * The same shape `app/net/curl.py` builds on the backend: `-k` because the analyzer does not
 * verify TLS (it reports the chain instead), redirects not followed so each hop's headers are
 * printed, and the exact User-Agent the run went out as — a CDN answers per User-Agent.
 */

/** POSIX single-quoting: safe for any URL, token or User-Agent string. */
export function shellQuote(value: string): string {
  if (/^[A-Za-z0-9_\-./:=@%+,]+$/.test(value)) return value
  return `'${value.replace(/'/g, `'"'"'`)}'`
}

export function curlCommand(url: string, userAgent = ''): string {
  const parts = ['curl', '-sS', '-k', '-D', '-', '-o', '/dev/null']
  if (userAgent) parts.push('-A', userAgent)
  parts.push(url)
  return parts.map(shellQuote).join(' ')
}

/** The request an evidence sample measured, if it names one. */
export function requestUrlOf(record: Record<string, unknown>): string | null {
  for (const key of ['url', 'uri', 'final_url', 'segment_uri']) {
    const value = record[key]
    if (typeof value === 'string' && /^https?:\/\//.test(value)) return value
  }
  return null
}
