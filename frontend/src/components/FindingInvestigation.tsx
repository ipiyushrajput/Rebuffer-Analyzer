/**
 * Finding investigation.
 *
 * The drill-down that turns one finding into an escalation. It reads top to bottom as a
 * chain: what the playlist declares, what the delivery layer returned, what the player did,
 * and what the viewer lost. The decision card beneath it names the owner and the fix in the
 * words the escalation carries, so the operator copies rather than rewrites.
 */

import { useMemo } from 'react'
import { OWNER_LABEL, SEVERITY_STYLE, type Severity } from '../lib/constants'
import { localDateTime, utcTitle } from '../lib/format'
import type { FindingData } from '../ws/messages'
import { Card, CardHeader, CopyButton, EmptyState, SeverityChip, cx } from './ui'
import { IconArrowRight } from './ui/icons'

const IMPACT_TEXT: Record<string, string> = {
  direct: 'The viewer buffers while this condition holds.',
  indirect: 'This condition removes headroom the player needs to survive the next one.',
  none: 'Playback continues while this condition holds.',
}

const LAYER_TEXT: Record<string, string> = {
  PLAYBACK: 'Playback URL',
  ORIGIN: 'Origin',
  CDN: 'CDN',
  SSAI: 'SSAI',
}

/** The escalation text, assembled once so the copy button and the panel never disagree. */
export function escalationText(finding: FindingData): string {
  return [
    `Rule: ${finding.rule_id} — ${finding.title}`,
    `Severity: ${finding.severity}`,
    `Layer: ${LAYER_TEXT[finding.stream_layer] ?? finding.stream_layer}`,
    `Rendition: ${finding.variant ?? 'every rendition'}`,
    `Occurrences: ${finding.count}`,
    `First seen: ${finding.first_seen}`,
    `Last seen: ${finding.last_seen}`,
    '',
    `Measured: ${finding.detail}`,
    `Root cause: ${finding.root_cause}`,
    `Responsible party: ${finding.owner_label || OWNER_LABEL[finding.owner] || finding.owner}`,
    `Required fix: ${finding.fix}`,
    `Rebuffer impact: ${IMPACT_TEXT[finding.rebuffer_impact] ?? finding.rebuffer_impact}`,
    finding.reference ? `Reference: ${finding.reference}` : '',
  ]
    .filter((line) => line !== '')
    .join('\n')
}

function Step({
  index,
  eyebrow,
  title,
  body,
  tone,
  last,
}: {
  index: number
  eyebrow: string
  title: string
  body: string
  tone: 'blue' | 'violet' | 'pink' | 'ink'
  last?: boolean
}) {
  const ring = {
    blue: 'bg-brand-600',
    violet: 'bg-violet-500',
    pink: 'bg-pink-500',
    ink: 'bg-ink',
  }[tone]
  return (
    <li className="flex flex-1 items-stretch gap-3">
      <div className="min-w-0 flex-1 rounded-tile border border-surface-line bg-white px-3.5 py-3">
        <div className="flex items-center gap-2">
          <span
            className={cx(
              'flex h-5 w-5 items-center justify-center rounded-full font-mono text-[10px] font-semibold text-white',
              ring,
            )}
          >
            {index}
          </span>
          <span className="label">{eyebrow}</span>
        </div>
        <p className="mt-2 text-body font-semibold text-ink">{title}</p>
        <p className="mt-1 text-small leading-snug text-ink-muted">{body}</p>
      </div>
      {!last && (
        <span className="hidden shrink-0 self-center text-ink-faint lg:block">
          <IconArrowRight size={16} />
        </span>
      )}
    </li>
  )
}

function DecisionRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="border-b border-surface-line px-5 py-3.5 last:border-b-0">
      <p className="label">{label}</p>
      <div className="mt-1 text-body text-ink-soft">{children}</div>
    </div>
  )
}

/** Evidence dictionaries are flat; nested values are shown as their JSON so nothing is lost. */
function evidenceValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export function FindingInvestigation({
  finding,
  manifest,
}: {
  finding: FindingData
  manifest?: { title: string; raw: string; url?: string } | null
}) {
  const style = SEVERITY_STYLE[finding.severity as Severity]
  const owner = finding.owner_label || OWNER_LABEL[finding.owner] || finding.owner

  const evidenceKeys = useMemo(() => {
    const keys: string[] = []
    for (const record of finding.evidence ?? []) {
      for (const key of Object.keys(record)) if (!keys.includes(key)) keys.push(key)
    }
    return keys
  }, [finding.evidence])

  /*
   * The manifest lines worth showing are the ones naming a resource the rule measured. Only
   * string evidence becomes a needle, and a URL contributes its last path segment as well —
   * a bare number matches a timestamp or a duration on any line and marks the wrong one.
   */
  const manifestLines = useMemo(() => {
    if (!manifest?.raw) return []
    const needles = new Set<string>()
    for (const record of finding.evidence ?? []) {
      for (const value of Object.values(record)) {
        if (typeof value !== 'string') continue
        if (value.length < 4 || value.length > 400) continue
        if (/^[\d.\-+:TZ]+$/.test(value)) continue
        const tail = value.split('?')[0].split('/').pop()
        if (tail && tail.length > 3) needles.add(tail)
        if (!value.includes('/')) needles.add(value)
      }
    }
    const lines = manifest.raw.split('\n')
    const marked = lines.map((text, index) => ({
      number: index + 1,
      text,
      marked: [...needles].some((needle) => text.includes(needle)),
    }))
    const hits = marked.filter((line) => line.marked)
    if (hits.length === 0) return marked.slice(0, 24)
    /* Three lines of context each side of the first hit keep the tag in its block. */
    const first = hits[0].number - 1
    return marked.slice(Math.max(0, first - 3), first + 9)
  }, [manifest, finding.evidence])

  return (
    <div className="space-y-4">
      {/* --- the condition --------------------------------------------------- */}
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-4 px-5 pb-4 pt-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <SeverityChip severity={finding.severity as Severity} />
              <code className="font-mono text-micro text-ink-faint">{finding.rule_id}</code>
              <span className="chip-neutral">{LAYER_TEXT[finding.stream_layer] ?? finding.stream_layer}</span>
              <span className="chip-blue">{owner}</span>
            </div>
            <h2 className="mt-2.5 text-section text-ink">{finding.title}</h2>
            <p className="mt-1.5 max-w-3xl text-body text-ink-soft">{finding.detail}</p>
          </div>
          <div className="shrink-0 text-right">
            <p className="label">Occurrences</p>
            <p className={cx('metric mt-1', style.text)}>{finding.count}</p>
            <p className="mt-1 text-micro text-ink-muted" title={utcTitle(finding.first_seen)}>
              first {localDateTime(finding.first_seen)}
            </p>
            <p className="text-micro text-ink-muted" title={utcTitle(finding.last_seen)}>
              last {localDateTime(finding.last_seen)}
            </p>
          </div>
        </div>
      </Card>

      {/* --- causal chain ---------------------------------------------------- */}
      <Card>
        <CardHeader
          title="Causal chain"
          subtitle="Each step is measured; the step after it is what that measurement caused."
        />
        <ol className="flex flex-col gap-3 px-5 pb-5 lg:flex-row">
          <Step
            index={1}
            eyebrow="Playlist"
            title={LAYER_TEXT[finding.stream_layer] ?? finding.stream_layer}
            body={finding.detail}
            tone="blue"
          />
          <Step
            index={2}
            eyebrow="Delivery"
            title={
              Object.entries(finding.layer_presence ?? {})
                .filter(([, present]) => present)
                .map(([layer]) => LAYER_TEXT[layer] ?? layer)
                .join(', ') || owner
            }
            body={finding.root_cause}
            tone="violet"
          />
          <Step
            index={3}
            eyebrow="Player"
            title={finding.variant ?? 'Every rendition'}
            body={`The condition was measured ${finding.count} time${finding.count === 1 ? '' : 's'} in this window.`}
            tone="pink"
          />
          <Step
            index={4}
            eyebrow="Impact"
            title={
              finding.rebuffer_impact === 'direct'
                ? 'Rebuffer'
                : finding.rebuffer_impact === 'indirect'
                  ? 'Reduced headroom'
                  : 'Playback held'
            }
            body={IMPACT_TEXT[finding.rebuffer_impact] ?? finding.rebuffer_impact}
            tone="ink"
            last
          />
        </ol>
      </Card>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        {/* --- evidence ------------------------------------------------------ */}
        <div className="space-y-4">
          <Card>
            <CardHeader
              title="Request evidence"
              subtitle="The measured values the rule fired on."
              actions={
                finding.evidence?.length > 0 && (
                  <CopyButton
                    label="Copy evidence"
                    text={() => JSON.stringify(finding.evidence, null, 2)}
                  />
                )
              }
            />
            {finding.evidence?.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="table table-hover">
                  <thead>
                    <tr>
                      <th>#</th>
                      {evidenceKeys.map((key) => (
                        <th key={key}>{key.replace(/_/g, ' ')}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {finding.evidence.map((record, index) => (
                      <tr key={index}>
                        <td className="font-mono text-micro text-ink-faint">{index + 1}</td>
                        {evidenceKeys.map((key) => {
                          const text = evidenceValue(record[key])
                          return (
                            <td key={key} className="font-mono text-micro text-ink-soft">
                              <span
                                className="block max-w-[280px] truncate"
                                title={text.length > 40 ? text : undefined}
                              >
                                {text}
                              </span>
                            </td>
                          )
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState
                title="This rule fired on a structural condition"
                detail="The condition is stated in the finding itself; there is no per-request sample to list."
              />
            )}
          </Card>

          {manifest && manifestLines.length > 0 && (
            <Card>
              <CardHeader
                title="Manifest context"
                subtitle={manifest.title}
                actions={<CopyButton label="Copy manifest" text={manifest.raw} />}
              />
              <div className="px-5 pb-5">
                {manifest.url && (
                  <p className="mb-2 truncate font-mono text-micro text-ink-faint" title={manifest.url}>
                    {manifest.url}
                  </p>
                )}
                <div className="overflow-hidden rounded-tile border border-surface-line bg-surface-raised">
                  {manifestLines.map((line) => (
                    <div
                      key={line.number}
                      className={cx(
                        'flex gap-3 px-3 py-0.5 font-mono text-micro leading-relaxed',
                        line.marked
                          ? 'border-l-[3px] border-pink-500 bg-pink-50 text-pink-600'
                          : 'border-l-[3px] border-transparent text-ink-soft',
                      )}
                    >
                      <span className="w-8 shrink-0 select-none text-right text-ink-faint">
                        {line.number}
                      </span>
                      <span className="min-w-0 break-all">{line.text || ' '}</span>
                    </div>
                  ))}
                </div>
              </div>
            </Card>
          )}
        </div>

        {/* --- decision ------------------------------------------------------ */}
        <div className="space-y-4">
          <Card>
            <div className="rounded-t-card bg-spectrum-soft px-5 py-4">
              <p className="text-label uppercase text-white/70">Decision</p>
              <p className="mt-1 text-card font-semibold text-white">{finding.title}</p>
            </div>
            <DecisionRow label="Root cause">{finding.root_cause}</DecisionRow>
            <DecisionRow label="Responsible party">
              <span className="chip-pink">{owner}</span>
            </DecisionRow>
            <DecisionRow label="Required fix">{finding.fix}</DecisionRow>
            <DecisionRow label="Rebuffer impact">
              {IMPACT_TEXT[finding.rebuffer_impact] ?? finding.rebuffer_impact}
            </DecisionRow>
            {finding.reference && (
              <DecisionRow label="Reference">
                <span className="font-mono text-micro text-ink-soft">{finding.reference}</span>
              </DecisionRow>
            )}
            <div className="flex items-center justify-between gap-3 bg-surface-sunken px-5 py-3.5">
              <span className="chip-clean">Escalation ready</span>
              <CopyButton
                label="Copy escalation"
                variant="primary"
                text={() => escalationText(finding)}
              />
            </div>
          </Card>

          <Card>
            <CardHeader title="Layer presence" subtitle="Where the same check was run." />
            <ul className="px-5 pb-5">
              {Object.keys(finding.layer_presence ?? {}).length === 0 && (
                <li className="text-small text-ink-muted">
                  Only the playback URL was supplied, so the defect is attributed from its own
                  response headers.
                </li>
              )}
              {Object.entries(finding.layer_presence ?? {}).map(([layer, present]) => (
                <li
                  key={layer}
                  className="flex items-center justify-between border-b border-surface-line py-2 last:border-b-0"
                >
                  <span className="text-body text-ink-soft">{LAYER_TEXT[layer] ?? layer}</span>
                  <span className={present ? 'chip-pink' : 'chip-clean'}>
                    {present ? 'Present' : 'Absent'}
                  </span>
                </li>
              ))}
            </ul>
          </Card>
        </div>
      </div>
    </div>
  )
}
