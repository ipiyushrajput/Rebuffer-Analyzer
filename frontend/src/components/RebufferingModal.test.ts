import { describe, expect, it } from 'vitest'
import { aboveOnly, formatPct, utcLabel } from './RebufferingModal'

const at = (minute: number) => `2026-09-10T00:${String(minute).padStart(2, '0')}:00+00:00`

describe('the breaches drawn over the week', () => {
  it('keeps only the minutes strictly above the threshold', () => {
    // The same comparison the backend applies: above, not at.
    const drawn = aboveOnly(
      [
        { at: at(0), value: 0.2 },
        { at: at(1), value: 0.25 },
        { at: at(2), value: 0.26 },
      ],
      0.25,
    )

    expect(drawn.map((point) => point[1])).toEqual([null, null, 0.26])
  })

  it('leaves an unmeasured minute blank rather than treating it as a breach', () => {
    const drawn = aboveOnly([{ at: at(0), value: null }], 0.25)
    expect(drawn[0][1]).toBeNull()
  })

  it('keeps every minute on the axis, so the blanks are gaps in place', () => {
    const points = [
      { at: at(0), value: 9 },
      { at: at(1), value: 0.01 },
      { at: at(2), value: 9 },
    ]
    const drawn = aboveOnly(points, 0.25)

    expect(drawn).toHaveLength(3)
    expect(drawn.map((point) => point[0])).toEqual(points.map((p) => new Date(p.at).valueOf()))
  })
})

describe('how measurements are written', () => {
  it('states a percentage to three places and an absent one as a dash', () => {
    expect(formatPct(0.12452777)).toBe('0.125 %')
    expect(formatPct(0.2)).toBe('0.200 %')
    expect(formatPct(null)).toBe('—')
    expect(formatPct(undefined)).toBe('—')
  })

  it('writes a timestamp as UTC without its offset', () => {
    expect(utcLabel('2026-09-17T05:13:00+00:00')).toBe('2026-09-17 05:13:00')
    expect(utcLabel(null)).toBe('—')
  })
})
