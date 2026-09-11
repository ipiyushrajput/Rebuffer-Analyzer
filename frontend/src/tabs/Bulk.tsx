/**
 * Bulk tab.
 *
 * Upload a file, see the parsed rows and every row-level error before anything starts, then
 * run the batch with a chosen mode and concurrency.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useState } from 'react'
import { api, endpoints } from '../api/client'
import { AGING_PRESETS } from '../lib/constants'
import { ratio } from '../lib/format'

interface ValidationRow {
  row_index: number
  channel_name: string
  playback_url: string
  origin_url: string | null
  cdn_url: string | null
  ssai_url: string | null
  country: string | null
  errors: string[]
  valid: boolean
}

interface BulkItem {
  row_index: number
  channel_name: string
  status: string
  verdict_status: string | null
  owner_label: string | null
  risk_score: number | null
  worst_ratio: number | null
  incident_count: number | null
  headline: string | null
  error: string | null
  child_job_id: string | null
}

type SortKey = 'risk_score' | 'channel_name' | 'status'

export function BulkTab() {
  const [file, setFile] = useState<File | null>(null)
  const [validation, setValidation] = useState<{
    rows: ValidationRow[]
    valid_count: number
    total: number
    column_mapping: Record<string, string>
  } | null>(null)
  const [mode, setMode] = useState<'snapshot' | 'aging'>('snapshot')
  const [durationMinutes, setDurationMinutes] = useState(60)
  const [concurrency, setConcurrency] = useState(5)
  const [jobId, setJobId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sortKey, setSortKey] = useState<SortKey>('risk_score')
  const [filter, setFilter] = useState('')
  const [dragging, setDragging] = useState(false)
  const queryClient = useQueryClient()

  const validate = useMutation({
    mutationFn: async (chosen: File) => {
      const form = new FormData()
      form.append('file', chosen)
      return endpoints.validateBulk(form)
    },
    onSuccess: (data) => {
      setError(null)
      setValidation(data as never)
    },
    onError: (err: Error) => {
      setValidation(null)
      setError(err.message)
    },
  })

  const start = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error('Choose a file first')
      const form = new FormData()
      form.append('file', file)
      form.append('mode', mode)
      form.append('concurrency', String(concurrency))
      if (mode === 'aging') form.append('duration_minutes', String(durationMinutes))
      return endpoints.createBulk(form)
    },
    onSuccess: (job) => {
      setError(null)
      setJobId(job.id)
      void queryClient.invalidateQueries({ queryKey: ['bulk-job'] })
    },
    onError: (err: Error) => setError(err.message),
  })

  const jobQuery = useQuery({
    queryKey: ['bulk-job', jobId],
    queryFn: () => endpoints.readBulk(jobId!),
    enabled: jobId != null,
    refetchInterval: (query) => {
      const status = (query.state.data as { status?: string } | undefined)?.status
      return status && ['COMPLETED', 'FAILED', 'CANCELLED'].includes(status) ? false : 3000
    },
  })

  const choose = useCallback(
    (chosen: File | null) => {
      setFile(chosen)
      setValidation(null)
      setJobId(null)
      if (chosen) validate.mutate(chosen)
    },
    [validate],
  )

  const job = jobQuery.data as (typeof jobQuery.data & { items?: BulkItem[] }) | undefined
  const items = (job?.items ?? []) as BulkItem[]
  const visible = items
    .filter((item) =>
      filter
        ? item.channel_name.toLowerCase().includes(filter.toLowerCase()) ||
          (item.verdict_status ?? '').toLowerCase().includes(filter.toLowerCase()) ||
          (item.owner_label ?? '').toLowerCase().includes(filter.toLowerCase())
        : true,
    )
    .sort((a, b) => {
      if (sortKey === 'channel_name') return a.channel_name.localeCompare(b.channel_name)
      if (sortKey === 'status') return a.status.localeCompare(b.status)
      return (b.risk_score ?? -1) - (a.risk_score ?? -1)
    })

  return (
    <div className="space-y-4">
      <section className="card px-4 py-4">
        <div
          onDragOver={(e) => {
            e.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDragging(false)
            choose(e.dataTransfer.files?.[0] ?? null)
          }}
          className={`rounded-lg border-2 border-dashed px-4 py-8 text-center transition-colors ${
            dragging ? 'border-brand-600 bg-brand-50' : 'border-[var(--rba-line)] bg-slate-50'
          }`}
        >
          <p className="text-sm font-semibold">Drop a CSV, XLSX or JSON file here</p>
          <p className="mt-1 text-xs text-[var(--rba-muted)]">
            Required columns: <code>channel_name</code> and <code>playback_url</code>. Optional:
            channel_id, country, content_provider, cdn, origin_url, cdn_url, ssai_url. Column
            names are matched through their aliases.
          </p>
          <div className="mt-3 flex flex-wrap items-center justify-center gap-2">
            <label className="btn-secondary cursor-pointer">
              Choose file
              <input
                type="file"
                accept=".csv,.tsv,.xlsx,.xlsm,.json"
                className="hidden"
                onChange={(e) => choose(e.target.files?.[0] ?? null)}
              />
            </label>
            {(['csv', 'xlsx', 'json'] as const).map((fmt) => (
              <a
                key={fmt}
                className="btn-secondary"
                href={api.url(`/bulk/template.${fmt}`)}
                download
              >
                Template .{fmt}
              </a>
            ))}
          </div>
          {file && (
            <p className="mt-2 text-xs text-[var(--rba-muted)]">
              {file.name} · {(file.size / 1024).toFixed(1)} kB
            </p>
          )}
        </div>

        {error && (
          <p className="mt-3 rounded border border-critical bg-red-50 px-3 py-2 text-sm text-critical">
            {error}
          </p>
        )}

        {validation && (
          <div className="mt-4 space-y-3">
            <p className="text-sm">
              <strong>{validation.valid_count}</strong> of <strong>{validation.total}</strong> row(s)
              are valid.{' '}
              {validation.total - validation.valid_count > 0 && (
                <span className="text-critical">
                  {validation.total - validation.valid_count} row(s) carry an error and are skipped.
                </span>
              )}
            </p>
            <div className="max-h-64 overflow-auto rounded border border-[var(--rba-line)]">
              <table className="w-full border-collapse text-xs">
                <thead>
                  <tr className="table-head">
                    <th className="px-2 py-1.5">Row</th>
                    <th className="px-2 py-1.5">Channel</th>
                    <th className="px-2 py-1.5">Playback URL</th>
                    <th className="px-2 py-1.5">Comparison URLs</th>
                    <th className="px-2 py-1.5">Errors</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--rba-line)]">
                  {validation.rows.map((row) => (
                    <tr key={row.row_index} className={row.valid ? '' : 'bg-red-50'}>
                      <td className="px-2 py-1.5 font-mono">{row.row_index}</td>
                      <td className="px-2 py-1.5">{row.channel_name || '—'}</td>
                      <td className="px-2 py-1.5 font-mono">
                        <span className="block max-w-[360px] truncate">{row.playback_url || '—'}</span>
                      </td>
                      <td className="px-2 py-1.5">
                        {[row.origin_url && 'origin', row.cdn_url && 'cdn', row.ssai_url && 'ssai']
                          .filter(Boolean)
                          .join(', ') || '—'}
                      </td>
                      <td className="px-2 py-1.5 text-critical">{row.errors.join('; ') || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="flex flex-wrap items-end gap-3">
              <div>
                <label className="label" htmlFor="bulk-mode">
                  Mode
                </label>
                <select
                  id="bulk-mode"
                  className="input"
                  value={mode}
                  onChange={(e) => setMode(e.target.value as 'snapshot' | 'aging')}
                >
                  <option value="snapshot">Snapshot — about three minutes per channel</option>
                  <option value="aging">Aging — a chosen duration per channel</option>
                </select>
              </div>
              {mode === 'aging' && (
                <div>
                  <label className="label" htmlFor="bulk-duration">
                    Duration per channel
                  </label>
                  <select
                    id="bulk-duration"
                    className="input"
                    value={durationMinutes}
                    onChange={(e) => setDurationMinutes(Number(e.target.value))}
                  >
                    {AGING_PRESETS.map((preset) => (
                      <option key={preset.minutes} value={preset.minutes}>
                        {preset.label}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              <div>
                <label className="label" htmlFor="bulk-concurrency">
                  Concurrency
                </label>
                <input
                  id="bulk-concurrency"
                  className="input max-w-[100px]"
                  type="number"
                  min={1}
                  max={50}
                  value={concurrency}
                  onChange={(e) => setConcurrency(Number(e.target.value))}
                />
              </div>
              <button
                type="button"
                className="btn-primary"
                onClick={() => start.mutate()}
                disabled={start.isPending || validation.valid_count === 0}
              >
                Start bulk analysis
              </button>
            </div>
          </div>
        )}
      </section>

      {job && (
        <section className="card">
          <div className="card-header">
            <div>
              <h2 className="card-title">
                {job.channel_name} — {job.status}
              </h2>
              <p className="text-xs text-[var(--rba-muted)]">
                {(job as { completed?: number }).completed ?? 0} of{' '}
                {(job as { total?: number }).total ?? items.length} channel(s) analysed
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <input
                className="input max-w-[200px]"
                placeholder="Filter channels"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
              <select
                className="input max-w-[180px]"
                value={sortKey}
                onChange={(e) => setSortKey(e.target.value as SortKey)}
              >
                <option value="risk_score">Sort by risk score</option>
                <option value="channel_name">Sort by channel</option>
                <option value="status">Sort by status</option>
              </select>
              <a
                className="btn-secondary"
                href={api.url(`/bulk/jobs/${job.id}/report.html`)}
                target="_blank"
                rel="noreferrer"
              >
                Consolidated report
              </a>
              <a className="btn-secondary" href={api.url(`/bulk/jobs/${job.id}/reports.zip`)}>
                Download ZIP
              </a>
              {!['COMPLETED', 'FAILED', 'CANCELLED'].includes(job.status) && (
                <button
                  type="button"
                  className="btn-danger"
                  onClick={() => endpoints.cancelBulk(job.id)}
                >
                  Cancel
                </button>
              )}
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="table-head">
                  <th className="px-3 py-2">#</th>
                  <th className="px-3 py-2">Channel</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">Verdict</th>
                  <th className="px-3 py-2">Responsible party</th>
                  <th className="px-3 py-2">Risk</th>
                  <th className="px-3 py-2">Worst ratio</th>
                  <th className="px-3 py-2">Incidents</th>
                  <th className="px-3 py-2">Primary root cause</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--rba-line)]">
                {visible.map((item) => (
                  <tr key={item.row_index}>
                    <td className="px-3 py-2 font-mono text-xs">{item.row_index}</td>
                    <td className="px-3 py-2 font-semibold">{item.channel_name}</td>
                    <td className="px-3 py-2 text-xs">{item.status}</td>
                    <td className="px-3 py-2 text-xs">{item.verdict_status ?? '—'}</td>
                    <td className="px-3 py-2 text-xs">{item.owner_label ?? '—'}</td>
                    <td
                      className={`px-3 py-2 font-mono font-semibold ${
                        (item.risk_score ?? 0) >= 70
                          ? 'text-critical'
                          : (item.risk_score ?? 0) >= 40
                            ? 'text-warn'
                            : 'text-pass'
                      }`}
                    >
                      {item.risk_score ?? '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">{ratio(item.worst_ratio)}</td>
                    <td className="px-3 py-2 font-mono text-xs">{item.incident_count ?? '—'}</td>
                    <td className="px-3 py-2 text-xs">
                      {item.headline ?? item.error ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  )
}
