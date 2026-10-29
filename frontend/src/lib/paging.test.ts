import { describe, expect, it } from 'vitest'
import { pageOf } from './paging'

const rows = Array.from({ length: 45 }, (_, index) => index)

describe('pageOf', () => {
  it('cuts the requested page and says where it is', () => {
    const second = pageOf(rows, 1, 20)
    expect(second.rows).toEqual(rows.slice(20, 40))
    expect(second.label).toBe('21–40 of 45 · page 2 of 3')
    expect(second.hasPrevious).toBe(true)
    expect(second.hasNext).toBe(true)
  })

  it('clamps a page past the end to the last one, as after a delete', () => {
    const last = pageOf(rows.slice(0, 21), 5, 20)
    expect(last.page).toBe(1)
    expect(last.rows).toEqual([20])
    expect(last.hasNext).toBe(false)
  })

  it('states an empty list rather than a page of nothing', () => {
    const empty = pageOf([], 0, 20)
    expect(empty.rows).toEqual([])
    expect(empty.label).toBe('No rows')
    expect(empty.hasPrevious || empty.hasNext).toBe(false)
  })
})
