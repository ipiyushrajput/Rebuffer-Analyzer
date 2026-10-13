/**
 * What happened, in order: polls, segment fetches, player events, findings and incidents.
 *
 * A stall is the last link of a chain — a playlist that stopped advancing, a segment that came
 * back 404, a buffer that ran dry — and each link lives in its own table. This reads them for a
 * window and lists them as one sequence, so the incident reads the way the player lived it.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { api } from '../api/client'
import { localTime, utcTitle } from '../lib/format'
import { Card, CardHeader, EmptyState, InlineAlert, SegmentedControl, cx } from './ui'

export interface TimelineEvent {
  at: string
  kind: 'poll' | 'segment' | 'player' | 'finding' | 'incident_start' | 'incident_end'
  layer?: string | null
  variant?: string | null
  status?: number | null
  ok?: boolean
  ms?: number | null
  seq?: number | null
  msn?: number | null
  severity?: string | null
  summary: string
  detail?: string | null
}

const KIND_LABEL: Record<TimelineEvent['kind'], string> = {
  poll: 'Poll',
  segment: 'Segment',
  player: 'Player',
  finding: 'Finding',
  incident_start: 'Incident',
  incident_end: 'Incident',
}

const KIND_TONE: Record<TimelineEvent['kind'], string> = {
  poll: 'chip-blue',
  segment: 'chip-neutral',
  player: 'chip-violet',
  finding: 'chip-pink',
  incident_start: 'chip-pink',
  incident_end: 'chip-neutral',
}

type Filter = 'all' | 'problems'

/** Only what went wrong, and the incident edges around it. */
export function problemsOnly(events: TimelineEvent[]): TimelineEvent[] {
  return events.filter(
    (event) =>
      event.kind === 'finding' ||
      event.kind === 'incident_start' ||
      event.kind === 'incident_end' ||
      event.ok === false,
  )
}

export function Timeline({
  jobId,
  start,
  end,
  variant,
  title = 'Timeline',
}: {
  jobId: string
  start: string
  end: string
  variant?: string | null
  title?: string
}) {
  const [filter, setFilter] = useState<Filter>('all')
  const [scope, setScope] = useState<'variant' | 'ladder'>(variant ? 'variant' : 'ladder')
  const params = new URLSearchParams({ start, end })
  if (scope === 'variant' && variant) params.set('variant', variant)
  const { data, error, isLoading } = useQuery({
    queryKey: ['timeline', jobId, start, end, scope, variant],
    queryFn: () =>
      api.get<{ events: TimelineEvent[]; truncated: string[] }>(
        `/jobs/${jobId}/timeline?${params.toString()}`,
      ),
    staleTime: 30_000,
  })
  const events = useMemo(
    () => (filter === 'problems' ? problemsOnly(data?.events ?? []) : (data?.events ?? [])),
    [data, filter],
  )

  return (
    <Card>
      <CardHeader
        title={title}
        subtitle={
          <span className="font-mono">
            {localTime(start)} → {localTime(end)}
          </span>
        }
        actions={
          <div className="flex flex-wrap gap-1.5">
            {variant && (
              <SegmentedControl
                options={[
                  { value: 'variant', label: variant },
                  { value: 'ladder', label: 'Every rendition' },
                ]}
                value={scope}
                onChange={(next) => setScope(next as 'variant' | 'ladder')}
                ariaLabel="Timeline scope"
              />
            )}
            <SegmentedControl
              options={[
                { value: 'all', label: 'Everything' },
                { value: 'problems', label: 'Problems only' },
              ]}
              value={filter}
              onChange={(next) => setFilter(next as Filter)}
              ariaLabel="Timeline filter"
            />
          </div>
        }
      />
      <div className="px-5 pb-5">
        {isLoading && <EmptyState title="Loading…" />}
        {error && (
          <InlineAlert tone="warn">{error instanceof Error ? error.message : String(error)}</InlineAlert>
        )}
        {data && data.truncated.length > 0 && (
          <InlineAlert tone="info">
            Cut short at the row limit: {data.truncated.join(', ')}. Narrow the window to see all of it.
          </InlineAlert>
        )}
        {data && events.length === 0 && <EmptyState title="Nothing recorded in this window" />}
        {events.length > 0 && (
          <ol className="max-h-[520px] overflow-auto rounded-tile border border-surface-line">
            {events.map((event, index) => (
              <li
                key={`${event.at}-${index}`}
                className={cx(
                  'grid grid-cols-[92px_88px_minmax(0,1fr)] items-start gap-3 border-b border-surface-line px-3 py-1.5 last:border-b-0',
                  event.ok === false && event.kind !== 'incident_end' && 'bg-pink-50',
                )}
              >
                <span className="font-mono text-micro text-ink-muted" title={utcTitle(event.at)}>
                  {localTime(event.at)}
                </span>
                <span className={cx(KIND_TONE[event.kind], 'justify-self-start')}>
                  {KIND_LABEL[event.kind]}
                </span>
                <span className="min-w-0">
                  <span
                    className={cx(
                      'block break-words text-small',
                      event.ok === false ? 'font-semibold text-pink-600' : 'text-ink-soft',
                    )}
                  >
                    {event.summary}
                    {event.ms != null && (
                      <span className="font-mono text-micro text-ink-faint"> · {Math.round(event.ms)} ms</span>
                    )}
                  </span>
                  {event.detail && (
                    <span className="block break-all font-mono text-micro text-ink-faint">{event.detail}</span>
                  )}
                </span>
              </li>
            ))}
          </ol>
        )}
      </div>
    </Card>
  )
}

/** A window around a moment: what led up to it and what followed. */
export function windowAround(at: string, beforeS = 90, afterS = 60): { start: string; end: string } {
  const moment = new Date(at).valueOf()
  return {
    start: new Date(moment - beforeS * 1000).toISOString(),
    end: new Date(moment + afterS * 1000).toISOString(),
  }
}
