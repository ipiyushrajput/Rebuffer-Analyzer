/**
 * One CASCADA metric per minute: this week, last week behind it, and the breaches over it.
 *
 * Rebuffering and errors are drawn by this one chart. They differ in what the value is — a
 * percentage of viewing time, a count of errors — and in whether there is a threshold at all,
 * so both come in as props and nothing else changes between them.
 *
 * The chart holds a week of per-minute data, so the series is handed to ECharts whole and
 * downsampled for drawing alone; the statistics and the downloads read the same raw points.
 *
 * The historical source is one value per UTC day, and draws on this same chart with
 * `granularity="day"`: a marker per day, ticks on whole days, no zoom, and a day with no value
 * drawn as a gap with a shaded "no data" band over it — never as a zero.
 */

import ReactECharts from 'echarts-for-react'
import { useMemo } from 'react'
import type { CascadaPoint } from '../api/client'
import { AXIS_LINE, BRAND, PALETTE, baseOption, thresholdLine } from './charts/base'
import type { ChartOption } from './charts/base'
import {
  WEEK_MS,
  drawnExtent,
  pairs,
  previousWeekTick,
  shifted,
  utcMinute,
  utcTick,
} from './charts/weekAxes'

/** The comparison week is drawn in the neutral axis grey, so it never reads as a severity. */
export const COMPARISON_COLOR = '#98A0B4'
/** This week's axis labels take a tint of this week's line, so each axis names its line. */
const THIS_WEEK_LABEL = PALETTE[4]

export const CHART_HEIGHT = 340

export const THIS_WEEK = 'This week'
export const PREVIOUS_WEEK = 'Previous week'
export const ABOVE_THRESHOLD = 'Above threshold'

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

export type Granularity = 'minute' | 'day'

const DAY_MS = 24 * 60 * 60 * 1000

/** A UTC day as `YYYY-MM-DD`. */
export function utcDay(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10)
}

/**
 * The days with no value, as shaded bands a day wide.
 *
 * A null day is a gap in the line already; the band is what names it, so an empty stretch
 * reads as "CASCADA has nothing for this day" and not as a line that stopped drawing.
 */
export function noDataBands(points: CascadaPoint[]): { xAxis: number; name?: string }[][] {
  return points
    .filter((point) => point.value === null)
    .map((point) => {
      const at = new Date(point.at).valueOf()
      return [{ xAxis: at - DAY_MS / 2, name: 'no data' }, { xAxis: at + DAY_MS / 2 }]
    })
}

export interface CascadaChartProps {
  origin: CascadaPoint[]
  comparison: CascadaPoint[]
  showComparison: boolean
  /** Null draws no threshold line and no breach series: nothing is marked without a number. */
  threshold: number | null
  thresholdLabel?: string
  /** The y axis name, which states the unit. */
  yName: string
  /** How one value is written in the tooltip. */
  formatValue: (value: number) => string
  /** 'day' for the historical source's one value per UTC day. */
  granularity?: Granularity
}

const MINUTE_MS = 60 * 1000

/** Each measured minute by its millisecond timestamp, for a lookup by position. */
function byMinute(points: CascadaPoint[]): Map<number, number | null> {
  return new Map(points.map((point) => [new Date(point.at).valueOf(), point.value]))
}

/**
 * What the tooltip says at one position on the axis: this week's minute there, and the
 * previous week's minute exactly seven days before it.
 *
 * Both are read from the raw series by timestamp, not from the points ECharts hands the
 * formatter. Those are the points `lttb` kept for drawing, and each series keeps its own, so
 * the nearest drawn point of one week and of the other can be minutes apart — the tooltip
 * would name two moments that are not a week apart, and neither of them the one under the
 * pointer.
 */
export function tooltipLines({
  at,
  origin,
  comparison,
  withComparison,
  threshold,
  formatValue,
  granularity = 'minute',
}: {
  at: number
  origin: Map<number, number | null>
  comparison: Map<number, number | null>
  withComparison: boolean
  threshold: number | null
  formatValue: (value: number) => string
  granularity?: Granularity
}): string[] {
  const step = granularity === 'day' ? DAY_MS : MINUTE_MS
  const minute = Math.round(at / step) * step
  const absent = granularity === 'day' ? 'no data' : 'no measurement'
  const written = (value: number | null | undefined) =>
    value === null || value === undefined ? absent : formatValue(value)

  const current = origin.get(minute)
  const lines =
    granularity === 'day'
      ? [`${utcDay(minute)} UTC`, `Rebuffering ratio: ${written(current)}`]
      : [`${utcMinute(minute)} UTC`, `${THIS_WEEK}: ${written(current)}`]
  if (withComparison) {
    const before = minute - WEEK_MS
    lines.push(`${PREVIOUS_WEEK} (${utcMinute(before)}): ${written(comparison.get(before))}`)
  }
  // A minute at or below the threshold is not a breach; saying so on every hover is noise.
  if (threshold !== null && current !== null && current !== undefined && current > threshold) {
    lines.push(`${ABOVE_THRESHOLD}: ${formatValue(current)}`)
  }
  return lines
}

type TooltipRow = { axisValue?: number | string }

/** The option the chart draws, kept pure so the two-axis layout can be asserted directly. */
export function cascadaChartOption({
  origin,
  comparison,
  showComparison,
  threshold,
  thresholdLabel,
  yName,
  formatValue,
  granularity = 'minute',
}: CascadaChartProps): ChartOption {
  const daily = granularity === 'day'
  const withComparison = !daily && showComparison && comparison.length > 0
  const extent = drawnExtent(origin, comparison, withComparison)
  // Both axes take the same range, which is what keeps them a week apart at every zoom. A
  // daily chart is padded half a day either side so the first and last markers are whole.
  const range = extent
    ? daily
      ? { min: extent[0] - DAY_MS / 2, max: extent[1] + DAY_MS / 2, minInterval: DAY_MS }
      : { min: extent[0], max: extent[1] }
    : {}
  const axisBase = {
    type: 'time',
    ...range,
    axisLine: AXIS_LINE,
    axisTick: { show: false },
    splitLine: { show: false },
  }

  const xAxis: Record<string, unknown>[] = [
    {
      ...axisBase,
      position: 'bottom',
      axisLabel: { hideOverlap: true, color: THIS_WEEK_LABEL, formatter: utcTick },
      axisPointer: { snap: false },
    },
  ]
  if (withComparison) {
    xAxis.push({
      ...axisBase,
      position: 'top',
      axisLabel: { hideOverlap: true, color: COMPARISON_COLOR, formatter: previousWeekTick },
      axisPointer: { snap: false },
    })
  }
  const zoomed = withComparison ? [0, 1] : [0]
  const originAt = byMinute(origin)
  const comparisonAt = byMinute(comparison)

  const series: Record<string, unknown>[] = []
  if (withComparison) {
    series.push({
      name: PREVIOUS_WEEK,
      type: 'line',
      xAxisIndex: 1,
      showSymbol: false,
      sampling: 'lttb',
      lineStyle: { width: 1, color: COMPARISON_COLOR },
      itemStyle: { color: COMPARISON_COLOR },
      data: shifted(comparison),
      z: 1,
    })
  }

  const gaps = daily ? noDataBands(origin) : []
  series.push({
    name: daily ? 'Rebuffering ratio' : THIS_WEEK,
    type: 'line',
    xAxisIndex: 0,
    showSymbol: daily,
    symbolSize: daily ? 7 : undefined,
    // The whole series is handed over; `lttb` thins it for drawing only. Seven days need none.
    sampling: daily ? undefined : 'lttb',
    lineStyle: { width: 1.5, color: BRAND.blue },
    itemStyle: { color: BRAND.blue },
    data: pairs(origin),
    z: 2,
    ...(threshold !== null
      ? { markLine: thresholdLine(threshold, thresholdLabel ?? `threshold ${threshold}`) }
      : {}),
    ...(gaps.length > 0
      ? {
          markArea: {
            silent: true,
            itemStyle: { color: 'rgba(152, 160, 180, 0.12)' },
            label: { position: 'insideTop', color: COMPARISON_COLOR, fontSize: 10 },
            data: gaps,
          },
        }
      : {}),
  })

  if (threshold !== null) {
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
      name: ABOVE_THRESHOLD,
      type: 'line',
      xAxisIndex: 0,
      showSymbol: true,
      symbolSize: daily ? 9 : 3,
      lineStyle: { width: 1.5, color: BRAND.pink },
      itemStyle: { color: BRAND.pink },
      data: aboveOnly(origin, threshold),
      z: 3,
    })
  }

  return baseOption({
    useUTC: true,
    grid: {
      left: 58,
      right: 20,
      top: withComparison ? 58 : 34,
      bottom: daily ? 24 : 58,
      containLabel: true,
    },
    xAxis,
    yAxis: {
      type: 'value',
      name: yName,
      nameLocation: 'middle',
      nameRotate: 90,
      nameGap: 48,
      nameTextStyle: { color: COMPARISON_COLOR, fontSize: 10 },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: COMPARISON_COLOR },
      splitLine: { lineStyle: { color: '#EEF0F6' } },
    },
    // The two x axes cover the same values, so one pointer position is one minute on both.
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
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
        const rows = (Array.isArray(params) ? params : [params]) as TooltipRow[]
        const position = rows.find((row) => row.axisValue !== undefined)?.axisValue
        const at = typeof position === 'number' ? position : Number(position)
        if (!Number.isFinite(at)) return ''
        return tooltipLines({
          at,
          origin: originAt,
          comparison: comparisonAt,
          withComparison,
          threshold,
          formatValue,
          granularity,
        }).join('<br/>')
      },
    },
    // Seven daily points need no zoom.
    dataZoom: daily
      ? []
      : [
          { type: 'inside', xAxisIndex: zoomed, throttle: 50 },
          {
            type: 'slider',
            xAxisIndex: zoomed,
            height: 18,
            bottom: 12,
            borderColor: '#E6E8F0',
            labelFormatter: (value: number) => utcMinute(value),
          },
        ],
    series,
  })
}

export function CascadaChart(props: CascadaChartProps) {
  const {
    origin,
    comparison,
    showComparison,
    threshold,
    thresholdLabel,
    yName,
    formatValue,
    granularity,
  } = props
  const option = useMemo(
    () =>
      cascadaChartOption({
        origin,
        comparison,
        showComparison,
        threshold,
        thresholdLabel,
        yName,
        formatValue,
        granularity,
      }),
    [
      origin,
      comparison,
      showComparison,
      threshold,
      thresholdLabel,
      yName,
      formatValue,
      granularity,
    ],
  )

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

/** Which axis names which week, stated under the chart rather than left to colour. */
export function WeekAxesNote({ showComparison }: { showComparison: boolean }) {
  return (
    <>
      Bottom axis: this week&apos;s dates{showComparison && '; top axis: the previous week’s dates for the grey line'}. All times UTC.
    </>
  )
}
