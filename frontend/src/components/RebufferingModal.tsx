/**
 * Rebuffering Data: one channel's measured week, and the week before it.
 *
 * Every figure on this panel comes from the current week's rows. The previous week is drawn
 * behind them as a comparison and never enters an average, a maximum or a threshold check —
 * mixing the two would average a fortnight into a number labelled as this week.
 *
 * The chart holds a week of per-minute data, so the series is handed to ECharts whole and
 * downsampled for drawing alone; the statistics and the downloads read the same raw points.
 */

import ReactECharts from 'echarts-for-react'
import { useEffect, useMemo } from 'react'
import {
  type CascadaChannel,
  type CascadaChannelQuery,
  type CascadaPoint,
  endpoints,
} from '../api/client'
import { BRAND, baseOption, thresholdLine } from './charts/base'
import type { ChartOption } from './charts/base'
import { CardHeader, InlineAlert, MetricRow, MetricTile } from './ui'
import { IconAging, IconClose, IconDownload, IconPulse } from './ui/icons'

/** The comparison week is drawn in the neutral axis grey, so it never reads as a severity. */
const COMPARISON_COLOR = '#98A0B4'

const CHART_HEIGHT = 320

export function formatPct(value: number | null | undefined, digits = 3): string {
  return value === null || value === undefined ? '—' : `${value.toFixed(digits)} %`
}

/** A UTC timestamp as the product writes them, with the viewer's own clock on hover. */
export function utcLabel(iso: string | null): string {
  if (!iso) return '—'
  return iso.replace('T', ' ').replace('+00:00', '').slice(0, 19)
}

function localTitle(iso: string | null): string {
  if (!iso) return ''
  const parsed = new Date(iso)
  return Number.isNaN(parsed.valueOf()) ? '' : `Local time: ${parsed.toLocaleString()}`
}

function pairs(points: CascadaPoint[]): [number, number | null][] {
  return points.map((point) => [new Date(point.at).valueOf(), point.value])
}

/**
 * The same minutes with everything at or below the threshold blanked out.
 *
 * Drawing the breaches as their own series is what puts them in pink and, more usefully, in
 * the legend: the reader is told which minutes are above threshold rather than left to infer
 * it from a colour. A blanked minute is a gap, so the pink only ever covers real breaches.
 */
export function aboveOnly(points: CascadaPoint[], threshold: number): [number, number | null][] {
  return points.map((point) => [
    new Date(point.at).valueOf(),
    point.value !== null && point.value > threshold ? point.value : null,
  ])
}

/**
 * The comparison week shifted forward by seven days.
 *
 * Laying last week under this week only reads if the two share an axis, so the previous
 * week's points are drawn at the time they correspond to rather than the time they happened;
 * the tooltip states which series a value came from.
 */
const WEEK_MS = 7 * 24 * 60 * 60 * 1000

function shifted(points: CascadaPoint[]): [number, number | null][] {
  return points.map((point) => [new Date(point.at).valueOf() + WEEK_MS, point.value])
}

export function RebufferingChart({
  channel,
  showComparison,
}: {
  channel: CascadaChannel
  showComparison: boolean
}) {
  const option = useMemo<ChartOption>(() => {
    const threshold = channel.threshold_pct
    const series: Record<string, unknown>[] = []

    if (showComparison && channel.comparison.length > 0) {
      series.push({
        name: 'Previous week',
        type: 'line',
        showSymbol: false,
        sampling: 'lttb',
        lineStyle: { width: 1, color: COMPARISON_COLOR },
        itemStyle: { color: COMPARISON_COLOR },
        data: shifted(channel.comparison),
        z: 1,
      })
    }

    series.push({
      name: 'This week',
      type: 'line',
      showSymbol: false,
      // The whole series is handed over; `lttb` thins it for drawing only.
      sampling: 'lttb',
      lineStyle: { width: 1.5, color: BRAND.blue },
      itemStyle: { color: BRAND.blue },
      data: pairs(channel.origin),
      z: 2,
      markLine: thresholdLine(threshold, `threshold ${threshold} %`),
    })

    /*
     * The breaches, drawn over the week in pink.
     *
     * A second series rather than a `visualMap`: colouring one series by value needs a
     * piecewise map over the line, which ECharts renders by walking coordinates the sampled
     * line no longer has. Two series need no mapping at all, and the legend then names the
     * pink instead of leaving colour to carry the meaning on its own. The symbols are on so
     * a single minute's spike, which has no neighbour to draw a segment to, is still visible.
     */
    series.push({
      name: 'Above threshold',
      type: 'line',
      showSymbol: true,
      symbolSize: 3,
      lineStyle: { width: 1.5, color: BRAND.pink },
      itemStyle: { color: BRAND.pink },
      data: aboveOnly(channel.origin, threshold),
      z: 3,
    })

    return baseOption({
      grid: { left: 58, right: 20, top: 30, bottom: 58, containLabel: true },
      yAxis: {
        type: 'value',
        name: 'rebuffering ratio (%)',
        nameTextStyle: { color: '#98A0B4', fontSize: 10, align: 'left' },
        nameGap: 12,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: '#98A0B4' },
        splitLine: { lineStyle: { color: '#EEF0F6' } },
      },
      tooltip: {
        trigger: 'axis',
        confine: true,
        backgroundColor: '#ffffff',
        borderColor: '#E6E8F0',
        borderWidth: 1,
        padding: [8, 10],
        textStyle: { color: '#0B1020', fontSize: 11 },
        extraCssText: 'box-shadow: 0 8px 24px rgba(11,16,32,0.10); border-radius: 8px;',
        formatter: (params: unknown) => {
          const rows = Array.isArray(params) ? params : [params]
          const first = rows[0] as { value?: [number, number | null] } | undefined
          const at = first?.value?.[0]
          const head = at ? new Date(at).toISOString().replace('T', ' ').slice(0, 16) : ''
          const lines = rows
            .map((row) => row as { seriesName?: string; value?: [number, number | null] })
            // A minute below the threshold is simply not in the breach series; saying so on
            // every hover would be noise. A gap in a measured series is worth stating.
            .filter(
              (point) =>
                point.seriesName !== 'Above threshold' ||
                (point.value?.[1] !== null && point.value?.[1] !== undefined),
            )
            .map((point) => {
              const value = point.value?.[1]
              const shown = value === null || value === undefined ? 'no measurement' : `${value} %`
              return `${point.seriesName}: ${shown}`
            })
            .join('<br/>')
          return `${head} UTC<br/>${lines}`
        },
      },
      dataZoom: [
        { type: 'inside', throttle: 50 },
        { type: 'slider', height: 18, bottom: 12, borderColor: '#E6E8F0' },
      ],
      series,
    })
  }, [channel, showComparison])

  return (
    <ReactECharts
      option={option}
      style={{ height: CHART_HEIGHT, width: '100%' }}
      notMerge
      lazyUpdate
      opts={{ renderer: 'canvas' }}
    />
  )
}

interface Props {
  query: CascadaChannelQuery
  channel: CascadaChannel | null
  loading: boolean
  error: string | null
  showComparison: boolean
  onToggleComparison: (next: boolean) => void
  onClose: () => void
  onAnalyse: (target: 'realtime' | 'aging') => void
  analysable: boolean
}

export function RebufferingModal({
  query,
  channel,
  loading,
  error,
  showComparison,
  onToggleComparison,
  onClose,
  onAnalyse,
  analysable,
}: Props) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const delta = channel?.week_over_week_delta_pct ?? null

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-ink/40 p-4 sm:p-8"
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Rebuffering data for ${query.channel_name}`}
        className="card w-full max-w-5xl"
      >
        <CardHeader
          title={query.channel_name}
          subtitle={
            channel ? (
              <span title={localTitle(channel.window.start)}>
                <span className="font-mono">{query.service_id}</span>
                {query.country && ` · ${query.country}`} ·{' '}
                <span className="font-mono">
                  {utcLabel(channel.window.start)} → {utcLabel(channel.window.end)} UTC
                </span>{' '}
                ({channel.window.days.toFixed(1)} days)
              </span>
            ) : (
              <span className="font-mono">{query.service_id}</span>
            )
          }
          actions={
            <>
              {channel && (
                <span className={channel.above_threshold ? 'chip-pink' : 'chip-clean'}>
                  {channel.above_threshold ? 'Above threshold' : 'Below threshold'}
                </span>
              )}
              <button
                type="button"
                className="btn-ghost btn-sm"
                onClick={onClose}
                aria-label="Close"
              >
                <IconClose size={14} />
                Close
              </button>
            </>
          }
        />

        <div className="space-y-4 px-5 pb-5">
          {error && <InlineAlert tone="error">{error}</InlineAlert>}

          {loading && !channel && (
            <p className="px-1 py-12 text-center text-small text-ink-muted">
              Reading this channel's rebuffering data from CASCADA…
            </p>
          )}

          {channel && (
            <>
              {channel.truncated && (
                <InlineAlert tone="warn">
                  CASCADA returned {channel.minutes_counted} measured minute(s) for a window of{' '}
                  {channel.window.minutes}, so these figures cover less than the window above.
                </InlineAlert>
              )}

              <MetricRow>
                <MetricTile
                  label="Average"
                  value={formatPct(channel.average_pct)}
                  tone={channel.above_threshold ? 'pink' : 'clean'}
                  note={`threshold ${channel.threshold_pct} %`}
                />
                <MetricTile
                  label="Maximum"
                  value={formatPct(channel.max_pct)}
                  tone="violet"
                  note={utcLabel(channel.max_at)}
                />
                <MetricTile
                  label="Minutes above"
                  value={channel.minutes_above}
                  tone={channel.minutes_above > 0 ? 'violet' : 'default'}
                  note={`of ${channel.minutes_counted} measured`}
                />
                <MetricTile
                  label="Time above"
                  value={
                    channel.percent_time_above === null
                      ? '—'
                      : `${channel.percent_time_above.toFixed(2)} %`
                  }
                  note="of the measured week"
                />
                <MetricTile
                  label="Previous week"
                  value={formatPct(channel.previous_week_average_pct)}
                  note="average, for comparison"
                />
                <MetricTile
                  label="Week on week"
                  value={
                    delta === null
                      ? '—'
                      : `${delta > 0 ? '+' : ''}${delta.toFixed(3)}`
                  }
                  suffix="pp"
                  tone={delta === null ? 'default' : delta > 0 ? 'pink' : 'clean'}
                  note={
                    delta === null ? 'no comparison week' : delta > 0 ? 'worse than last week' : 'better than last week'
                  }
                />
              </MetricRow>

              <div className="card">
                <div className="flex flex-wrap items-center justify-between gap-2 px-4 pb-1 pt-3.5">
                  <h3 className="text-body font-semibold text-ink">Rebuffering ratio per minute</h3>
                  <label className="flex items-center gap-2 text-small text-ink-soft">
                    <input
                      type="checkbox"
                      className="h-4 w-4 accent-brand-600"
                      checked={showComparison}
                      onChange={(e) => onToggleComparison(e.target.checked)}
                      disabled={channel.comparison.length === 0}
                    />
                    Previous week overlay
                  </label>
                </div>
                <div className="px-1.5 pb-2">
                  {channel.origin.length === 0 ? (
                    <p
                      className="flex items-center justify-center px-4 text-center text-small text-ink-muted"
                      style={{ height: CHART_HEIGHT }}
                    >
                      CASCADA measured no minute of this channel inside the window.
                    </p>
                  ) : (
                    <RebufferingChart channel={channel} showComparison={showComparison} />
                  )}
                </div>
                <p className="px-4 pb-3 text-micro text-ink-faint">
                  Minutes above {channel.threshold_pct} % are drawn in pink; the rest in blue. A
                  minute CASCADA reported nothing for is a gap, not a zero —{' '}
                  {channel.minutes_missing} such minute(s) in this window.
                </p>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <a
                  className="btn-ghost btn-sm"
                  href={endpoints.cascadaChannelReportUrl(query, 'csv')}
                >
                  <IconDownload size={13} />
                  Download CSV
                </a>
                <a
                  className="btn-ghost btn-sm"
                  href={endpoints.cascadaChannelReportUrl(query, 'xlsx')}
                >
                  <IconDownload size={13} />
                  Download XLSX
                </a>
                <span className="flex-1" />
                <button
                  type="button"
                  className="btn-ghost btn-sm"
                  disabled={!analysable}
                  title={
                    analysable
                      ? 'Open Realtime with this channel filled in'
                      : 'The catalogue lists no playback URL for this channel'
                  }
                  onClick={() => onAnalyse('realtime')}
                >
                  <IconPulse size={13} />
                  Realtime
                </button>
                <button
                  type="button"
                  className="btn-ghost btn-sm"
                  disabled={!analysable}
                  title={
                    analysable
                      ? 'Open Aging with this channel filled in'
                      : 'The catalogue lists no playback URL for this channel'
                  }
                  onClick={() => onAnalyse('aging')}
                >
                  <IconAging size={13} />
                  Aging
                </button>
              </div>

              <p className="text-micro text-ink-faint">
                Measured at <span className="font-mono">{utcLabel(channel.fetched_at)}</span> UTC
                {channel.cached && ' · served from the stored window'}. CASCADA is queried with
                channel_country=ALL, so this is the channel's rebuffering across every country it
                runs in.
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
