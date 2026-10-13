/**
 * Findings across every run: "which channels had HTTP-005 this week", "everything CRITICAL on
 * this CDN host". One row per run and rule, newest run first, each opening its run.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../api/client'
import { localDateTime } from '../lib/format'
import { Card, CardHeader, EmptyState, Field, SeverityChip } from './ui'
import type { Severity } from '../lib/constants'

interface SearchResult {
  job_id: string
  channel_name: string | null
  job_type: string
  created_at: string | null
  rule_id: string
  title: string
  severity: string
  owner: string
  renditions: string[]
  count: number
}

export function FindingSearch({ onOpen }: { onOpen: (jobId: string) => void }) {
  const [form, setForm] = useState({ rule_id: '', channel: '', host: '', severity: '', since_days: 7 })
  const [query, setQuery] = useState<typeof form | null>(null)
  const params = new URLSearchParams()
  if (query) {
    for (const [key, value] of Object.entries(query)) if (value !== '' && value != null) params.set(key, String(value))
  }
  const { data, isFetching, error } = useQuery({
    queryKey: ['finding-search', params.toString()],
    queryFn: () =>
      api.get<{ results: SearchResult[]; runs: number; channels: number }>(`/search/findings?${params.toString()}`),
    enabled: query !== null,
  })

  return (
    <Card>
      <CardHeader title="Search findings across runs" />
      <form
        className="grid gap-3 px-5 pb-4 md:grid-cols-[repeat(5,minmax(0,1fr))_auto]"
        onSubmit={(event) => {
          event.preventDefault()
          setQuery({ ...form })
        }}
      >
        <Field label="Rule" htmlFor="search-rule">
          <input id="search-rule" className="input-mono" placeholder="HTTP-005" value={form.rule_id}
            onChange={(e) => setForm({ ...form, rule_id: e.target.value })} />
        </Field>
        <Field label="Channel contains" htmlFor="search-channel">
          <input id="search-channel" className="input" value={form.channel}
            onChange={(e) => setForm({ ...form, channel: e.target.value })} />
        </Field>
        <Field label="CDN host contains" htmlFor="search-host">
          <input id="search-host" className="input-mono" placeholder="akamaized.net" value={form.host}
            onChange={(e) => setForm({ ...form, host: e.target.value })} />
        </Field>
        <Field label="Severity" htmlFor="search-severity">
          <select id="search-severity" className="input" value={form.severity}
            onChange={(e) => setForm({ ...form, severity: e.target.value })}>
            <option value="">Any</option>
            <option value="CRITICAL">CRITICAL</option>
            <option value="ERROR">ERROR and above</option>
            <option value="WARN">WARN and above</option>
          </select>
        </Field>
        <Field label="Last" htmlFor="search-days">
          <select id="search-days" className="input" value={form.since_days}
            onChange={(e) => setForm({ ...form, since_days: Number(e.target.value) })}>
            <option value={1}>24 hours</option>
            <option value={7}>7 days</option>
            <option value={30}>30 days</option>
            <option value={90}>90 days</option>
          </select>
        </Field>
        <div className="flex items-end">
          <button type="submit" className="btn-primary" disabled={isFetching}>
            {isFetching ? 'Searching' : 'Search'}
          </button>
        </div>
      </form>
      {error && <p className="px-5 pb-4 text-small text-pink-600">{String(error)}</p>}
      {data && (
        <div className="border-t border-surface-line">
          <p className="px-5 py-2 text-micro text-ink-muted">
            {data.results.length} result(s) in {data.runs} run(s) of {data.channels} channel(s)
          </p>
          {data.results.length === 0 ? (
            <EmptyState title="Nothing matches" />
          ) : (
            <div className="max-h-[420px] overflow-auto">
              <table className="table table-hover">
                <thead className="sticky top-0">
                  <tr>
                    <th>Run</th><th>Channel</th><th>Rule</th><th>Severity</th><th>Renditions</th><th>Count</th><th />
                  </tr>
                </thead>
                <tbody>
                  {data.results.map((row) => (
                    <tr key={`${row.job_id}-${row.rule_id}`}>
                      <td className="whitespace-nowrap font-mono text-micro text-ink-soft">
                        {localDateTime(row.created_at)}<br /><span className="text-ink-faint">{row.job_type}</span>
                      </td>
                      <td className="text-small">{row.channel_name}</td>
                      <td className="text-small"><code className="font-mono text-micro">{row.rule_id}</code> {row.title}</td>
                      <td><SeverityChip severity={row.severity as Severity} /></td>
                      <td className="max-w-[220px] break-words font-mono text-micro text-ink-soft">{row.renditions.join(', ') || 'ladder'}</td>
                      <td className="font-mono text-micro">{row.count}</td>
                      <td><button type="button" className="btn-ghost btn-sm" onClick={() => onOpen(row.job_id)}>Open</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Card>
  )
}
