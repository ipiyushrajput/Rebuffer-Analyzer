/** Reports tab: every report generated, searchable and re-downloadable. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { api, endpoints } from '../api/client'
import { OWNER_LABEL } from '../lib/constants'
import { bytes, localDateTime, utcTitle } from '../lib/format'

interface ReportRow {
  id: number
  job_id: string
  channel_name: string | null
  job_type: string
  format: string
  verdict_status: string | null
  owner: string | null
  risk_score: number | null
  size_bytes: number
  created_at: string
  url: string
  exists: boolean
}

const VERDICTS = [
  'REBUFFERING — STREAM DEFECT',
  'REBUFFERING RISK — STREAM DEFECT',
  'NO STREAM-SIDE DEFECT',
]

export function ReportsTab() {
  const [channel, setChannel] = useState('')
  const [owner, setOwner] = useState('')
  const [verdict, setVerdict] = useState('')
  const [since, setSince] = useState('')
  const queryClient = useQueryClient()

  const query = useMemo(() => {
    const params = new URLSearchParams()
    if (channel) params.set('channel', channel)
    if (owner) params.set('owner', owner)
    if (verdict) params.set('verdict', verdict)
    if (since) params.set('since', new Date(since).toISOString())
    const text = params.toString()
    return text ? `?${text}` : ''
  }, [channel, owner, verdict, since])

  const reportsQuery = useQuery({
    queryKey: ['reports', query],
    queryFn: () => endpoints.listReports(query),
    refetchInterval: 15000,
  })

  const remove = useMutation({
    mutationFn: (id: number) => endpoints.deleteReport(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['reports'] }),
  })

  const reports = (reportsQuery.data?.reports ?? []) as unknown as ReportRow[]

  return (
    <div className="space-y-4">
      <section className="card px-4 py-4">
        <div className="grid gap-3 md:grid-cols-4">
          <div>
            <label className="label" htmlFor="filter-channel">
              Channel
            </label>
            <input
              id="filter-channel"
              className="input"
              placeholder="Channel name contains"
              value={channel}
              onChange={(e) => setChannel(e.target.value)}
            />
          </div>
          <div>
            <label className="label" htmlFor="filter-owner">
              Responsible party
            </label>
            <select
              id="filter-owner"
              className="input"
              value={owner}
              onChange={(e) => setOwner(e.target.value)}
            >
              <option value="">Any</option>
              {Object.entries(OWNER_LABEL).map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="filter-verdict">
              Verdict
            </label>
            <select
              id="filter-verdict"
              className="input"
              value={verdict}
              onChange={(e) => setVerdict(e.target.value)}
            >
              <option value="">Any</option>
              {VERDICTS.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="filter-since">
              Generated since
            </label>
            <input
              id="filter-since"
              className="input"
              type="date"
              value={since}
              onChange={(e) => setSince(e.target.value)}
            />
          </div>
        </div>
      </section>

      <section className="card">
        <div className="card-header">
          <h2 className="card-title">Reports</h2>
          <span className="text-xs text-[var(--rba-muted)]">{reports.length} report(s)</span>
        </div>
        {reports.length === 0 ? (
          <p className="px-4 py-6 text-sm text-[var(--rba-muted)]">
            No report matches these filters.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="table-head">
                  <th className="px-3 py-2">Channel</th>
                  <th className="px-3 py-2">Type</th>
                  <th className="px-3 py-2">Verdict</th>
                  <th className="px-3 py-2">Responsible party</th>
                  <th className="px-3 py-2">Risk</th>
                  <th className="px-3 py-2">Format</th>
                  <th className="px-3 py-2">Size</th>
                  <th className="px-3 py-2">Generated</th>
                  <th className="px-3 py-2">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--rba-line)]">
                {reports.map((report) => (
                  <tr key={report.id} className={report.exists ? '' : 'opacity-60'}>
                    <td className="px-3 py-2 font-semibold">{report.channel_name ?? '—'}</td>
                    <td className="px-3 py-2 text-xs capitalize">{report.job_type}</td>
                    <td className="px-3 py-2 text-xs">{report.verdict_status ?? '—'}</td>
                    <td className="px-3 py-2 text-xs">
                      {report.owner ? (OWNER_LABEL[report.owner] ?? report.owner) : '—'}
                    </td>
                    <td className="px-3 py-2 font-mono">{report.risk_score ?? '—'}</td>
                    <td className="px-3 py-2 text-xs uppercase">{report.format}</td>
                    <td className="px-3 py-2 font-mono text-xs">{bytes(report.size_bytes)}</td>
                    <td className="px-3 py-2 text-xs" title={utcTitle(report.created_at)}>
                      {localDateTime(report.created_at)}
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex gap-1">
                        {report.exists ? (
                          <a
                            className="btn-secondary !px-2 !py-1 text-xs"
                            href={api.url(report.url.replace('/api', ''))}
                            target="_blank"
                            rel="noreferrer"
                          >
                            Open
                          </a>
                        ) : (
                          <span className="chip border-warn bg-amber-50 text-warn">
                            File removed
                          </span>
                        )}
                        <button
                          type="button"
                          className="btn-danger !px-2 !py-1 text-xs"
                          onClick={() => remove.mutate(report.id)}
                        >
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}
