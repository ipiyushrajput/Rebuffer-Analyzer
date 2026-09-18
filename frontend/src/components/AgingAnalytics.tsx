/**
 * The analytics an aging run was missing.
 *
 * Realtime draws these charts from a live socket. An aging run has no socket anyone is
 * watching — it runs for days — so the same charts are drawn from what was written down:
 * `/api/aging/jobs/{id}/samples` returns the stored playlist polls and segment fetches in the
 * exact shapes the chart components already take. The components are imported, not rewritten.
 *
 * A week of samples is far too much to draw, so the range filter narrows it and the backend
 * thins what is left. The raw rows stay in the database; what arrives here is what renders,
 * and the panel says when it has been thinned so nobody reads a smoothed line as the whole
 * measurement.
 */

import { useMemo, useState } from 'react'
import { endpoints, type JobSamples } from '../api/client'
import { useQuery } from '@tanstack/react-query'
import {
  AvSkewChart,
  BitrateChart,
  DiscontinuityChart,
  DownloadRatioChart,
  DurationChart,
  FetchTimingChart,
  FreshnessChart,
  SequenceLadderChart,
  StatusHeatmapChart,
  TrafficChart,
  VirtualBufferChart,
} from './charts'
import { Card, CardHeader, InlineAlert, SegmentedControl } from './ui'
import type { BufferPoint } from '../store/session'

/*
 * The defaults the charts colour against, matching `Thresholds` in `app/config.py`. The
 * live values arrive as a prop from the settings the tab already reads; these are what a
 * chart draws before that answer lands, so the two screens never disagree on the line.
 */
const AV_SKEW_CRITICAL_MS = 1000
const DOWNLOAD_RATIO_WARN = 0.5
const DOWNLOAD_RATIO_ERROR = 1

/** How far back the charts look. `full` is the whole run. */
export type RangeId = '1d' | '2d' | '7d' | 'full'

const RANGES: { value: RangeId; label: string }[] = [
  { value: '1d', label: 'Last 1 day' },
  { value: '2d', label: 'Last 2 days' },
  { value: '7d', label: 'Last 7 days' },
  { value: 'full', label: 'Full range' },
]

const DAYS: Record<Exclude<RangeId, 'full'>, number> = { '1d': 1, '2d': 2, '7d': 7 }

/**
 * The window a range means, measured back from the run's last sample.
 *
 * Back from the last sample rather than from now: a run that finished on Friday still shows
 * its last day when it is opened on Monday, instead of an empty chart.
 */
export function windowFor(range: RangeId, lastSample: string | null): string | undefined {
  if (range === 'full' || !lastSample) return undefined
  const end = new Date(lastSample)
  if (Number.isNaN(end.valueOf())) return undefined
  return new Date(end.valueOf() - DAYS[range] * 86_400_000).toISOString()
}

/** A spike window from the batch correlation, shaded on the timeline. */
export interface SpikeBand {
  start: string
  end: string
  label: string
}

export function AgingAnalytics({
  jobId,
  thresholds = {},
  spikes = [],
}: {
  jobId: string
  thresholds?: Record<string, number>
  spikes?: SpikeBand[]
}) {
  const [range, setRange] = useState<RangeId>('full')

  // The bounds come back with the samples, so the first read is always the whole run.
  const bounds = useQuery({
    queryKey: ['aging-samples-bounds', jobId],
    queryFn: () => endpoints.agingSamples(jobId, { maxPoints: 50 }),
    staleTime: Infinity,
  })
  const lastSample = bounds.data?.range.last_sample ?? null

  const samples = useQuery({
    queryKey: ['aging-samples', jobId, range, lastSample],
    queryFn: () => endpoints.agingSamples(jobId, { from: windowFor(range, lastSample) }),
    enabled: bounds.isSuccess,
  })

  const data = samples.data as JobSamples | undefined
  const snapshots = data?.snapshots ?? []
  const segments = data?.segments ?? []

  /*
   * The virtual buffer series, in the shape the chart takes: one flat list of points, each
   * carrying the rendition it belongs to and a millisecond timestamp.
   */
  const buffer = useMemo<BufferPoint[]>(() => {
    const points: BufferPoint[] = []
    for (const [variant, series] of Object.entries(data?.vpb ?? {})) {
      for (const point of series) {
        points.push({
          t: new Date(point.at).valueOf(),
          level: point.level_s,
          source: 'vpb',
          variant,
        })
      }
    }
    return points.sort((a, b) => a.t - b.t)
  }, [data])

  /*
   * The declared rate per rung, recovered by the backend from the rung identifier, which is
   * generated from the master playlist. A rung that declares none is null, so the bitrate
   * chart draws the measurement alone rather than a zero nobody measured.
   */
  const declared = data?.declared ?? {}

  const empty = snapshots.length === 0 && segments.length === 0

  /*
   * What a segment chart says instead of drawing nothing. The engine's own account is not
   * available here — the run finished days ago — so the account is the one this screen can
   * make: the range holds no segment, and the full range is one click away.
   */
  const reason =
    segments.length > 0
      ? undefined
      : range === 'full'
        ? 'This run stored none.'
        : 'The selected range holds none. Widen it to the full range to read the whole run.'

  return (
    <Card>
      <CardHeader
        title="Analytics"
        subtitle="The same charts Realtime draws, from the samples this run stored."
        actions={
          <SegmentedControl
            value={range}
            onChange={(next) => setRange(next as RangeId)}
            options={RANGES}
          />
        }
      />

      <div className="space-y-3 px-5 pb-5">
        {samples.isError && (
          <InlineAlert tone="error">
            {samples.error instanceof Error ? samples.error.message : String(samples.error)}
          </InlineAlert>
        )}

        {data?.downsampled && (
          <InlineAlert tone="info">
            This range holds more samples than a chart can draw, so the lines are thinned to{' '}
            {data.max_points} points each. The stored rows are unchanged — a download or the
            per-channel view reads all of them.
          </InlineAlert>
        )}

        {data?.player_note && <InlineAlert tone="info">{data.player_note}</InlineAlert>}

        {spikes.length > 0 && (
          <InlineAlert tone="warn">
            {spikes.length} rebuffering spike window(s) were measured for this channel in the
            week this run covers: {spikes.map((band) => band.label).join(', ')}.
          </InlineAlert>
        )}

        {empty && !samples.isPending ? (
          <p className="py-10 text-center text-small text-ink-muted">
            This run stored no playlist poll or segment fetch in the selected range.
          </p>
        ) : (
          /* Each chart carries its own card, title and empty state. They are rendered as
             they are — the aging screen and the Realtime screen draw the same figure. */
          <div className="grid gap-3 xl:grid-cols-2">
            <FetchTimingChart snapshots={snapshots} />
            <StatusHeatmapChart segments={segments} reason={reason} />
            <SequenceLadderChart snapshots={snapshots} />
            <DiscontinuityChart snapshots={snapshots} />
            <FreshnessChart snapshots={snapshots} />
            <DownloadRatioChart
              segments={segments}
              reason={reason}
              warn={thresholds.download_ratio_warn ?? DOWNLOAD_RATIO_WARN}
              error={thresholds.download_ratio_error ?? DOWNLOAD_RATIO_ERROR}
            />
            <BitrateChart segments={segments} declared={declared} reason={reason} />
            <DurationChart segments={segments} reason={reason} />
            <AvSkewChart
              segments={segments}
              reason={reason}
              criticalMs={thresholds.av_pts_delta_critical_ms ?? AV_SKEW_CRITICAL_MS}
            />
            <TrafficChart segments={segments} reason={reason} />
            <VirtualBufferChart points={buffer} playerSamples={[]} />
          </div>
        )}
      </div>
    </Card>
  )
}
