/**
 * Shared ECharts configuration.
 *
 * One palette, one axis style, one tooltip style, so fifteen charts read as one system.
 * Series colours are paired with distinct marker shapes so the charts stay readable without
 * relying on colour alone.
 */

/**
 * Categorical palette: the brand spectrum first — Samsung blue, violet, TV Plus pink — then
 * the clean green and tints of the same three hues. No colour outside the brand range enters
 * a chart.
 */
export const PALETTE = [
  '#1428A0',
  '#7B2CBF',
  '#FF2D55',
  '#12864C',
  '#4B63D6',
  '#A66CE0',
  '#FF7A96',
  '#3FAE7C',
]

export const SEVERITY_COLOR: Record<string, string> = {
  CRITICAL: '#FF2D55',
  ERROR: '#E01142',
  WARN: '#7B2CBF',
  INFO: '#1428A0',
  PASS: '#12864C',
}

/** Named brand values the individual charts draw threshold lines and markers with. */
export const BRAND = {
  blue: '#1428A0',
  violet: '#7B2CBF',
  pink: '#FF2D55',
  clean: '#12864C',
  ink: '#0B1020',
}

export const SYMBOLS = ['circle', 'triangle', 'rect', 'diamond', 'roundRect', 'pin']

export const AXIS_LINE = { lineStyle: { color: '#D4D8E6' } }
export const SPLIT_LINE = { lineStyle: { color: '#EEF0F6' } }

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
    // Axis labels and tooltips carry measurements, so they are set in the mono face.
    textStyle: { fontFamily: '"JetBrains Mono", "Roboto Mono", monospace', fontSize: 11 },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'line', lineStyle: { color: '#D4D8E6' } },
      backgroundColor: '#ffffff',
      borderColor: '#E6E8F0',
      borderWidth: 1,
      padding: [8, 10],
      extraCssText: 'box-shadow: 0 8px 24px rgba(11,16,32,0.10); border-radius: 8px;',
      textStyle: { color: '#0B1020', fontSize: 11 },
      confine: true,
    },
    legend: {
      type: 'scroll',
      top: 0,
      itemWidth: 10,
      itemHeight: 8,
      icon: 'roundRect',
      textStyle: { fontSize: 10, color: '#667085' },
    },
    xAxis: {
      type: 'time',
      axisLine: AXIS_LINE,
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { hideOverlap: true, color: '#98A0B4' },
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: '#98A0B4' },
      splitLine: SPLIT_LINE,
    },
    ...overrides,
  }
}

/** A horizontal reference line labelled with the threshold it draws. */
export function thresholdLine(value: number, label: string, color = BRAND.pink) {
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
  color = 'rgba(255, 45, 85, 0.12)',
) {
  return {
    silent: true,
    itemStyle: { color },
    data: bands.map((band) => [{ xAxis: band.start }, { xAxis: band.end ?? now }]),
  }
}
