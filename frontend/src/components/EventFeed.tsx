/**
 * Event feed, newest first, with a details drawer — the Qosifire view.
 *
 * Every network step is listed: hostname resolved, redirect, connected, bad playlist,
 * timeout, wrong media sequence, and the proxy's own fetch timings.
 */

import { useState } from 'react'
import { localTime, ms, utcTitle } from '../lib/format'
import type { EventData } from '../ws/messages'

const KIND_LABEL: Record<string, string> = {
  dns: 'Hostname resolved',
  tls: 'TLS negotiated',
  resolve: 'Playback URL resolved',
  master: 'Master playlist parsed',
  playlist_state: 'Playlist state changed',
  proxy_fetch: 'Proxy fetch',
}

const KIND_TONE: Record<string, string> = {
  dns: 'border-pass bg-green-50 text-pass',
  tls: 'border-pass bg-green-50 text-pass',
  resolve: 'border-info bg-blue-50 text-info',
  master: 'border-info bg-blue-50 text-info',
  playlist_state: 'border-warn bg-amber-50 text-warn',
  proxy_fetch: 'border-slate-200 bg-slate-50 text-[var(--rba-muted)]',
}

function summarise(event: EventData): string {
  const data = (event.data ?? {}) as Record<string, unknown>
  switch (event.kind) {
    case 'dns':
      return `${data.host} → ${(data.a as string[] | undefined)?.join(', ') || 'no A record'} in ${ms(
        data.resolve_ms as number,
      )}`
    case 'tls':
      return `${data.host} · ${data.version} · ${data.cipher}`
    case 'resolve':
      return `HTTP ${data.status} after ${(data.hops as unknown[] | undefined)?.length ?? 1} hop(s) · ${ms(
        (data.timings as Record<string, number> | undefined)?.ttfb_ms,
      )} to first byte`
    case 'master':
      return `${(data.variants as unknown[] | undefined)?.length ?? 0} rung(s), ${
        (data.renditions as unknown[] | undefined)?.length ?? 0
      } rendition(s)`
    case 'playlist_state':
      return `${event.variant ?? ''} ${data.from} → ${data.to}`
    case 'proxy_fetch':
      return `${data.host} · HTTP ${data.status} · ${ms(data.ttfb_ms as number)}`
    default:
      return JSON.stringify(data).slice(0, 160)
  }
}

export function EventFeed({ events }: { events: (EventData & { ts: string })[] }) {
  const [openIndex, setOpenIndex] = useState<number | null>(null)

  if (events.length === 0) {
    return (
      <p className="px-4 py-6 text-sm text-[var(--rba-muted)]">
        Network events appear here as the analysis resolves and fetches each layer.
      </p>
    )
  }

  return (
    <ul className="divide-y divide-[var(--rba-line)]">
      {events.map((event, index) => (
        <li key={`${event.ts}-${index}`} className="px-4 py-2">
          <div className="flex items-start gap-3">
            <span
              className="w-16 shrink-0 font-mono text-[11px] text-[var(--rba-muted)]"
              title={utcTitle(event.ts)}
            >
              {localTime(event.ts)}
            </span>
            <span className={`chip shrink-0 ${KIND_TONE[event.kind] ?? 'border-slate-200 bg-slate-50'}`}>
              {KIND_LABEL[event.kind] ?? event.kind}
            </span>
            <span className="min-w-0 flex-1 truncate text-sm">{summarise(event)}</span>
            <button
              type="button"
              className="btn-secondary shrink-0 !px-2 !py-1 text-xs"
              onClick={() => setOpenIndex(openIndex === index ? null : index)}
              aria-expanded={openIndex === index}
            >
              Details
            </button>
          </div>
          {openIndex === index && (
            <pre className="manifest-pane mono mt-2 max-h-56 rounded border border-[var(--rba-line)] bg-slate-50 p-2">
              {JSON.stringify(event, null, 2)}
            </pre>
          )}
        </li>
      ))}
    </ul>
  )
}
