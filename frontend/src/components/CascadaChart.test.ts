/**
 * The two-week chart: the previous week's dates along the top, a week behind the bottom axis.
 *
 * The comparison week is drawn seven days late so it lies under this week, which is what makes
 * the overlay readable and is also why the bottom axis alone could not say which days the grey
 * line is. These pin the top axis to the same range as the bottom one — the property that
 * keeps the two a week apart at every zoom — and its labels to the moment a week earlier.
 */

import { describe, expect, it } from 'vitest'
import type { CascadaPoint } from '../api/client'
import {
  ABOVE_THRESHOLD,
  PREVIOUS_WEEK,
  THIS_WEEK,
  cascadaChartOption,
  noDataBands,
  tooltipLines,
  utcDay,
} from './CascadaChart'
import {
  WEEK_MS,
  drawnExtent,
  previousWeekTick,
  shifted,
  utcMinute,
  utcTick,
} from './charts/weekAxes'
import { formatChange, formatCompact, formatCount } from './ErrorDataModal'

const minute = (iso: string, value: number | null): CascadaPoint => ({ at: iso, value })

// This week starts Monday 21 September; the comparison week is the seven days before it.
const origin = [
  minute('2026-09-21T00:00:00+00:00', 881),
  minute('2026-09-21T00:01:00+00:00', 870),
  minute('2026-09-23T05:04:00+00:00', 534),
]
const comparison = [
  minute('2026-09-14T00:00:00+00:00', 1467),
  minute('2026-09-20T23:59:00+00:00', 700),
]

type Axis = {
  position: string
  min: number
  max: number
  axisLabel: { formatter: (value: number) => string }
}
type Series = { name: string; xAxisIndex: number; data: [number, number | null][] }

function build(overrides: Partial<Parameters<typeof cascadaChartOption>[0]> = {}) {
  const option = cascadaChartOption({
    origin,
    comparison,
    showComparison: true,
    threshold: null,
    yName: 'errors per minute',
    formatValue: (value) => `${value}`,
    ...overrides,
  })
  return {
    option,
    axes: option.xAxis as Axis[],
    series: option.series as Series[],
    zoom: option.dataZoom as { xAxisIndex: number[] }[],
  }
}

describe('the axis labels', () => {
  it('writes a UTC midnight as the date and any other tick as the time', () => {
    expect(utcTick(Date.UTC(2026, 8, 21, 0, 0))).toBe('09-21')
    expect(utcTick(Date.UTC(2026, 8, 21, 6, 0))).toBe('06:00')
    expect(utcMinute(Date.UTC(2026, 8, 21, 5, 4))).toBe('2026-09-21 05:04')
  })

  it('names the day a week earlier on the top axis', () => {
    // The position of Monday 21 September on the chart is Monday 14 September last week.
    expect(previousWeekTick(Date.UTC(2026, 8, 21, 0, 0))).toBe('09-14')
    expect(previousWeekTick(Date.UTC(2026, 8, 23, 12, 0))).toBe('12:00')
  })
})

describe('the range both axes share', () => {
  it('spans this week and the shifted comparison week together', () => {
    const extent = drawnExtent(origin, comparison, true)
    expect(extent).toEqual([
      new Date('2026-09-21T00:00:00+00:00').valueOf(),
      new Date('2026-09-20T23:59:00+00:00').valueOf() + WEEK_MS,
    ])
  })

  it('spans this week alone when the overlay is off', () => {
    expect(drawnExtent(origin, comparison, false)).toEqual([
      new Date('2026-09-21T00:00:00+00:00').valueOf(),
      new Date('2026-09-23T05:04:00+00:00').valueOf(),
    ])
  })

  it('has nothing to span with no point to draw', () => {
    expect(drawnExtent([], [], true)).toBeNull()
  })

  it('moves the comparison week forward exactly seven days', () => {
    expect(shifted(comparison)[0][0]).toBe(new Date('2026-09-21T00:00:00+00:00').valueOf())
  })
})

describe('the chart with the previous-week overlay', () => {
  it('draws a second axis along the top over the same range as the bottom one', () => {
    const { axes } = build()

    expect(axes.map((axis) => axis.position)).toEqual(['bottom', 'top'])
    // One range for both is what keeps them a week apart at every zoom level.
    expect(axes[1].min).toBe(axes[0].min)
    expect(axes[1].max).toBe(axes[0].max)
    expect(axes[0].axisLabel.formatter(axes[0].min)).toBe('09-21')
    expect(axes[1].axisLabel.formatter(axes[1].min)).toBe('09-14')
  })

  it('puts last week on the top axis and this week on the bottom one', () => {
    const { series } = build()
    const byName = Object.fromEntries(series.map((item) => [item.name, item]))

    expect(byName[PREVIOUS_WEEK].xAxisIndex).toBe(1)
    expect(byName[THIS_WEEK].xAxisIndex).toBe(0)
  })

  it('zooms both axes together', () => {
    const { zoom } = build()
    expect(zoom.map((item) => item.xAxisIndex)).toEqual([
      [0, 1],
      [0, 1],
    ])
  })

  it('runs in UTC, the clock every CASCADA time is stated in', () => {
    expect(build().option.useUTC).toBe(true)
  })
})

describe('the chart without the overlay', () => {
  it('draws one axis and no previous-week line', () => {
    const { axes, series, zoom } = build({ showComparison: false })

    expect(axes).toHaveLength(1)
    expect(series.map((item) => item.name)).not.toContain(PREVIOUS_WEEK)
    expect(zoom[0].xAxisIndex).toEqual([0])
  })

  it('draws one axis when there is no comparison week to show', () => {
    expect(build({ comparison: [] }).axes).toHaveLength(1)
  })
})

describe('the threshold', () => {
  it('marks nothing when no threshold is set', () => {
    const { series } = build({ threshold: null })
    const names = series.map((item) => item.name)

    expect(names).not.toContain(ABOVE_THRESHOLD)
    expect(series.find((item) => item.name === THIS_WEEK)).not.toHaveProperty('markLine')
  })

  it('draws the line and the breaches once a threshold is set', () => {
    const { series } = build({ threshold: 875 })
    const breaches = series.find((item) => item.name === ABOVE_THRESHOLD)

    expect(series.find((item) => item.name === THIS_WEEK)).toHaveProperty('markLine')
    expect(breaches?.data.map((point) => point[1])).toEqual([881, null, null])
  })
})

describe('the tooltip', () => {
  const at = (iso: string) => new Date(iso).valueOf()
  const lines = (position: number, threshold: number | null = null) =>
    tooltipLines({
      at: position,
      origin: new Map([
        [at('2026-09-21T00:00:00Z'), 881],
        [at('2026-09-21T00:01:00Z'), null],
      ]),
      comparison: new Map([[at('2026-09-14T00:00:00Z'), 1467]]),
      withComparison: true,
      threshold,
      formatValue: (value) => `${value} errors`,
    })

  it('names this week and the same minute a week earlier, whatever was drawn', () => {
    expect(lines(at('2026-09-21T00:00:00Z'))).toEqual([
      '2026-09-21 00:00 UTC',
      'This week: 881 errors',
      'Previous week (2026-09-14 00:00): 1467 errors',
    ])
  })

  it('reads the minute under the pointer, rounding a position between two minutes', () => {
    expect(lines(at('2026-09-21T00:00:20Z'))[0]).toBe('2026-09-21 00:00 UTC')
  })

  it('states a minute CASCADA reported nothing for as no measurement', () => {
    expect(lines(at('2026-09-21T00:01:00Z'))).toEqual([
      '2026-09-21 00:01 UTC',
      'This week: no measurement',
      'Previous week (2026-09-14 00:01): no measurement',
    ])
  })

  it('adds the breach only for a minute above the threshold', () => {
    expect(lines(at('2026-09-21T00:00:00Z'), 875)).toContain('Above threshold: 881 errors')
    expect(lines(at('2026-09-21T00:00:00Z'), 900)).toHaveLength(3)
  })
})

describe('how error figures are written', () => {
  it('groups thousands and keeps the places it is asked for', () => {
    expect(formatCount(231480)).toBe('231,480')
    expect(formatCount(643.04, 1)).toBe('643')
    expect(formatCount(535.94, 1)).toBe('535.9')
    expect(formatCount(null)).toBe('—')
  })

  it('shortens a total of a million or more and leaves a smaller one whole', () => {
    expect(formatCompact(5961234)).toBe('5.96M')
    expect(formatCompact(231480)).toBe('231,480')
    expect(formatCompact(null)).toBe('—')
  })

  it('writes a change with its sign', () => {
    expect(formatChange(12.345)).toBe('+12.3 %')
    expect(formatChange(-39.7)).toBe('-39.7 %')
    expect(formatChange(null)).toBe('—')
  })
})

describe('the historical chart, one value per UTC day', () => {
  const day = (d: number, value: number | null): CascadaPoint => ({
    at: `2026-09-${String(d).padStart(2, '0')}T00:00:00+00:00`,
    value,
  })
  const days = [day(16, 0.1), day(17, null), day(18, 0.3), day(19, 0.2), day(20, 0.26), day(21, null), day(22, 0.1)]
  const option = cascadaChartOption({
    origin: days,
    comparison: [],
    showComparison: true,
    threshold: 0.25,
    yName: 'rebuffering ratio (%)',
    formatValue: (value) => `${value} %`,
    granularity: 'day',
  })
  const series = option.series as {
    name: string
    showSymbol?: boolean
    markArea?: { data: { xAxis: number; name?: string }[][] }
    data: [number, number | null][]
  }[]
  const axes = option.xAxis as { min: number; max: number; minInterval: number }[]

  it('draws one axis, no comparison and no zoom', () => {
    expect(axes).toHaveLength(1)
    expect(option.dataZoom).toEqual([])
    expect(series.map((item) => item.name)).toEqual(['Rebuffering ratio', ABOVE_THRESHOLD])
  })

  it('puts a marker on every day and ticks on whole days, padded half a day either side', () => {
    expect(series[0].showSymbol).toBe(true)
    expect(axes[0].minInterval).toBe(24 * 60 * 60 * 1000)
    expect(axes[0].min).toBe(Date.UTC(2026, 8, 15, 12))
    expect(axes[0].max).toBe(Date.UTC(2026, 8, 22, 12))
  })

  it('leaves a null day as a gap and names it with a shaded no-data band', () => {
    expect(series[0].data[1][1]).toBeNull()
    const bands = series[0].markArea?.data ?? []
    expect(bands).toHaveLength(2)
    expect(bands[0][0].name).toBe('no data')
    expect(noDataBands(days)).toEqual(bands)
  })

  it('marks the days above the threshold for the red series and nothing else', () => {
    expect(series[1].data.map((point) => point[1])).toEqual([null, null, 0.3, null, 0.26, null, null])
  })

  it('reads the day under the pointer and says no data for an empty one', () => {
    const at = (d: number) => Date.UTC(2026, 8, d)
    const lines = (position: number) =>
      tooltipLines({
        at: position,
        origin: new Map(days.map((point) => [new Date(point.at).valueOf(), point.value])),
        comparison: new Map(),
        withComparison: false,
        threshold: 0.25,
        formatValue: (value) => `${value} %`,
        granularity: 'day',
      })

    expect(lines(at(18) + 5 * 60 * 60 * 1000)).toEqual([
      '2026-09-18 UTC',
      'Rebuffering ratio: 0.3 %',
      'Above threshold: 0.3 %',
    ])
    expect(lines(at(17))).toEqual(['2026-09-17 UTC', 'Rebuffering ratio: no data'])
    expect(utcDay(at(22))).toBe('2026-09-22')
  })
})
