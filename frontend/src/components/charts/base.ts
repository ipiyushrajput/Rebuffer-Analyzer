/**
 * Shared ECharts configuration.
 *
 * One palette, one axis style, one tooltip style, so fifteen charts read as one system.
 * Series colours are paired with distinct marker shapes so the charts stay readable without
 * relying on colour alone.
 */

/** Categorical palette: brand blue first, then hues separable at small sizes. */
export const PALETTE = [
  '#1428A0',
  '#1f9d55',
  '#c2410c',
  '#7c3aed',
  '#0891b2',
  '#a16207',
  '#be185d',
  '#4d7c0f',
]

export const SEVERITY_COLOR: Record<string, string> = {
  CRITICAL: '#b4151b',
  ERROR: '#d4581a',
  WARN: '#9a6700',
  INFO: '#1f6feb',
  PASS: '#1a7f37',
}

export const SYMBOLS = ['circle', 'triangle', 'rect', 'diamond', 'roundRect', 'pin']

export const AXIS_LINE = { lineStyle: { color: '#cbd2e0' } }
export const SPLIT_LINE = { lineStyle: { color: '#eef1f6' } }

export const GRID = { left: 52, right: 18, top: 28, bottom: 36, containLabel: true }

/**
 * Options are built as plain objects rather than typed `EChartsOption` values: several of
 * the charts use callback formatters and a custom `renderItem`, whose parameter types the
 * library declares far more loosely than the values it actually passes.
 */
export type ChartOption = Record<string, unknown>

export function baseOption(overrides: ChartOption = {}): ChartOption {
  return {
    animation: false, // High-frequency updates repaint faster without transitions.
    color: PALETTE,
    grid: GRID,
    textStyle: { fontFamily: '"Space Grotesk", system-ui, sans-serif', fontSize: 11 },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'line' },
      backgroundColor: '#ffffff',
      borderColor: '#e4e7ef',
      borderWidth: 1,
      textStyle: { color: '#10131c', fontSize: 11 },
      confine: true,
    },
    legend: { type: 'scroll', top: 0, itemWidth: 12, itemHeight: 8, textStyle: { fontSize: 10 } },
    xAxis: {
      type: 'time',
      axisLine: AXIS_LINE,
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { hideOverlap: true },
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: SPLIT_LINE,
    },
    ...overrides,
  }
}

/** A horizontal reference line labelled with the threshold it draws. */
export function thresholdLine(value: number, label: string, color = '#b4151b') {
  return {
    silent: true,
    symbol: 'none',
    lineStyle: { color, type: 'dashed' as const, width: 1 },
    data: [{ yAxis: value, label: { formatter: label, position: 'insideEndTop' as const, fontSize: 10 } }],
  }
}

/** Shaded bands for stall intervals, drawn behind every series. */
export function stallBands(
  bands: { start: number; end: number | null }[],
  now: number,
  color = 'rgba(180, 21, 27, 0.12)',
) {
  return {
    silent: true,
    itemStyle: { color },
    data: bands.map((band) => [{ xAxis: band.start }, { xAxis: band.end ?? now }]),
  }
}
