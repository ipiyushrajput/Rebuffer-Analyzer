/**
 * The range filter's arithmetic.
 *
 * A run that finished on Friday and is opened on Monday must still show its last day. That
 * is the whole reason the window is measured back from the run's last sample rather than from
 * now: measuring from now would hand back an empty chart for every finished run.
 */

import { describe, expect, it } from 'vitest'
import { windowFor } from './AgingAnalytics'

const LAST = '2026-09-11T18:30:00.000Z'

describe('windowFor', () => {
  it('measures the window back from the run, not from now', () => {
    expect(windowFor('1d', LAST)).toBe('2026-09-10T18:30:00.000Z')
    expect(windowFor('2d', LAST)).toBe('2026-09-09T18:30:00.000Z')
    expect(windowFor('7d', LAST)).toBe('2026-09-04T18:30:00.000Z')
  })

  it('crosses a month boundary by arithmetic, not by day number', () => {
    expect(windowFor('7d', '2026-10-03T00:00:00.000Z')).toBe('2026-09-26T00:00:00.000Z')
  })

  it('sends no bound for the full range, so the backend returns the whole run', () => {
    expect(windowFor('full', LAST)).toBeUndefined()
  })

  it('sends no bound when the run has no sample to measure back from', () => {
    expect(windowFor('7d', null)).toBeUndefined()
  })

  it('sends no bound rather than an invalid one when the timestamp does not parse', () => {
    expect(windowFor('7d', 'last tuesday')).toBeUndefined()
  })
})
