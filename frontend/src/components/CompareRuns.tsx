/**
 * Two runs side by side: what only the first raised, what only the second raised, and what
 * both raised with each run's count — the before and after of a fix, two CDNs, two devices.
 */

import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { localDateTime } from '../lib/format'
import type { Severity } from '../lib/constants'
import { PageBody, PageHeader } from './layout/PageHeader'
import { Card, CardHeader, EmptyState, InlineAlert, SeverityChip } from './ui'

interface Run {
  job_id: string
  channel_name: string | null
  job_type: string
  created_at: string | null
  playback_url: string | null
  ua_profile: string | null
  verdict_status: string | null
  risk_score: number | null
}

interface Issue {
  rule_id: string
  stream_layer: string
  title: string
  severity: string
  count: number
  renditions: string[]
  severity_a?: string
  severity_b?: string
  count_a?: number
  count_b?: number
}

function RunCard({ label, run }: { label: string; run: Run }) {
  return (
    <Card>
      <CardHeader title={`${label} — ${run.channel_name ?? run.job_id}`} subtitle={`${run.job_type} · ${localDateTime(run.created_at)}`} />
      <dl className="grid gap-2 px-5 pb-4 text-small">
        <div><dt className="label">Verdict</dt><dd>{run.verdict_status ?? '—'} · risk {run.risk_score ?? '—'}</dd></div>
        <div><dt className="label">User-Agent profile</dt><dd className="font-mono text-micro">{run.ua_profile ?? '—'}</dd></div>
        <div><dt className="label">Playback URL</dt><dd className="break-all font-mono text-micro text-ink-soft">{run.playback_url ?? '—'}</dd></div>
      </dl>
    </Card>
  )
}

function IssueTable({ title, issues, both }: { title: string; issues: Issue[]; both?: boolean }) {
  return (
    <Card>
      <CardHeader title={title} actions={<span className="chip-neutral">{issues.length}</span>} />
      {issues.length === 0 ? (
        <EmptyState title="None" />
      ) : (
        <div className="overflow-x-auto">
          <table className="table">
            <thead>
              <tr>
                <th>Rule</th>
                {both ? (<><th>A</th><th>B</th></>) : (<><th>Severity</th><th>Count</th></>)}
                <th>Renditions</th>
              </tr>
            </thead>
            <tbody>
              {issues.map((issue) => (
                <tr key={`${issue.rule_id}-${issue.stream_layer}`}>
                  <td className="text-small"><code className="font-mono text-micro">{issue.rule_id}</code> {issue.title}</td>
                  {both ? (
                    <>
                      <td><SeverityChip severity={issue.severity_a as Severity} /> <span className="font-mono text-micro">×{issue.count_a}</span></td>
                      <td><SeverityChip severity={issue.severity_b as Severity} /> <span className="font-mono text-micro">×{issue.count_b}</span></td>
                    </>
                  ) : (
                    <>
                      <td><SeverityChip severity={issue.severity as Severity} /></td>
                      <td className="font-mono text-micro">{issue.count}</td>
                    </>
                  )}
                  <td className="max-w-[260px] break-words font-mono text-micro text-ink-soft">{issue.renditions.join(', ') || 'ladder'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

export function CompareRuns({ a, b, onBack }: { a: string; b: string; onBack: () => void }) {
  const { data, error, isLoading } = useQuery({
    queryKey: ['compare', a, b],
    queryFn: () =>
      api.get<{ a: Run; b: Run; only_a: Issue[]; only_b: Issue[]; both: Issue[] }>(
        `/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`,
      ),
  })
  return (
    <>
      <PageHeader title="Compare runs" onBack={onBack} backLabel="Back to channels" />
      <PageBody>
        {isLoading && <EmptyState title="Loading…" />}
        {error && <InlineAlert tone="error">{String(error)}</InlineAlert>}
        {data && (
          <div className="space-y-4">
            <div className="grid gap-4 lg:grid-cols-2">
              <RunCard label="A" run={data.a} />
              <RunCard label="B" run={data.b} />
            </div>
            <div className="grid gap-4 xl:grid-cols-2">
              <IssueTable title="Only in A" issues={data.only_a} />
              <IssueTable title="Only in B" issues={data.only_b} />
            </div>
            <IssueTable title="In both" issues={data.both} both />
          </div>
        )}
      </PageBody>
    </>
  )
}
