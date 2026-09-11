import { beforeEach, describe, expect, it } from 'vitest'
import { MSN_GAP_TOLERANCE, SEVERITY_RANK } from '../lib/constants'
import { duration, kbps, maskUrl, ratio } from '../lib/format'
import { findingList, useSessionStore } from './session'
import type { FindingData, ServerMessage } from '../ws/messages'

function finding(overrides: Partial<FindingData>): FindingData {
  return {
    rule_id: 'MED-004',
    title: 'Stale live playlist',
    layer: 'media_playlist',
    stream_layer: 'PLAYBACK',
    severity: 'ERROR',
    owner: 'PACKAGER',
    owner_label: 'Packager',
    variant: 'v720p@1500k',
    detail: 'measured',
    root_cause: 'cause',
    fix: 'fix',
    rebuffer_impact: 'direct',
    count: 1,
    first_seen: '2026-09-11T10:00:00Z',
    last_seen: '2026-09-11T10:00:10Z',
    evidence: [],
    layer_presence: {},
    reference: '',
    ...overrides,
  }
}

function message(type: ServerMessage['type'], data: Record<string, unknown>): ServerMessage {
  return { type, session_id: 's1', ts: '2026-09-11T10:00:00Z', data }
}

describe('the session store', () => {
  beforeEach(() => useSessionStore.getState().reset())

  it('replaces a finding with the same rule, layer and rendition rather than duplicating it', () => {
    const { ingest } = useSessionStore.getState()
    ingest(message('finding', finding({ count: 1 }) as unknown as Record<string, unknown>))
    ingest(message('finding', finding({ count: 7 }) as unknown as Record<string, unknown>))

    const list = findingList(useSessionStore.getState())
    expect(list).toHaveLength(1)
    expect(list[0].count).toBe(7)
  })

  it('keeps the same rule on a different rendition as a separate finding', () => {
    const { ingest } = useSessionStore.getState()
    ingest(message('finding', finding({ variant: 'a' }) as unknown as Record<string, unknown>))
    ingest(message('finding', finding({ variant: 'b' }) as unknown as Record<string, unknown>))
    expect(findingList(useSessionStore.getState())).toHaveLength(2)
  })

  it('sorts findings by severity, then by occurrence count', () => {
    const { ingest } = useSessionStore.getState()
    ingest(
      message(
        'finding',
        finding({ rule_id: 'MST-012', severity: 'WARN', count: 99 }) as unknown as Record<
          string,
          unknown
        >,
      ),
    )
    ingest(
      message(
        'finding',
        finding({ rule_id: 'SEQ-009', severity: 'CRITICAL', count: 1 }) as unknown as Record<
          string,
          unknown
        >,
      ),
    )
    const list = findingList(useSessionStore.getState())
    expect(list[0].rule_id).toBe('SEQ-009')
    expect(list[1].rule_id).toBe('MST-012')
  })

  it('records playlist snapshots and segment results in arrival order', () => {
    const { ingest } = useSessionStore.getState()
    ingest(message('playlist_snapshot', { variant: 'v1', last_msn: 10 }))
    ingest(message('playlist_snapshot', { variant: 'v1', last_msn: 11 }))
    ingest(message('segment_result', { variant: 'v1', msn: 10 }))
    const state = useSessionStore.getState()
    expect(state.snapshots).toHaveLength(2)
    expect(state.segments).toHaveLength(1)
  })

  it('opens and closes a player stall band', () => {
    const store = useSessionStore.getState()
    store.openStall('v1', 1000)
    expect(useSessionStore.getState().stalls[0].end).toBeNull()
    useSessionStore.getState().closeStall('v1', 5000)
    expect(useSessionStore.getState().stalls[0].end).toBe(5000)
  })

  it('ignores a message type it does not know', () => {
    const before = useSessionStore.getState()
    before.ingest({
      type: 'not-a-type' as ServerMessage['type'],
      session_id: 's1',
      ts: '',
      data: {},
    })
    expect(useSessionStore.getState().snapshots).toEqual([])
  })
})

describe('formatting', () => {
  it('masks long token values and leaves ordinary parameters alone', () => {
    const masked = maskUrl('https://cdn/x.m3u8?hdnts=exp1234567890abcd&ads.chan=news')
    expect(masked).toContain('ads.chan=news')
    expect(masked).not.toContain('exp1234567890abcd')
    expect(masked).toContain('%E2%80%A6')
  })

  it('leaves a URL without a query string untouched', () => {
    expect(maskUrl('https://cdn/x.m3u8')).toBe('https://cdn/x.m3u8')
  })

  it('formats durations, bitrates and ratios', () => {
    expect(duration(42)).toBe('42 s')
    expect(duration(125)).toBe('2m 5s')
    expect(duration(7200)).toBe('2h 0m')
    expect(kbps(1_500_000)).toBe('1.50 Mbit/s')
    expect(kbps(600_000)).toBe('600 kbit/s')
    expect(ratio(0.2567)).toBe('0.257')
    expect(ratio(null)).toBe('—')
  })
})

describe('shared constants', () => {
  it('keeps the media sequence tolerance at the value the backend rule uses', () => {
    expect(MSN_GAP_TOLERANCE).toBe(5)
  })

  it('ranks severities so CRITICAL always sorts first', () => {
    expect(SEVERITY_RANK.CRITICAL).toBeGreaterThan(SEVERITY_RANK.ERROR)
    expect(SEVERITY_RANK.ERROR).toBeGreaterThan(SEVERITY_RANK.WARN)
    expect(SEVERITY_RANK.PASS).toBe(0)
  })
})
