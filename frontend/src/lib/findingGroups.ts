/**
 * One issue, however many renditions it was measured on.
 *
 * The engine keeps a finding per rule, stream layer and rendition, which is right for the
 * evidence: each rendition has its own playlist, its own segments and its own poll. On screen
 * that made one defect four cards on a four-rung ladder. A group is the same rule on the same
 * layer: one card, the worst severity, the summed occurrences, and every rendition it was
 * seen on — opening it shows each rendition's own evidence and manifest.
 */

import { SEVERITY_RANK, type Severity } from './constants'
import type { FindingData } from '../ws/messages'

export interface FindingGroup {
  key: string
  rule_id: string
  stream_layer: string
  title: string
  severity: FindingData['severity']
  count: number
  first_seen: string
  last_seen: string
  /** The member a card leads with: worst severity, then most occurrences. */
  lead: FindingData
  /** Every rendition's finding, worst first. */
  members: FindingData[]
}

export function groupKey(finding: Pick<FindingData, 'rule_id' | 'stream_layer'>): string {
  return `${finding.rule_id}|${finding.stream_layer}`
}

function rank(finding: FindingData): number {
  return SEVERITY_RANK[finding.severity as Severity] ?? 0
}

function byWeight(a: FindingData, b: FindingData): number {
  return rank(b) - rank(a) || b.count - a.count || (a.variant ?? '').localeCompare(b.variant ?? '')
}

/** Groups in the order their first member appears, so a ranked list stays ranked. */
export function groupFindings(findings: FindingData[]): FindingGroup[] {
  const order: string[] = []
  const members = new Map<string, FindingData[]>()
  for (const finding of findings) {
    const key = groupKey(finding)
    if (!members.has(key)) {
      members.set(key, [])
      order.push(key)
    }
    members.get(key)!.push(finding)
  }
  return order.map((key) => {
    const list = [...members.get(key)!].sort(byWeight)
    const lead = list[0]
    return {
      key,
      rule_id: lead.rule_id,
      stream_layer: lead.stream_layer,
      title: lead.title,
      severity: lead.severity,
      count: list.reduce((sum, item) => sum + item.count, 0),
      first_seen: list.map((item) => item.first_seen).sort()[0],
      last_seen: list.map((item) => item.last_seen).sort().at(-1) ?? lead.last_seen,
      lead,
      members: list,
    }
  })
}

/** The findings that share a finding's group, the finding itself included. */
export function membersOf(finding: FindingData, all: FindingData[]): FindingData[] {
  const key = groupKey(finding)
  const found = all.filter((item) => groupKey(item) === key)
  return (found.length > 0 ? found : [finding]).sort(byWeight)
}

/** What a group's renditions read as on a card: "720p, 1080p" or "every rendition". */
export function renditionLabel(group: FindingGroup): string {
  const names = group.members.map((item) => item.variant).filter((v): v is string => !!v)
  return names.length === 0 ? 'every rendition' : names.join(', ')
}
