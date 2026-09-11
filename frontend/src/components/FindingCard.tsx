/**
 * One finding, rendered as a titled card with a one-line explanation and the fix.
 *
 * Every card states the exact error, the evidence that proves it, the root cause, the
 * responsible party and the fix. There is no hedging text anywhere in this component.
 */

import { useState } from 'react'
import { OWNER_LABEL, SEVERITY_STYLE, type Severity } from '../lib/constants'
import { localTime, utcTitle } from '../lib/format'
import type { FindingData } from '../ws/messages'

interface Props {
  finding: FindingData
  defaultOpen?: boolean
}

export function SeverityChip({ severity }: { severity: Severity }) {
  const style = SEVERITY_STYLE[severity]
  return (
    <span className={`chip ${style.bg} ${style.text} ${style.border}`}>
      <span aria-hidden>{style.icon}</span>
      {style.label}
    </span>
  )
}

export function OwnerChip({ owner, label }: { owner: string; label?: string }) {
  return (
    <span className="chip border-brand-200 bg-brand-50 text-brand-700">
      {label ?? OWNER_LABEL[owner] ?? owner}
    </span>
  )
}

export function FindingCard({ finding, defaultOpen = false }: Props) {
  const [open, setOpen] = useState(defaultOpen)
  const [copied, setCopied] = useState(false)
  const style = SEVERITY_STYLE[finding.severity]

  const copy = async () => {
    const text = [
      `${finding.rule_id} — ${finding.title}`,
      `Severity: ${finding.severity}`,
      `Responsible party: ${finding.owner_label}`,
      finding.variant ? `Rendition: ${finding.variant}` : '',
      `Occurrences: ${finding.count} (first ${finding.first_seen}, last ${finding.last_seen})`,
      '',
      `Evidence: ${finding.detail}`,
      `Root cause: ${finding.root_cause}`,
      `Fix: ${finding.fix}`,
    ]
      .filter(Boolean)
      .join('\n')
    await navigator.clipboard.writeText(text)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
  }

  return (
    <article className={`card border-l-4 ${style.border}`}>
      <div className="flex items-start justify-between gap-3 px-4 py-3">
        <button
          type="button"
          className="flex-1 text-left"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
        >
          <div className="flex flex-wrap items-center gap-2">
            <SeverityChip severity={finding.severity} />
            <code className="mono text-[var(--rba-muted)]">{finding.rule_id}</code>
            <span className="text-sm font-semibold">{finding.title}</span>
            {finding.count > 1 && (
              <span className="chip border-slate-200 bg-slate-50 text-[var(--rba-muted)]">
                ×{finding.count}
              </span>
            )}
          </div>
          <p className="mt-1.5 text-sm text-[var(--rba-ink)]">{finding.detail}</p>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-[var(--rba-muted)]">
            <OwnerChip owner={finding.owner} label={finding.owner_label} />
            {finding.variant && (
              <span className="chip border-slate-200 bg-slate-50">{finding.variant}</span>
            )}
            <span className="chip border-slate-200 bg-slate-50">{finding.stream_layer}</span>
            <span title={utcTitle(finding.first_seen)}>first {localTime(finding.first_seen)}</span>
            <span title={utcTitle(finding.last_seen)}>last {localTime(finding.last_seen)}</span>
          </div>
        </button>
        <button type="button" className="btn-secondary shrink-0" onClick={copy}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>

      {open && (
        <div className="border-t border-[var(--rba-line)] bg-slate-50/60 px-4 py-3 text-sm">
          <dl className="space-y-2">
            <div>
              <dt className="label">Root cause</dt>
              <dd>{finding.root_cause}</dd>
            </div>
            <div>
              <dt className="label">Required fix</dt>
              <dd>{finding.fix}</dd>
            </div>
            <div>
              <dt className="label">Rebuffer impact</dt>
              <dd className="capitalize">{finding.rebuffer_impact}</dd>
            </div>
            {finding.reference && (
              <div>
                <dt className="label">Equivalent check in market tools</dt>
                <dd>{finding.reference}</dd>
              </div>
            )}
            {Object.keys(finding.layer_presence ?? {}).length > 0 && (
              <div>
                <dt className="label">Layer presence</dt>
                <dd className="flex flex-wrap gap-2">
                  {Object.entries(finding.layer_presence).map(([layer, present]) => (
                    <span
                      key={layer}
                      className={`chip ${
                        present
                          ? 'border-critical bg-red-50 text-critical'
                          : 'border-pass bg-green-50 text-pass'
                      }`}
                    >
                      {layer}: {present ? 'present' : 'clean'}
                    </span>
                  ))}
                </dd>
              </div>
            )}
            {finding.evidence?.length > 0 && (
              <div>
                <dt className="label">Evidence ({finding.evidence.length} sample(s))</dt>
                <dd>
                  <pre className="manifest-pane mono max-h-64 rounded border border-[var(--rba-line)] bg-white p-2">
                    {JSON.stringify(finding.evidence, null, 2)}
                  </pre>
                </dd>
              </div>
            )}
          </dl>
        </div>
      )}
    </article>
  )
}

export function FindingsFeed({ findings }: { findings: FindingData[] }) {
  if (findings.length === 0) {
    return (
      <p className="px-4 py-6 text-sm text-[var(--rba-muted)]">
        No finding has been raised yet. Checks that return clean appear here as PASS lines.
      </p>
    )
  }
  return (
    <div className="space-y-2">
      {findings.map((finding) => (
        <FindingCard
          key={`${finding.rule_id}|${finding.stream_layer}|${finding.variant ?? ''}`}
          finding={finding}
        />
      ))}
    </div>
  )
}
