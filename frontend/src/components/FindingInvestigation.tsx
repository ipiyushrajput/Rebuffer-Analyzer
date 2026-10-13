/**
 * Finding investigation.
 *
 * The drill-down that turns one finding into an escalation. It reads top to bottom as a
 * chain: what the playlist declares, what the delivery layer returned, what the player did,
 * and what the viewer lost. The decision card beneath it names the owner and the fix in the
 * words the escalation carries, so the operator copies rather than rewrites.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { api, endpoints } from '../api/client'
import { curlCommand, requestUrlOf } from '../lib/curl'
import { OWNER_LABEL, SEVERITY_STYLE, type Severity } from '../lib/constants'
import { localDateTime, utcTitle } from '../lib/format'
import type { FindingData } from '../ws/messages'
import { membersOf } from '../lib/findingGroups'
import { PollManifest } from './PollManifest'
import { Timeline, windowAround } from './Timeline'
import { Card, CardHeader, CopyButton, EmptyState, SegmentedControl, SeverityChip, cx } from './ui'
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
export function escalationText(finding: FindingData, members: FindingData[] = [finding]): string {
  const renditions = members.map((item) => item.variant).filter(Boolean)
  return [
    `Rule: ${finding.rule_id} — ${finding.title}`,
    `Severity: ${finding.severity}`,
    `Layer: ${LAYER_TEXT[finding.stream_layer] ?? finding.stream_layer}`,
    members.length > 1
      ? `Renditions: ${renditions.join(', ')} (${members.length})`
      : `Rendition: ${finding.variant ?? 'every rendition'}`,
    `Occurrences: ${members.reduce((sum, item) => sum + item.count, 0)}`,
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

/** Evidence columns that are shown elsewhere: the poll in its own column, the sentence too. */
const HIDDEN_KEYS = new Set(['poll', 'detail', 'at'])

export function FindingInvestigation({
  finding: opened,
  findings,
  jobId,
}: {
  finding: FindingData
  /** Every finding of the run, so the same rule on other renditions opens with this one. */
  findings?: FindingData[]
  /** The run the finding belongs to; the manifest is read from its poll log. */
  jobId?: string | null
}) {
  const members = useMemo(() => membersOf(opened, findings ?? [opened]), [opened, findings])
  const [memberKey, setMemberKey] = useState(() => opened.variant ?? '')
  const finding = members.find((item) => (item.variant ?? '') === memberKey) ?? members[0]
  const evidence = useMemo(() => finding.evidence ?? [], [finding])
  // The latest occurrence first: it is the one the sentence above describes.
  const [sampleIndex, setSampleIndex] = useState<number | null>(null)
  const selected = evidence.length > 0 ? Math.min(sampleIndex ?? evidence.length - 1, evidence.length - 1) : -1
  const sample = selected >= 0 ? evidence[selected] : null

  const { data: profile } = useQuery({
    queryKey: ['request-profile', jobId],
    queryFn: () => api.get<{ user_agent: string }>(`/jobs/${jobId}/request-profile`),
    enabled: !!jobId,
    staleTime: Infinity,
  })
  const userAgent = profile?.user_agent ?? ''

  const samplePoll = sample?.poll as { at?: string } | undefined
  const occurrenceAt = samplePoll?.at ?? (sample?.at as string | undefined) ?? finding.last_seen

  const style = SEVERITY_STYLE[finding.severity as Severity]
  const owner = finding.owner_label || OWNER_LABEL[finding.owner] || finding.owner
  const total = members.reduce((sum, item) => sum + item.count, 0)

  const evidenceKeys = useMemo(() => {
    const keys: string[] = []
    for (const record of evidence) {
      for (const key of Object.keys(record)) {
        if (!HIDDEN_KEYS.has(key) && !keys.includes(key)) keys.push(key)
      }
    }
    return keys
  }, [evidence])

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
            <p className="mt-1.5 max-w-3xl break-words text-body text-ink-soft">{finding.detail}</p>
            {members.length > 1 && (
              <p className="mt-2 text-small text-ink-muted">
                Seen on {members.length} renditions — each opens with its own evidence and manifest.
              </p>
            )}
          </div>
          <div className="shrink-0 text-right">
            <p className="label">Occurrences</p>
            <p className={cx('metric mt-1', style.text)}>{total}</p>
            <p className="mt-1 text-micro text-ink-muted" title={utcTitle(finding.first_seen)}>
              first {localDateTime(finding.first_seen)}
            </p>
            <p className="text-micro text-ink-muted" title={utcTitle(finding.last_seen)}>
              last {localDateTime(finding.last_seen)}
            </p>
          </div>
        </div>
      </Card>

      {members.length > 1 && (
        <Card>
          <div className="flex flex-wrap items-center gap-3 px-5 py-3">
            <span className="label">Rendition</span>
            <SegmentedControl
              options={members.map((item) => ({
                value: item.variant ?? '',
                label: `${item.variant ?? 'every rendition'} · ${item.count}`,
              }))}
              value={finding.variant ?? ''}
              onChange={(next) => {
                setMemberKey(String(next))
                setSampleIndex(null)
              }}
              ariaLabel="Rendition"
            />
          </div>
        </Card>
      )}

      {/* --- causal chain ---------------------------------------------------- */}
      <Card>
        <CardHeader title="Causal chain" />
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
              actions={
                finding.evidence?.length > 0 && (
                  <CopyButton
                    label="Copy evidence"
                    text={() => JSON.stringify(finding.evidence, null, 2)}
                  />
                )
              }
            />
            {evidence.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="table table-hover">
                  <thead>
                    <tr>
                      <th>#</th>
                      <th>Poll</th>
                      <th>Measured</th>
                      <th>Reproduce</th>
                      {evidenceKeys.map((key) => (
                        <th key={key}>{key.replace(/_/g, ' ')}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {evidence.map((record, index) => {
                      const poll = record.poll as { seq?: number; at?: string } | undefined
                      return (
                        <tr
                          key={index}
                          onClick={() => setSampleIndex(index)}
                          className={cx('cursor-pointer', index === selected && 'bg-brand-50')}
                          aria-selected={index === selected}
                        >
                          <td className="font-mono text-micro text-ink-faint">{index + 1}</td>
                          <td className="whitespace-nowrap font-mono text-micro text-ink-soft">
                            {poll?.seq != null ? `#${poll.seq}` : '—'}
                            {poll?.at && (
                              <span className="block text-ink-faint" title={utcTitle(poll.at)}>
                                {localDateTime(poll.at)}
                              </span>
                            )}
                          </td>
                          <td className="min-w-[220px] max-w-[360px] text-micro text-ink-soft">
                            <span className="line-clamp-3 break-words">
                              {String(record.detail ?? finding.detail)}
                            </span>
                          </td>
                          <td onClick={(event) => event.stopPropagation()}>
                            {requestUrlOf(record) ? (
                              <CopyButton
                                label="cURL"
                                text={() => curlCommand(requestUrlOf(record)!, userAgent)}
                              />
                            ) : (
                              <span className="text-micro text-ink-faint">—</span>
                            )}
                          </td>
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
                      )
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title="Structural condition — no per-request sample" />
            )}
          </Card>

          {jobId && <PollManifest jobId={jobId} finding={finding} sample={sample} />}
          {jobId && occurrenceAt && (
            <Timeline
              jobId={jobId}
              {...windowAround(occurrenceAt)}
              variant={finding.variant}
              title="What happened around this occurrence"
            />
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
              {jobId && (
                <a className="btn-ghost btn-sm" href={endpoints.escalationPackUrl(jobId, finding.owner)}>
                  Pack for {owner}
                </a>
              )}
              <CopyButton
                label="Copy escalation"
                variant="primary"
                text={() => escalationText(finding, members)}
              />
            </div>
          </Card>

          <Card>
            <CardHeader title="Layer presence" />
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
