/**
 * Two weeks on one time axis, with each week's own dates.
 *
 * The CASCADA charts lay last week under this week, which only reads if the two share an
 * axis, so the comparison points are drawn seven days later than they happened. That leaves
 * the bottom axis naming this week's dates only, and a reader has no way to see which days
 * the grey line is. A second axis along the top, with the same range and the same ticks,
 * names the tick seven days earlier: every vertical position then carries both of its dates.
 *
 * Both axes run in UTC. The product states every CASCADA time in UTC and the window starts at
 * UTC midnight, so a local-time axis would put the day boundaries at a different place from
 * the window they divide — five and a half hours off in India.
 */

import type { CascadaPoint } from '../../api/client'

export const WEEK_MS = 7 * 24 * 60 * 60 * 1000

const pad = (value: number) => String(value).padStart(2, '0')

/** A millisecond timestamp as `YYYY-MM-DD HH:mm`, in UTC. */
export function utcMinute(ms: number): string {
  return new Date(ms).toISOString().replace('T', ' ').slice(0, 16)
}

/**
 * One tick label: the date at a UTC midnight, the time of day everywhere else.
 *
 * ECharts lands time-axis ticks on whole days and whole hours, so a week reads as a date at
 * each day boundary and hours between them, which is what its own default does too.
 */
export function utcTick(ms: number): string {
  const at = new Date(ms)
  const hours = at.getUTCHours()
  const minutes = at.getUTCMinutes()
  if (hours === 0 && minutes === 0) return `${pad(at.getUTCMonth() + 1)}-${pad(at.getUTCDate())}`
  return `${pad(hours)}:${pad(minutes)}`
}

/** The label the top axis prints at a position: the moment a week before it. */
export function previousWeekTick(ms: number): string {
  return utcTick(ms - WEEK_MS)
}

/** Points as `[ms, value]` pairs at the moment they were measured. */
export function pairs(points: CascadaPoint[]): [number, number | null][] {
  return points.map((point) => [new Date(point.at).valueOf(), point.value])
}

/** The comparison week moved forward seven days, onto the minutes it corresponds to. */
export function shifted(points: CascadaPoint[]): [number, number | null][] {
  return points.map((point) => [new Date(point.at).valueOf() + WEEK_MS, point.value])
}

/**
 * The range both axes cover, from the earliest drawn point to the latest.
 *
 * Both axes are pinned to this one range. dataZoom sizes each axis from its own min and max,
 * so two axes with the same range zoom to the same window, and the top axis stays exactly a
 * week behind the bottom one at every zoom level.
 */
export function drawnExtent(
  origin: CascadaPoint[],
  comparison: CascadaPoint[],
  showComparison: boolean,
): [number, number] | null {
  const moments = pairs(origin).map(([at]) => at)
  if (showComparison) moments.push(...shifted(comparison).map(([at]) => at))
  if (moments.length === 0) return null
  let low = moments[0]
  let high = moments[0]
  for (const at of moments) {
    if (at < low) low = at
    if (at > high) high = at
  }
  return [low, high]
}
