/**
 * Findings.
 *
 * A finding card carries condition, ownership and consequence through composition alone:
 * a severity rail on the edge, the rule identifier in monospace, the title in prose, and
 * the affected layer and rendition beneath. The evidence itself opens in its own view.
 */

import { useMemo } from 'react'
import { SEVERITY_STYLE, type Severity } from '../lib/constants'
import { groupFindings, type FindingGroup } from '../lib/findingGroups'
import { localTime, utcTitle } from '../lib/format'
import type { FindingData } from '../ws/messages'
import { EmptyState, SeverityChip, cx } from './ui'
import { IconArrowRight } from './ui/icons'

export function findingKey(finding: FindingData): string {
  return `${finding.rule_id}|${finding.stream_layer}|${finding.variant ?? ''}`
}

export function FindingCard({
  finding,
  onOpen,
  compact = false,
}: {
  finding: FindingData
  onOpen?: (finding: FindingData) => void
  compact?: boolean
}) {
  const style = SEVERITY_STYLE[finding.severity as Severity]
  const context = [finding.owner_label, finding.variant].filter(Boolean).join(' · ')

  return (
    <article
      className={cx(
        'group relative overflow-hidden rounded-card border bg-white shadow-card transition-colors duration-150',
        finding.severity === 'CRITICAL' || finding.severity === 'ERROR'
          ? 'border-pink-200'
          : finding.severity === 'WARN'
            ? 'border-violet-200'
            : finding.severity === 'INFO'
              ? 'border-brand-200'
              : 'border-surface-line',
        onOpen && 'hover:shadow-raised',
      )}
    >
      <span className={cx('absolute inset-y-0 left-0 w-[3px]', style.rail)} />
      <div className="px-4 py-3 pl-5">
        <div className="flex items-center justify-between gap-3">
          <SeverityChip severity={finding.severity as Severity} />
          <code className="font-mono text-[11px] text-ink-faint">{finding.rule_id}</code>
        </div>

        <h4 className="mt-2 text-body font-semibold leading-snug text-ink">{finding.title}</h4>

        {!compact && (
          <p className="mt-1 line-clamp-2 text-small text-ink-muted">{finding.detail}</p>
        )}

        <p className="mt-1.5 text-micro text-ink-muted">
          {context || 'the whole ladder'}
          {finding.count > 1 && <span className="tabular"> · {finding.count} occurrences</span>}
        </p>

        <div className="mt-2 flex items-center justify-between gap-2">
          <span className="text-[11px] text-ink-faint" title={utcTitle(finding.first_seen)}>
            {finding.evidence?.length ? 'Evidence captured' : 'No evidence sample'} ·{' '}
            {localTime(finding.first_seen)}
          </span>
          {onOpen && (
            <button
              type="button"
              onClick={() => onOpen(finding)}
              className="inline-flex items-center gap-1 text-[11px] font-semibold text-brand-600 hover:underline"
            >
              Open
              <IconArrowRight size={13} />
            </button>
          )}
        </div>
      </div>
    </article>
  )
}

/** One card for one issue, however many renditions it was measured on. */
export function FindingGroupCard({
  group,
  onOpen,
}: {
  group: FindingGroup
  onOpen?: (finding: FindingData) => void
}) {
  if (group.members.length === 1) {
    return <FindingCard finding={group.lead} onOpen={onOpen} />
  }
  const style = SEVERITY_STYLE[group.severity as Severity]
  return (
    <article
      className={cx(
        'group relative overflow-hidden rounded-card border bg-white shadow-card transition-colors duration-150',
        group.severity === 'CRITICAL' || group.severity === 'ERROR'
          ? 'border-pink-200'
          : group.severity === 'WARN'
            ? 'border-violet-200'
            : group.severity === 'INFO'
              ? 'border-brand-200'
              : 'border-surface-line',
        onOpen && 'hover:shadow-raised',
      )}
    >
      <span className={cx('absolute inset-y-0 left-0 w-[3px]', style.rail)} />
      <div className="px-4 py-3 pl-5">
        <div className="flex items-center justify-between gap-3">
          <SeverityChip severity={group.severity as Severity} />
          <code className="font-mono text-[11px] text-ink-faint">{group.rule_id}</code>
        </div>
        <h4 className="mt-2 text-body font-semibold leading-snug text-ink">{group.title}</h4>
        <p className="mt-1 line-clamp-2 break-words text-small text-ink-muted">{group.lead.detail}</p>
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-micro text-ink-muted">
            {group.lead.owner_label} · {group.members.length} renditions ·{' '}
            <span className="tabular">{group.count} occurrences</span>
          </span>
          {group.members.map((member) => (
            <span key={member.variant ?? ''} className="chip-neutral font-mono" title={`${member.count} occurrence(s)`}>
              {member.variant ?? 'ladder'}
            </span>
          ))}
        </div>
        <div className="mt-2 flex items-center justify-between gap-2">
          <span className="text-[11px] text-ink-faint" title={utcTitle(group.first_seen)}>
            first {localTime(group.first_seen)}
          </span>
          {onOpen && (
            <button
              type="button"
              onClick={() => onOpen(group.lead)}
              className="inline-flex items-center gap-1 text-[11px] font-semibold text-brand-600 hover:underline"
            >
              Open
              <IconArrowRight size={13} />
            </button>
          )}
        </div>
      </div>
    </article>
  )
}

export function FindingsList({
  findings,
  onOpen,
  emptyTitle = 'No findings yet',
}: {
  findings: FindingData[]
  onOpen?: (finding: FindingData) => void
  emptyTitle?: string
}) {
  const groups = useMemo(() => groupFindings(findings), [findings])
  if (findings.length === 0) {
    return <EmptyState title={emptyTitle} />
  }
  return (
    <div className="space-y-2.5">
      {groups.map((group) => (
        <FindingGroupCard key={group.key} group={group} onOpen={onOpen} />
      ))}
    </div>
  )
}

/** A compact row for report and history contexts, where space is tighter than the feed. */
export function FindingRow({ finding }: { finding: FindingData }) {
  const style = SEVERITY_STYLE[finding.severity as Severity]
  return (
    <div className="flex gap-3 border-b border-surface-line py-3 last:border-b-0">
      <span className={cx('mt-1.5 h-2 w-2 shrink-0 rounded-full', style.rail)} />
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <code className="font-mono text-[11px] text-ink-faint">{finding.rule_id}</code>
          <span className="text-body font-semibold text-ink">{finding.title}</span>
        </div>
        <p className="mt-0.5 text-small text-ink-muted">{finding.detail}</p>
        <p className="mt-1 text-micro text-ink-faint">
          {finding.owner_label} · {finding.variant ?? 'ladder'} · {finding.count} occurrence
          {finding.count === 1 ? '' : 's'}
        </p>
      </div>
    </div>
  )
}
