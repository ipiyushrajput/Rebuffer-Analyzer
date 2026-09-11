/** The sticky verdict banner: primary root cause, owner, rebuffer ratio and risk score. */

import { OwnerChip } from './FindingCard'
import { duration, ratio } from '../lib/format'
import type { VerdictData } from '../ws/messages'

interface Props {
  verdict: VerdictData | null
  threshold: number
  elapsed: number
}

const STATUS_STYLE: Record<string, string> = {
  'REBUFFERING — STREAM DEFECT': 'border-critical bg-red-50 text-critical',
  'REBUFFERING RISK — STREAM DEFECT': 'border-warn bg-amber-50 text-warn',
  'NO STREAM-SIDE DEFECT': 'border-pass bg-green-50 text-pass',
}

function RiskGauge({ score }: { score: number }) {
  const colour = score >= 70 ? '#b4151b' : score >= 40 ? '#9a6700' : '#1a7f37'
  const circumference = 2 * Math.PI * 26
  return (
    <div className="flex items-center gap-2" title={`Rebuffer risk score ${score} of 100`}>
      <svg width="64" height="64" viewBox="0 0 64 64" role="img" aria-label={`Risk score ${score}`}>
        <circle cx="32" cy="32" r="26" fill="none" stroke="#e4e7ef" strokeWidth="8" />
        <circle
          cx="32"
          cy="32"
          r="26"
          fill="none"
          stroke={colour}
          strokeWidth="8"
          strokeLinecap="round"
          strokeDasharray={`${(score / 100) * circumference} ${circumference}`}
          transform="rotate(-90 32 32)"
        />
        <text x="32" y="37" textAnchor="middle" fontSize="16" fontWeight="700" fill="#10131c">
          {score}
        </text>
      </svg>
      <span className="text-xs font-semibold uppercase tracking-wide text-[var(--rba-muted)]">
        Risk
        <br />
        score
      </span>
    </div>
  )
}

export function VerdictBanner({ verdict, threshold, elapsed }: Props) {
  if (!verdict) {
    return (
      <div className="card border-l-4 border-brand-600 px-4 py-3">
        <p className="text-sm text-[var(--rba-muted)]">
          The verdict appears once the first measurements are in. Elapsed {duration(elapsed)}.
        </p>
      </div>
    )
  }

  const measured = verdict.measured_rebuffer_ratio
  const overThreshold = measured != null && measured > threshold
  const statusClass = STATUS_STYLE[verdict.status] ?? 'border-brand-600 bg-brand-50 text-brand-700'

  return (
    <div className={`card border-l-4 ${statusClass.split(' ')[0]}`}>
      <div className="flex flex-col gap-4 px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`chip ${statusClass}`}>{verdict.status}</span>
            {verdict.owner && <OwnerChip owner={verdict.owner} label={verdict.owner_label ?? undefined} />}
          </div>
          <p className="mt-2 text-sm font-semibold leading-snug">{verdict.headline}</p>
          {verdict.required_fix && (
            <p className="mt-1 text-sm text-[var(--rba-muted)]">
              <span className="font-semibold text-[var(--rba-ink)]">Required fix. </span>
              {verdict.required_fix}
            </p>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-6">
          <div>
            <div className="label">Rebuffer ratio</div>
            <div
              className={`font-mono text-lg font-semibold ${
                overThreshold ? 'text-critical' : 'text-pass'
              }`}
            >
              {ratio(measured)}
              <span className="ml-1 text-xs font-normal text-[var(--rba-muted)]">
                / {threshold}
              </span>
            </div>
            {verdict.worst_variant && (
              <div className="text-xs text-[var(--rba-muted)]">on {verdict.worst_variant}</div>
            )}
          </div>
          <div>
            <div className="label">Incidents</div>
            <div className="font-mono text-lg font-semibold">{verdict.incident_count}</div>
            <div className="text-xs text-[var(--rba-muted)]">
              {duration(verdict.incident_seconds)} total
            </div>
          </div>
          <RiskGauge score={verdict.risk_score} />
        </div>
      </div>
    </div>
  )
}
