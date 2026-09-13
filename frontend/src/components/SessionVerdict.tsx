/**
 * The executive verdict.
 *
 * One sentence that decides the channel, the party that owns it, and the risk number the
 * escalation carries. The spectrum ground is reserved for this surface and the report header:
 * nothing else on a page competes with it.
 */

import { duration, ratio } from '../lib/format'
import type { VerdictData } from '../ws/messages'
import { Card, cx } from './ui'

const STATUS_TONE: Record<string, { chip: string; ground: string }> = {
  'REBUFFERING — STREAM DEFECT': { chip: 'bg-white/20 text-white', ground: 'bg-spectrum-soft' },
  'REBUFFERING RISK — STREAM DEFECT': {
    chip: 'bg-white/20 text-white',
    ground: 'bg-spectrum-soft',
  },
  'NO STREAM-SIDE DEFECT': { chip: 'bg-white/20 text-white', ground: 'bg-clean-600' },
}

function ScoreBlock({ score, clean }: { score: number; clean: boolean }) {
  return (
    <div
      className={cx(
        'flex min-w-[150px] shrink-0 flex-row items-center justify-between gap-4 px-6 py-5 text-white lg:flex-col lg:justify-center lg:gap-1',
        clean ? 'bg-clean-500' : 'bg-pink-500',
      )}
    >
      <span className="font-mono text-[40px] font-semibold leading-none tabular-nums">{score}</span>
      <span className="text-label uppercase text-white/85">Risk / 100</span>
    </div>
  )
}

export function SessionVerdict({
  verdict,
  threshold,
  elapsed,
}: {
  verdict: VerdictData | null
  threshold: number
  elapsed: number
}) {
  if (!verdict) {
    return (
      <Card className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
        <div>
          <p className="label">Verdict</p>
          <p className="mt-1 text-body text-ink-soft">
            The verdict is issued once the first playlist and segment measurements complete.
          </p>
        </div>
        <span className="chip-neutral">elapsed {duration(elapsed)}</span>
      </Card>
    )
  }

  const clean = verdict.status === 'NO STREAM-SIDE DEFECT'
  const tone = STATUS_TONE[verdict.status] ?? STATUS_TONE['NO STREAM-SIDE DEFECT']
  const measured = verdict.measured_rebuffer_ratio
  const overThreshold = measured != null && measured > threshold

  return (
    <div className="flex flex-col overflow-hidden rounded-card shadow-raised lg:flex-row">
      <div className={cx('min-w-0 flex-1 px-6 py-5 text-white', tone.ground)}>
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={cx(
              'inline-flex items-center rounded-pill px-2.5 py-1 text-label uppercase ring-1 ring-inset ring-white/25',
              tone.chip,
            )}
          >
            {verdict.status}
          </span>
          {verdict.owner_label && (
            <span className="inline-flex items-center rounded-pill bg-white/15 px-2.5 py-1 text-label uppercase text-white ring-1 ring-inset ring-white/25">
              Owner · {verdict.owner_label}
            </span>
          )}
        </div>

        <h2 className="mt-3 max-w-4xl text-[22px] font-semibold leading-snug">
          {verdict.headline}
        </h2>

        {verdict.required_fix && (
          <p className="mt-2 max-w-4xl text-body text-white/85">
            <span className="font-semibold text-white">Required fix. </span>
            {verdict.required_fix}
          </p>
        )}

        <dl className="mt-4 flex flex-wrap gap-x-8 gap-y-2">
          <div>
            <dt className="text-label uppercase text-white/65">Rebuffer ratio</dt>
            <dd className="font-mono text-body font-semibold tabular-nums">
              {ratio(measured)}
              <span className="ml-1 font-sans text-micro font-normal text-white/65">
                of {threshold} {overThreshold ? '· over threshold' : '· within threshold'}
              </span>
            </dd>
          </div>
          <div>
            <dt className="text-label uppercase text-white/65">Incidents</dt>
            <dd className="font-mono text-body font-semibold tabular-nums">
              {verdict.incident_count}
              <span className="ml-1 font-sans text-micro font-normal text-white/65">
                · {duration(verdict.incident_seconds)}
              </span>
            </dd>
          </div>
          <div>
            <dt className="text-label uppercase text-white/65">Worst rendition</dt>
            <dd className="font-mono text-body font-semibold">
              {verdict.worst_variant ?? 'none'}
            </dd>
          </div>
          <div>
            <dt className="text-label uppercase text-white/65">Checked</dt>
            <dd className="font-mono text-body font-semibold tabular-nums">
              {verdict.playlists_checked}
              <span className="ml-1 font-sans text-micro font-normal text-white/65">playlists</span>
              <span className="ml-2">{verdict.segments_checked}</span>
              <span className="ml-1 font-sans text-micro font-normal text-white/65">segments</span>
            </dd>
          </div>
        </dl>
      </div>
      <ScoreBlock score={verdict.risk_score} clean={clean} />
    </div>
  )
}
