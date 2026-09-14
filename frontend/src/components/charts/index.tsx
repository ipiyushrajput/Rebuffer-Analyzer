/**
 * The Realtime charts (§8.1).
 *
 * Each chart is a pure function of the samples the session has collected, so the same
 * components render a live session and a stored job in history mode.
 */

import ReactECharts from 'echarts-for-react'
import { useMemo } from 'react'
import { MSN_GAP_TOLERANCE, PLAYER_METRICS_NOTE } from '../../lib/constants'
import type {
  PlaylistSnapshotData,
  SegmentResultData,
} from '../../ws/messages'
import type { BufferPoint, PlayerSample, StallBand } from '../../store/session'
import { BRAND, PALETTE, SEVERITY_COLOR, baseOption, stallBands, thresholdLine } from './base'
import type { ChartOption } from './base'

const CHART_HEIGHT = 220

/**
 * One chart, with its title and the measurement note that explains the axis.
 *
 * `empty` names the measurement the chart is waiting on. A chart with no series drew an
 * empty grid, which reads the same whether the analyzer measured nothing or the chart
 * dropped the data on the floor; the message says which, so the operator can act on it.
 */
export function ChartCard({
  title,
  note,
  empty,
  children,
}: {
  title: string
  note?: string
  empty?: string
  children: React.ReactNode
}) {
  return (
    <section className="card">
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 pb-1 pt-3.5">
        <h3 className="text-body font-semibold text-ink">{title}</h3>
        {note && <span className="text-micro text-ink-faint">{note}</span>}
      </div>
      <div className="px-1.5 pb-2">
        {empty ? (
          <div
            className="flex items-center justify-center px-4 text-center text-small text-ink-muted"
            style={{ height: CHART_HEIGHT }}
          >
            {empty}
          </div>
        ) : (
          children
        )}
      </div>
    </section>
  )
}

const NO_SEGMENTS = 'No segment has been sampled yet. This chart draws one point per segment fetch.'
const NO_SNAPSHOTS = 'No playlist poll has completed yet.'
const NO_PLAYER =
  'The player on this host has reported no sample. A player error is shown beside the video.'

function Chart({ option }: { option: ChartOption }) {
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

function byVariant<T extends { variant: string }>(items: T[]): Map<string, T[]> {
  const map = new Map<string, T[]>()
  for (const item of items) {
    const list = map.get(item.variant)
    if (list) list.push(item)
    else map.set(item.variant, [item])
  }
  return map
}

// 1 — Player buffer with stall bands ----------------------------------------

export function PlayerBufferChart({
  samples,
  stalls,
}: {
  samples: PlayerSample[]
  stalls: StallBand[]
}) {
  const option = useMemo(() => {
    const now = Date.now()
    return baseOption({
      yAxis: { type: 'value', name: 'seconds', min: 0 },
      series: [
        {
          name: 'Player buffer',
          type: 'line',
          showSymbol: false,
          smooth: false,
          areaStyle: { opacity: 0.08 },
          data: samples.map((s) => [s.t, s.buffer_s ?? 0]),
          markArea: stallBands(stalls, now),
        },
      ],
    })
  }, [samples, stalls])
  return (
    <ChartCard
      title="1 · Player buffer"
      note={PLAYER_METRICS_NOTE}
      empty={samples.length === 0 ? NO_PLAYER : undefined}
    >
      <Chart option={option} />
    </ChartCard>
  )
}

// 2 — Rebuffer ratio over time ----------------------------------------------

export function RebufferRatioChart({
  stalls,
  startedAt,
  threshold,
}: {
  stalls: StallBand[]
  startedAt: number | null
  threshold: number
}) {
  const option = useMemo(() => {
    const start = startedAt ?? Date.now()
    const now = Date.now()
    const points: [number, number][] = []
    const stepMs = 5000
    for (let t = start; t <= now; t += stepMs) {
      const stalled = stalls.reduce((total, band) => {
        const end = Math.min(band.end ?? now, t)
        return total + Math.max(0, end - band.start) / 1000
      }, 0)
      const elapsed = (t - start) / 1000
      points.push([t, elapsed > 0 ? stalled / elapsed : 0])
    }
    return baseOption({
      yAxis: { type: 'value', name: 'ratio', min: 0, max: Math.max(threshold * 2, 0.5) },
      series: [
        {
          name: 'Rebuffer ratio',
          type: 'line',
          showSymbol: false,
          data: points,
          markLine: thresholdLine(threshold, `threshold ${threshold}`),
        },
      ],
    })
  }, [stalls, startedAt, threshold])
  return (
    <ChartCard title="2 · Rebuffer ratio" note={`threshold ${threshold}`}>
      <Chart option={option} />
    </ChartCard>
  )
}

// 3 — Played rung over time --------------------------------------------------

export function PlayedRungChart({ samples }: { samples: PlayerSample[] }) {
  const option = useMemo(
    () =>
      baseOption({
        yAxis: { type: 'value', name: 'kbit/s', min: 0 },
        series: [
          {
            name: 'Played bitrate',
            type: 'line',
            step: 'end',
            showSymbol: false,
            data: samples.filter((s) => s.bitrate).map((s) => [s.t, (s.bitrate ?? 0) / 1000]),
          },
        ],
      }),
    [samples],
  )
  return (
    <ChartCard
      title="3 · Played rung"
      note={PLAYER_METRICS_NOTE}
      empty={
        samples.some((s) => s.bitrate)
          ? undefined
          : `${NO_PLAYER} The rung is read from the player, so it needs playback to start.`
      }
    >
      <Chart option={option} />
    </ChartCard>
  )
}

// 4 — Segment download ratio per rung ---------------------------------------

export function DownloadRatioChart({
  segments,
  warn,
  error,
}: {
  segments: SegmentResultData[]
  warn: number
  error: number
}) {
  const option = useMemo(() => {
    const groups = byVariant(segments)
    const series = [...groups.entries()].map(([variant, items], index) => ({
      name: variant,
      type: 'scatter' as const,
      symbolSize: 6,
      itemStyle: { color: PALETTE[index % PALETTE.length] },
      data: items
        .filter((s) => s.declared_duration)
        .map((s) => [
          new Date(s.at).getTime(),
          s.download_ms / 1000 / (s.declared_duration || 1),
        ]),
    }))
    if (series.length > 0) {
      const first = series[0] as Record<string, unknown>
      first.markLine = {
        silent: true,
        symbol: 'none',
        data: [
          { yAxis: warn, lineStyle: { color: BRAND.violet, type: 'dashed' }, label: { formatter: `warn ${warn}` } },
          { yAxis: error, lineStyle: { color: BRAND.pink, type: 'dashed' }, label: { formatter: `error ${error}` } },
        ],
      }
    }
    return baseOption({ yAxis: { type: 'value', name: 'download / EXTINF', min: 0 }, series })
  }, [segments, warn, error])
  return (
    <ChartCard
      title="4 · Segment download ratio"
      note="download time divided by playback duration"
      empty={segments.length === 0 ? NO_SEGMENTS : undefined}
    >
      <Chart option={option} />
    </ChartCard>
  )
}

// 5 — TTFB and total fetch time ---------------------------------------------

export function FetchTimingChart({ snapshots }: { snapshots: PlaylistSnapshotData[] }) {
  const option = useMemo(
    () =>
      baseOption({
        yAxis: { type: 'value', name: 'ms', min: 0 },
        series: [
          {
            name: 'TTFB',
            type: 'line',
            showSymbol: false,
            data: snapshots.map((s) => [new Date(s.at).getTime(), s.ttfb_ms ?? 0]),
          },
          {
            name: 'Total',
            type: 'line',
            showSymbol: false,
            data: snapshots.map((s) => [new Date(s.at).getTime(), s.total_ms]),
          },
        ],
      }),
    [snapshots],
  )
  return (
    <ChartCard
      title="5 · Playlist fetch timing"
      note="per request"
      empty={snapshots.length === 0 ? NO_SNAPSHOTS : undefined}
    >
      <Chart option={option} />
    </ChartCard>
  )
}

// 6 — HTTP status timeline heatmap ------------------------------------------

export function StatusHeatmapChart({ segments }: { segments: SegmentResultData[] }) {
  const option = useMemo(() => {
    const variants = [...new Set(segments.map((s) => s.variant))]
    const data = segments.map((s) => [
      new Date(s.at).getTime(),
      variants.indexOf(s.variant),
      s.status,
    ])
    return baseOption({
      tooltip: { trigger: 'item', formatter: (p: { value: number[] }) => `HTTP ${p.value[2]}` },
      legend: { show: false },
      yAxis: {
        type: 'category',
        data: variants,
        axisLine: { show: false },
        axisTick: { show: false },
      },
      series: [
        {
          type: 'scatter',
          symbol: 'rect',
          symbolSize: [8, 14],
          data,
          itemStyle: {
            color: (p: { value: number[] }) =>
              p.value[2] >= 500
                ? SEVERITY_COLOR.CRITICAL
                : p.value[2] >= 400
                  ? SEVERITY_COLOR.ERROR
                  : p.value[2] >= 300
                    ? SEVERITY_COLOR.WARN
                    : SEVERITY_COLOR.PASS,
          },
        },
      ],
    })
  }, [segments])
  return (
    <ChartCard
      title="6 · HTTP status timeline"
      note="per rendition"
      empty={segments.length === 0 ? NO_SEGMENTS : undefined}
    >
      <Chart option={option} />
    </ChartCard>
  )
}

// 7 — Sequence ladder: MSN and DSN per variant ------------------------------

export function SequenceLadderChart({ snapshots }: { snapshots: PlaylistSnapshotData[] }) {
  const option = useMemo(() => {
    const groups = byVariant(snapshots)
    const series = [...groups.entries()].map(([variant, items], index) => ({
      name: variant,
      type: 'line' as const,
      showSymbol: false,
      lineStyle: { width: 1.5 },
      itemStyle: { color: PALETTE[index % PALETTE.length] },
      data: items.map((s) => [new Date(s.at).getTime(), s.last_msn ?? 0]),
    }))
    return baseOption({
      yAxis: { type: 'value', name: 'media sequence', scale: true },
      series,
    })
  }, [snapshots])

  const spread = useMemo(() => {
    const latest = new Map<string, number>()
    for (const snapshot of snapshots) {
      if (snapshot.last_msn != null) latest.set(snapshot.variant, snapshot.last_msn)
    }
    const values = [...latest.values()]
    return values.length > 1 ? Math.max(...values) - Math.min(...values) : 0
  }, [snapshots])

  return (
    <ChartCard
      title="7 · Sequence ladder"
      note={
        spread >= MSN_GAP_TOLERANCE
          ? `spread ${spread} — at or above the tolerance of ${MSN_GAP_TOLERANCE}`
          : `spread ${spread} — inside the tolerance of ${MSN_GAP_TOLERANCE}`
      }
      empty={snapshots.length === 0 ? NO_SNAPSHOTS : undefined}
    >
      <Chart option={option} />
    </ChartCard>
  )
}

export function DiscontinuityChart({ snapshots }: { snapshots: PlaylistSnapshotData[] }) {
  const option = useMemo(() => {
    const groups = byVariant(snapshots)
    const series = [...groups.entries()].map(([variant, items], index) => ({
      name: variant,
      type: 'line' as const,
      step: 'end' as const,
      showSymbol: false,
      itemStyle: { color: PALETTE[index % PALETTE.length] },
      data: items.map((s) => [new Date(s.at).getTime(), s.dsn ?? 0]),
    }))
    return baseOption({ yAxis: { type: 'value', name: 'discontinuity sequence', scale: true }, series })
  }, [snapshots])
  return (
    <ChartCard
      title="7b · Discontinuity sequence"
      note="one counter is shared by the Tizen player"
      empty={snapshots.length === 0 ? NO_SNAPSHOTS : undefined}
    >
      <Chart option={option} />
    </ChartCard>
  )
}

// 8 — Playlist freshness -----------------------------------------------------

export function FreshnessChart({ snapshots }: { snapshots: PlaylistSnapshotData[] }) {
  const option = useMemo(() => {
    const groups = byVariant(snapshots)
    const series = [...groups.entries()].map(([variant, items], index) => {
      let lastMsn: number | null = null
      let lastChange = items.length ? new Date(items[0].at).getTime() : 0
      const points: [number, number][] = []
      for (const item of items) {
        const t = new Date(item.at).getTime()
        if (item.last_msn !== lastMsn) {
          lastMsn = item.last_msn
          lastChange = t
        }
        points.push([t, (t - lastChange) / 1000])
      }
      return {
        name: variant,
        type: 'line' as const,
        showSymbol: false,
        itemStyle: { color: PALETTE[index % PALETTE.length] },
        data: points,
      }
    })
    return baseOption({ yAxis: { type: 'value', name: 'seconds since a new segment', min: 0 }, series })
  }, [snapshots])
  return (
    <ChartCard
      title="8 · Playlist freshness"
      note="age since the last new segment"
      empty={snapshots.length === 0 ? NO_SNAPSHOTS : undefined}
    >
      <Chart option={option} />
    </ChartCard>
  )
}

// 9 — Declared vs measured bitrate ------------------------------------------

export function BitrateChart({
  segments,
  declared,
}: {
  segments: SegmentResultData[]
  declared: Record<string, number | null>
}) {
  const option = useMemo(() => {
    const variants = [...new Set(segments.map((s) => s.variant))]
    const peaks = variants.map((variant) => {
      const values = segments
        .filter((s) => s.variant === variant && s.measured_kbps)
        .map((s) => s.measured_kbps as number)
      return values.length ? Math.max(...values) : 0
    })
    return baseOption({
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: variants, axisLabel: { rotate: 20 } },
      yAxis: { type: 'value', name: 'kbit/s', min: 0 },
      series: [
        {
          name: 'Declared BANDWIDTH',
          type: 'bar',
          data: variants.map((v) => (declared[v] ?? 0) / 1000),
        },
        { name: 'Measured peak', type: 'scatter', symbolSize: 12, symbol: 'triangle', data: peaks },
      ],
    })
  }, [segments, declared])
  return (
    <ChartCard
      title="9 · Declared vs measured bitrate"
      note="peak per rung"
      empty={segments.length === 0 ? NO_SEGMENTS : undefined}
    >
      <Chart option={option} />
    </ChartCard>
  )
}

// 10 — A/V skew --------------------------------------------------------------

export function AvSkewChart({
  segments,
  criticalMs,
}: {
  segments: SegmentResultData[]
  criticalMs: number
}) {
  const option = useMemo(() => {
    const groups = byVariant(segments.filter((s) => s.av_skew_ms != null))
    const series = [...groups.entries()].map(([variant, items], index) => ({
      name: variant,
      type: 'line' as const,
      showSymbol: true,
      symbolSize: 4,
      itemStyle: { color: PALETTE[index % PALETTE.length] },
      data: items.map((s) => [new Date(s.at).getTime(), s.av_skew_ms as number]),
    }))
    if (series.length > 0) {
      const first = series[0] as Record<string, unknown>
      first.markLine = {
        silent: true,
        symbol: 'none',
        data: [
          { yAxis: criticalMs, lineStyle: { color: BRAND.pink, type: 'dashed' } },
          { yAxis: -criticalMs, lineStyle: { color: BRAND.pink, type: 'dashed' } },
        ],
      }
    }
    return baseOption({ yAxis: { type: 'value', name: 'ms (audio − video)' }, series })
  }, [segments, criticalMs])
  return (
    <ChartCard
      title="10 · A/V skew per segment"
      note={`critical above ${criticalMs} ms`}
      empty={
        segments.length === 0
          ? NO_SEGMENTS
          : segments.some((s) => s.av_skew_ms != null)
            ? undefined
            : 'No sampled segment carries both an audio and a video PTS, so there is no skew to plot.'
      }
    >
      <Chart option={option} />
    </ChartCard>
  )
}

// 11 — EXTINF vs measured duration ------------------------------------------

export function DurationChart({ segments }: { segments: SegmentResultData[] }) {
  const option = useMemo(
    () =>
      baseOption({
        yAxis: { type: 'value', name: 'seconds', min: 0 },
        series: [
          {
            name: 'Declared EXTINF',
            type: 'line',
            showSymbol: false,
            data: segments.map((s) => [new Date(s.at).getTime(), s.declared_duration ?? 0]),
          },
          {
            name: 'Measured duration',
            type: 'line',
            showSymbol: false,
            data: segments
              .filter((s) => s.actual_duration != null)
              .map((s) => [new Date(s.at).getTime(), s.actual_duration as number]),
          },
        ],
      }),
    [segments],
  )
  return (
    <ChartCard
      title="11 · Segment duration"
      note="EXTINF against the media inside"
      empty={segments.length === 0 ? NO_SEGMENTS : undefined}
    >
      <Chart option={option} />
    </ChartCard>
  )
}

// 12 — Virtual Player Buffer -------------------------------------------------

export function VirtualBufferChart({
  points,
  playerSamples,
}: {
  points: BufferPoint[]
  playerSamples: PlayerSample[]
}) {
  const option = useMemo(() => {
    const groups = new Map<string, BufferPoint[]>()
    for (const point of points) {
      const list = groups.get(point.variant)
      if (list) list.push(point)
      else groups.set(point.variant, [point])
    }
    const series: Record<string, unknown>[] = [...groups.entries()].map(([variant, items], index) => ({
      name: `VPB ${variant}`,
      type: 'line',
      showSymbol: false,
      itemStyle: { color: PALETTE[index % PALETTE.length] },
      data: items.map((p) => [p.t, p.level]),
    }))
    series.push({
      name: 'hls.js buffer',
      type: 'line',
      showSymbol: false,
      lineStyle: { type: 'dashed', width: 2 },
      itemStyle: { color: BRAND.ink },
      data: playerSamples.map((s) => [s.t, s.buffer_s ?? 0]),
    })
    return baseOption({ yAxis: { type: 'value', name: 'seconds buffered', min: 0 }, series })
  }, [points, playerSamples])
  return (
    <ChartCard
      title="12 · Virtual Player Buffer"
      note="modelled Tizen player, overlaid with the real hls.js buffer"
      empty={
        points.length === 0 && playerSamples.length === 0
          ? 'The buffer model starts once the first segment of a rung has been measured.'
          : undefined
      }
    >
      <Chart option={option} />
    </ChartCard>
  )
}

// 13 — Downloads Gantt -------------------------------------------------------

export function DownloadsGanttChart({
  segments,
  onSelect,
}: {
  segments: SegmentResultData[]
  onSelect?: (segment: SegmentResultData) => void
}) {
  const variants = useMemo(() => [...new Set(segments.map((s) => s.variant))], [segments])
  const option = useMemo(() => {
    const data = segments.map((s) => {
      const end = new Date(s.at).getTime()
      return {
        value: [variants.indexOf(s.variant), end - s.download_ms, end, s.msn],
        itemStyle: {
          color: s.status >= 400 || s.status === 0 ? SEVERITY_COLOR.CRITICAL : PALETTE[0],
        },
      }
    })
    return baseOption({
      tooltip: {
        trigger: 'item',
        formatter: (p: { value: number[] }) =>
          `segment ${p.value[3]} · ${Math.round(p.value[2] - p.value[1])} ms`,
      },
      legend: { show: false },
      yAxis: { type: 'category', data: variants, axisLine: { show: false }, axisTick: { show: false } },
      series: [
        {
          type: 'custom',
          renderItem: (
            _params: unknown,
            apiRef: {
              value: (index: number) => number
              coord: (point: number[]) => number[]
              size: (value: number[]) => number[]
              style: () => Record<string, unknown>
            },
          ) => {
            const categoryIndex = apiRef.value(0)
            const start = apiRef.coord([apiRef.value(1), categoryIndex])
            const end = apiRef.coord([apiRef.value(2), categoryIndex])
            const height = (apiRef.size([0, 1]) as number[])[1] * 0.5
            return {
              type: 'rect',
              shape: {
                x: start[0],
                y: start[1] - height / 2,
                width: Math.max(2, end[0] - start[0]),
                height,
              },
              style: apiRef.style(),
            }
          },
          encode: { x: [1, 2], y: 0 },
          data,
        },
      ],
    })
  }, [segments, variants])

  return (
    <ChartCard
      title="13 · Downloads"
      note="each bar is one segment fetch; failures are red"
      empty={segments.length === 0 ? NO_SEGMENTS : undefined}
    >
      <ReactECharts
        option={option}
        style={{ height: CHART_HEIGHT, width: '100%' }}
        notMerge
        lazyUpdate
        onEvents={{
          click: (params: { dataIndex?: number }) => {
            if (onSelect && params.dataIndex != null) onSelect(segments[params.dataIndex])
          },
        }}
      />
    </ChartCard>
  )
}

// 14 — Traffic and bandwidth -------------------------------------------------

export function TrafficChart({ segments }: { segments: SegmentResultData[] }) {
  const option = useMemo(() => {
    const buckets = new Map<number, number>()
    for (const segment of segments) {
      const second = Math.floor(new Date(segment.at).getTime() / 1000) * 1000
      buckets.set(second, (buckets.get(second) ?? 0) + segment.bytes)
    }
    const points = [...buckets.entries()].sort((a, b) => a[0] - b[0])
    return baseOption({
      yAxis: [
        { type: 'value', name: 'bytes/s', min: 0 },
        { type: 'value', name: 'Mbit/s', min: 0, splitLine: { show: false } },
      ],
      series: [
        { name: 'Traffic', type: 'bar', data: points },
        {
          name: 'Bandwidth',
          type: 'line',
          yAxisIndex: 1,
          showSymbol: false,
          data: points.map(([t, value]) => [t, (value * 8) / 1_000_000]),
        },
      ],
    })
  }, [segments])
  return (
    <ChartCard
      title="14 · Traffic and bandwidth"
      note="received by the analyzer"
      empty={segments.length === 0 ? NO_SEGMENTS : undefined}
    >
      <Chart option={option} />
    </ChartCard>
  )
}

// 15 — Playlist state timeline ----------------------------------------------

const STATE_ORDER = ['LIVE', 'STALLED', 'HTTP_ERROR', 'LIVE_END', 'VOD', 'UNKNOWN']

export function PlaylistStateChart({
  transitions,
}: {
  transitions: { at: string; variant: string; state: string }[]
}) {
  const option = useMemo(() => {
    const variants = [...new Set(transitions.map((t) => t.variant))]
    return baseOption({
      tooltip: {
        trigger: 'item',
        formatter: (p: { value: [number, number, string] }) => p.value[2],
      },
      legend: { show: false },
      yAxis: {
        type: 'category',
        data: variants,
        axisLine: { show: false },
        axisTick: { show: false },
      },
      series: [
        {
          type: 'scatter',
          symbolSize: 12,
          data: transitions.map((t) => ({
            value: [new Date(t.at).getTime(), variants.indexOf(t.variant), t.state],
            itemStyle: {
              color:
                t.state === 'LIVE'
                  ? SEVERITY_COLOR.PASS
                  : t.state === 'STALLED'
                    ? SEVERITY_COLOR.ERROR
                    : t.state === 'HTTP_ERROR'
                      ? SEVERITY_COLOR.CRITICAL
                      : SEVERITY_COLOR.INFO,
            },
          })),
        },
      ],
    })
  }, [transitions])
  return (
    <ChartCard
      title="15 · Playlist state"
      note={STATE_ORDER.join(' · ')}
      empty={
        transitions.length === 0
          ? 'No playlist has changed state yet. Every rung has stayed in the state it started in.'
          : undefined
      }
    >
      <Chart option={option} />
    </ChartCard>
  )
}
