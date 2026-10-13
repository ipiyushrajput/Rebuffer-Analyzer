/**
 * The playlist a finding fired on.
 *
 * Read from the poll log: the exact poll the occurrence names when it carries one, otherwise
 * the rendition's poll current at the moment it was measured — never the live window. The
 * lines the evidence points at are marked and scrolled to, the previous poll is a click away
 * as a diff, and the request is one copy away as a cURL a CDN engineer can run.
 */

import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { endpoints, type PollData } from '../api/client'
import { localDateTime, utcTitle } from '../lib/format'
import type { FindingData } from '../ws/messages'
import { Card, CardHeader, CopyButton, EmptyState, InlineAlert, SegmentedControl, cx } from './ui'

type Mode = 'manifest' | 'diff'

export interface DiffLine {
  number: number | null
  text: string
  change: 'same' | 'added' | 'removed'
}

/** Lines of `current` that were not in `previous`, and the ones that went, in order. */
export function diffLines(previous: string, current: string): DiffLine[] {
  const remaining = new Map<string, number>()
  const before = previous.split('\n')
  for (const line of before) remaining.set(line, (remaining.get(line) ?? 0) + 1)
  const out: DiffLine[] = current.split('\n').map((text, index) => {
    const left = remaining.get(text) ?? 0
    if (left > 0) {
      remaining.set(text, left - 1)
      return { number: index + 1, text, change: 'same' as const }
    }
    return { number: index + 1, text, change: 'added' as const }
  })
  const removed: DiffLine[] = []
  for (const line of before) {
    const left = remaining.get(line) ?? 0
    if (left > 0) {
      remaining.set(line, left - 1)
      removed.push({ number: null, text: line, change: 'removed' })
    }
  }
  return [...removed, ...out]
}

function pollOf(sample: Record<string, unknown> | null): Record<string, unknown> | null {
  const poll = sample?.poll
  return poll && typeof poll === 'object' ? (poll as Record<string, unknown>) : null
}

export function PollManifest({
  jobId,
  finding,
  sample,
}: {
  jobId: string
  finding: FindingData
  sample: Record<string, unknown> | null
}) {
  const [mode, setMode] = useState<Mode>('manifest')
  const ref = pollOf(sample)
  const seq = typeof ref?.seq === 'number' ? ref.seq : null
  const at = (typeof ref?.at === 'string' ? ref.at : null) ?? (sample?.at as string | undefined) ?? finding.first_seen
  const variant = (typeof ref?.variant === 'string' ? ref.variant : null) ?? finding.variant

  const { data, error, isLoading } = useQuery({
    queryKey: ['poll-locate', jobId, seq, variant, at, finding.rule_id, JSON.stringify(sample ?? {})],
    queryFn: () =>
      endpoints.locatePoll(jobId, {
        seq,
        variant: variant ?? (finding.stream_layer ? 'master' : null),
        at,
        evidence: sample ?? {},
        text: `${finding.title} ${finding.detail} ${(sample?.detail as string | undefined) ?? ''}`,
      }),
    enabled: !!jobId && (seq !== null || !!variant),
    staleTime: Infinity,
    retry: false,
  })

  return (
    <Card>
      <CardHeader
        title="Manifest at the time"
        subtitle={data ? <PollLine poll={data} /> : undefined}
        actions={
          data && (
            <div className="flex flex-wrap items-center gap-1.5">
              <SegmentedControl
                options={[
                  { value: 'manifest', label: 'Manifest' },
                  { value: 'diff', label: 'Diff vs previous', disabled: !data.previous },
                ]}
                value={mode}
                onChange={(next) => setMode(next as Mode)}
                ariaLabel="Manifest view"
              />
              <CopyButton label="Copy manifest" text={data.raw} />
              {data.curl && <CopyButton label="Copy as cURL" text={data.curl} />}
            </div>
          )
        }
      />
      <div className="px-5 pb-5">
        {!variant && seq === null && (
          <EmptyState title="This finding names no rendition or poll" />
        )}
        {isLoading && <EmptyState title="Loading…" />}
        {error && (
          <InlineAlert tone="warn">
            {error instanceof Error ? error.message : String(error)}
          </InlineAlert>
        )}
        {data && <PollBody poll={data} mode={mode} />}
      </div>
    </Card>
  )
}

function PollLine({ poll }: { poll: PollData }) {
  return (
    <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <span className="font-mono">poll #{poll.seq}</span>
      <span>·</span>
      <span className="font-mono" title={utcTitle(poll.at)}>
        {localDateTime(poll.at)}
      </span>
      <span>·</span>
      <span className={cx('font-mono', poll.status >= 200 && poll.status < 300 ? '' : 'text-pink-600')}>
        HTTP {poll.status || '—'}
      </span>
      {poll.ttfb_ms != null && <span className="font-mono">· TTFB {Math.round(poll.ttfb_ms)} ms</span>}
      {poll.msn != null && <span className="font-mono">· MSN {poll.msn}</span>}
      <span className={poll.exact ? 'chip-clean' : 'chip-neutral'}>
        {poll.exact ? 'Fetched when the rule fired' : 'Nearest poll to the moment'}
      </span>
    </span>
  )
}

function PollBody({ poll, mode }: { poll: PollData; mode: Mode }) {
  const box = useRef<HTMLDivElement | null>(null)
  const marks = useMemo(
    () => new Map((poll.highlights ?? []).map((h) => [h.line, h.reason])),
    [poll.highlights],
  )
  const lines = useMemo(() => {
    if (mode === 'diff' && poll.previous) return diffLines(poll.previous.raw ?? '', poll.raw)
    return poll.raw.split('\n').map((text, index) => ({
      number: index + 1,
      text,
      change: 'same' as const,
    }))
  }, [mode, poll])

  useEffect(() => {
    const container = box.current
    if (!container) return
    const first = container.querySelector<HTMLElement>('[data-marked="true"]')
    if (first) container.scrollTop = Math.max(0, first.offsetTop - 48)
  }, [lines])

  return (
    <div className="space-y-2">
      <p className="break-all font-mono text-micro text-ink-faint">{poll.final_url ?? poll.url}</p>
      {poll.error && <InlineAlert tone="error">{poll.error}</InlineAlert>}
      {mode === 'manifest' && marks.size === 0 && (
        <p className="text-micro text-ink-muted">No single line: the finding is about the window as a whole.</p>
      )}
      <div
        ref={box}
        className="relative max-h-[480px] overflow-auto rounded-tile border border-surface-line bg-surface-raised"
      >
        {lines.map((line, index) => {
          const reason = line.number != null && mode === 'manifest' ? marks.get(line.number) : undefined
          return (
            <div
              key={`${line.number ?? 'r'}-${index}`}
              data-marked={reason ? 'true' : undefined}
              title={reason}
              className={cx(
                'flex gap-3 border-l-[3px] px-3 py-0.5 font-mono text-micro leading-relaxed',
                reason
                  ? 'border-pink-500 bg-pink-50 text-pink-600'
                  : line.change === 'added'
                    ? 'border-violet-500 bg-violet-50 text-violet-700'
                    : line.change === 'removed'
                      ? 'border-ink-faint bg-surface-sunken text-ink-faint line-through'
                      : 'border-transparent text-ink-soft',
              )}
            >
              <span className="w-10 shrink-0 select-none text-right text-ink-faint">
                {line.change === 'removed' ? '−' : line.change === 'added' ? `+${line.number}` : line.number}
              </span>
              <span className="min-w-0 whitespace-pre-wrap break-all">{line.text || ' '}</span>
            </div>
          )
        })}
      </div>
      {mode === 'diff' && poll.previous && (
        <p className="text-micro text-ink-muted">
          Against poll #{poll.previous.seq} at{' '}
          <span className="font-mono">{localDateTime(poll.previous.at)}</span>
        </p>
      )}
    </div>
  )
}
