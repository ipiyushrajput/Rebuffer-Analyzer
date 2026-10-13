/**
 * One issue on four renditions is one card, not four.
 *
 * The engine keeps a finding per rendition, which is right for the evidence. On screen the same
 * rule on the same layer is one group: worst severity, summed occurrences, every rendition.
 */

import { describe, expect, it } from 'vitest'
import type { FindingData } from '../ws/messages'
import { groupFindings, membersOf, renditionLabel } from './findingGroups'
import { diffLines } from '../components/PollManifest'

function finding(overrides: Partial<FindingData>): FindingData {
  return {
    rule_id: 'HTTP-005',
    title: 'Segment listed in the playlist returns HTTP 404',
    layer: 'http',
    stream_layer: 'PLAYBACK',
    severity: 'ERROR',
    owner: 'CDN',
    owner_label: 'CDN',
    variant: 'v1',
    detail: 'segment returned HTTP 404',
    root_cause: '',
    fix: '',
    rebuffer_impact: 'direct',
    count: 1,
    first_seen: '2026-10-13T09:00:00+00:00',
    last_seen: '2026-10-13T09:00:00+00:00',
    evidence: [],
    layer_presence: {},
    reference: '',
    ...overrides,
  }
}

describe('grouping findings across renditions', () => {
  const findings = [
    finding({ variant: '360p', count: 2, first_seen: '2026-10-13T09:05:00+00:00' }),
    finding({ rule_id: 'MED-004', title: 'Stale', severity: 'WARN', variant: '360p' }),
    finding({ variant: '1080p', count: 5, severity: 'CRITICAL', last_seen: '2026-10-13T10:00:00+00:00' }),
    finding({ variant: '720p', count: 1 }),
  ]

  it('makes one group per rule and layer, in the order the list ranked them', () => {
    const groups = groupFindings(findings)
    expect(groups.map((g) => g.rule_id)).toEqual(['HTTP-005', 'MED-004'])
  })

  it('takes the worst severity, the summed count and the widest time span', () => {
    const [group] = groupFindings(findings)
    expect(group.severity).toBe('CRITICAL')
    expect(group.count).toBe(8)
    expect(group.first_seen).toBe('2026-10-13T09:00:00+00:00')
    expect(group.last_seen).toBe('2026-10-13T10:00:00+00:00')
    expect(group.lead.variant).toBe('1080p')
    expect(group.members.map((m) => m.variant)).toEqual(['1080p', '360p', '720p'])
    expect(renditionLabel(group)).toBe('1080p, 360p, 720p')
  })

  it('keeps the same rule on another layer apart', () => {
    const groups = groupFindings([finding({}), finding({ stream_layer: 'CDN' })])
    expect(groups).toHaveLength(2)
  })

  it('finds the siblings of an opened finding', () => {
    const members = membersOf(findings[3], findings)
    expect(members.map((m) => m.variant)).toEqual(['1080p', '360p', '720p'])
    expect(membersOf(findings[1], [])).toEqual([findings[1]])
  })
})

describe('the diff against the previous poll', () => {
  it('marks the segment that arrived and the one that rolled out', () => {
    const before = '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1\nseg1.ts\nseg2.ts'
    const after = '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:2\nseg2.ts\nseg3.ts'
    const lines = diffLines(before, after)
    expect(lines.filter((l) => l.change === 'removed').map((l) => l.text)).toEqual([
      '#EXT-X-MEDIA-SEQUENCE:1',
      'seg1.ts',
    ])
    expect(lines.filter((l) => l.change === 'added').map((l) => l.text)).toEqual([
      '#EXT-X-MEDIA-SEQUENCE:2',
      'seg3.ts',
    ])
  })

  it('treats a repeated line by count, not by presence', () => {
    const lines = diffLines('#EXTINF:6.0,\na.ts', '#EXTINF:6.0,\na.ts\n#EXTINF:6.0,\nb.ts')
    expect(lines.filter((l) => l.change === 'added').map((l) => l.text)).toEqual(['#EXTINF:6.0,', 'b.ts'])
  })
})
