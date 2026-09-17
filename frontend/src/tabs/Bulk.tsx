/**
 * Bulk tab.
 *
 * Three steps, stated as steps: choose the file, read the parsed rows and every row-level
 * error, then run the batch. Nothing starts until the operator has seen what will run.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useState } from 'react'
import { api, endpoints } from '../api/client'
import { PageBody, PageHeader } from '../components/layout/PageHeader'
import {
  Card,
  CardHeader,
  EmptyState,
  Field,
  InlineAlert,
  MetricRow,
  MetricTile,
  ProgressBar,
  Stepper,
  cx,
} from '../components/ui'
import { IconDownload, IconSearch, IconStop, IconUpload } from '../components/ui/icons'
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

const STEPS = ['Choose a file', 'Review the rows', 'Run the batch']

const TERMINAL = ['COMPLETED', 'FAILED', 'CANCELLED']

function riskTone(score: number | null): string {
  if (score == null) return 'text-ink-faint'
  if (score >= 70) return 'text-pink-600'
  if (score >= 40) return 'text-violet-500'
  return 'text-clean-600'
}

/**
 * A set of channels sent here from another tab.
 *
 * It arrives as the same `File` an operator would have chosen, so every step below — the
 * validation, the row errors, the review and the run — behaves exactly as it always has.
 * The token rises on every send, which is what lets the same set be sent twice.
 */
export interface BulkPrefill {
  file: File
  token: number
}

export function BulkTab({ prefill }: { prefill?: BulkPrefill | null }) {
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
  /* Once a batch is running its results are the page; the setup folds away until asked for. */
  const [setupOpen, setSetupOpen] = useState(true)
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
      setSetupOpen(false)
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
      return status && TERMINAL.includes(status) ? false : 3000
    },
  })

  const choose = useCallback(
    (chosen: File | null) => {
      setFile(chosen)
      setValidation(null)
      setJobId(null)
      setSetupOpen(true)
      if (chosen) validate.mutate(chosen)
    },
    [validate],
  )

  /* A set of channels sent from another tab is chosen for the operator; nothing auto-starts. */
  useEffect(() => {
    if (prefill) choose(prefill.file)
    // Only the token advances a send; the object identity changes on every render.
  }, [prefill?.token]) // eslint-disable-line react-hooks/exhaustive-deps

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

  const step = job ? 2 : validation ? 1 : 0
  const invalidRows = validation ? validation.total - validation.valid_count : 0
  const defectCount = items.filter(
    (item) => item.verdict_status && item.verdict_status !== 'NO STREAM-SIDE DEFECT',
  ).length

  return (
    <>
      <PageHeader
        title="Bulk analysis"
        subtitle="One file, one batch, one consolidated report — every channel analysed by the same rules."
        status={
          job ? (
            <span className={TERMINAL.includes(job.status) ? 'chip-clean' : 'chip-blue'}>
              {job.status}
            </span>
          ) : (
            <span className="chip-neutral">No batch running</span>
          )
        }
        actions={
          job && (
            <>
              <a
                className="btn-ghost btn-sm"
                href={api.url(`/bulk/jobs/${job.id}/report.html`)}
                target="_blank"
                rel="noreferrer"
              >
                Consolidated report
              </a>
              <a className="btn-ghost btn-sm" href={api.url(`/bulk/jobs/${job.id}/reports.zip`)}>
                <IconDownload size={14} />
                All reports
              </a>
              {!TERMINAL.includes(job.status) && (
                <button
                  type="button"
                  className="btn-accent btn-sm"
                  onClick={() => endpoints.cancelBulk(job.id)}
                >
                  <IconStop size={12} />
                  Cancel
                </button>
              )}
            </>
          )
        }
      />

      <PageBody>
        <Card className="card-pad">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-[320px] flex-1">
              <Stepper steps={STEPS} current={step} />
            </div>
            {job && (
              <button
                type="button"
                className="btn-ghost btn-sm"
                onClick={() => setSetupOpen((open) => !open)}
              >
                {setupOpen ? 'Hide the setup' : 'Start another batch'}
              </button>
            )}
          </div>
        </Card>

        {/* --- step one: the file --------------------------------------------- */}
        <Card className={cx(!setupOpen && 'panel-hidden')}>
          <CardHeader
            title="Channel list"
            subtitle="CSV, XLSX or JSON. Column names are matched through their aliases."
            actions={(['csv', 'xlsx', 'json'] as const).map((fmt) => (
              <a
                key={fmt}
                className="btn-ghost btn-sm"
                href={api.url(`/bulk/template.${fmt}`)}
                download
              >
                Template .{fmt}
              </a>
            ))}
          />
          <div className="px-5 pb-5">
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
              className={cx(
                'rounded-card border-2 border-dashed px-4 py-9 text-center transition-colors duration-150',
                dragging
                  ? 'border-brand-600 bg-brand-50'
                  : 'border-surface-lineStrong bg-surface-raised',
              )}
            >
              <span className="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-brand-50 text-brand-600">
                <IconUpload size={18} />
              </span>
              <p className="mt-3 text-card text-ink">Drop a channel list here</p>
              <p className="mx-auto mt-1 max-w-xl text-small text-ink-muted">
                Required columns: <code className="font-mono">channel_name</code> and{' '}
                <code className="font-mono">playback_url</code>. Optional: channel_id, country,
                content_provider, cdn, origin_url, cdn_url, ssai_url.
              </p>
              <label className="btn-primary mt-4 cursor-pointer">
                Choose file
                <input
                  type="file"
                  accept=".csv,.tsv,.xlsx,.xlsm,.json"
                  className="hidden"
                  onChange={(e) => choose(e.target.files?.[0] ?? null)}
                />
              </label>
              {file && (
                <p className="mt-3 font-mono text-micro text-ink-muted">
                  {file.name} · {(file.size / 1024).toFixed(1)} kB
                </p>
              )}
            </div>

            {error && (
              <div className="mt-3">
                <InlineAlert tone="error">{error}</InlineAlert>
              </div>
            )}
          </div>
        </Card>

        {/* --- step two: the rows --------------------------------------------- */}
        {validation && (
          <Card className={cx(!setupOpen && 'panel-hidden')}>
            <CardHeader
              title="Parsed rows"
              subtitle={
                invalidRows > 0
                  ? `${validation.valid_count} of ${validation.total} rows run. ${invalidRows} ${
                      invalidRows === 1 ? 'row carries' : 'rows carry'
                    } an error and ${invalidRows === 1 ? 'is' : 'are'} skipped.`
                  : `All ${validation.total} rows are valid and run.`
              }
              actions={
                <span className={invalidRows > 0 ? 'chip-pink' : 'chip-clean'}>
                  {validation.valid_count} / {validation.total} valid
                </span>
              }
            />

            <div className="max-h-72 overflow-auto border-y border-surface-line">
              <table className="table">
                <thead className="sticky top-0">
                  <tr>
                    <th>Row</th>
                    <th>Channel</th>
                    <th>Playback URL</th>
                    <th>Comparison layers</th>
                    <th>Errors</th>
                  </tr>
                </thead>
                <tbody>
                  {validation.rows.map((row) => (
                    <tr key={row.row_index} className={row.valid ? '' : 'bg-pink-50'}>
                      <td className="font-mono text-micro text-ink-faint">{row.row_index}</td>
                      <td className="text-body text-ink">{row.channel_name || '—'}</td>
                      <td>
                        <span className="block max-w-[380px] truncate font-mono text-micro text-ink-soft">
                          {row.playback_url || '—'}
                        </span>
                      </td>
                      <td className="text-micro text-ink-muted">
                        {[row.origin_url && 'origin', row.cdn_url && 'cdn', row.ssai_url && 'ssai']
                          .filter(Boolean)
                          .join(', ') || '—'}
                      </td>
                      <td
                        className={cx(
                          'text-micro',
                          row.errors.length > 0 ? 'text-pink-600' : 'text-ink-faint',
                        )}
                      >
                        {row.errors.join('; ') || '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* --- step three: the run ----------------------------------------- */}
            <div className="flex flex-wrap items-end gap-3 px-5 py-4">
              <Field label="Mode" htmlFor="bulk-mode" className="min-w-[280px]">
                <select
                  id="bulk-mode"
                  className="input"
                  value={mode}
                  onChange={(e) => setMode(e.target.value as 'snapshot' | 'aging')}
                >
                  <option value="snapshot">Snapshot — three minutes per channel</option>
                  <option value="aging">Aging — a chosen duration per channel</option>
                </select>
              </Field>
              {mode === 'aging' && (
                <Field label="Duration per channel" htmlFor="bulk-duration" className="min-w-[180px]">
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
                </Field>
              )}
              <Field label="Concurrency" htmlFor="bulk-concurrency" className="w-[120px]">
                <input
                  id="bulk-concurrency"
                  className="input"
                  type="number"
                  min={1}
                  max={50}
                  value={concurrency}
                  onChange={(e) => setConcurrency(Number(e.target.value))}
                />
              </Field>
              <button
                type="button"
                className="btn-primary"
                onClick={() => start.mutate()}
                disabled={start.isPending || validation.valid_count === 0}
              >
                Run {validation.valid_count} channels
              </button>
            </div>
          </Card>
        )}

        {/* --- results -------------------------------------------------------- */}
        {job && (
          <>
            <MetricRow>
              <MetricTile
                label="Channels"
                value={(job as { total?: number }).total ?? items.length}
                note="in this batch"
              />
              <MetricTile
                label="Analysed"
                value={(job as { completed?: number }).completed ?? 0}
                note={`${Math.round((job.progress ?? 0) * 100)}% complete`}
                tone="blue"
              />
              <MetricTile
                label="Channels with a defect"
                value={defectCount}
                note="verdict names a stream-side defect"
                tone={defectCount > 0 ? 'pink' : 'clean'}
              />
              <MetricTile
                label="Highest risk"
                value={
                  items.length > 0
                    ? Math.max(...items.map((item) => item.risk_score ?? 0))
                    : '—'
                }
                suffix="/ 100"
                tone="pink"
              />
              <MetricTile
                label="Failed rows"
                value={items.filter((item) => item.error).length}
                note="the analysis could not complete"
              />
              <MetricTile
                label="Mode"
                value={mode === 'aging' ? `${durationMinutes} min` : 'Snapshot'}
                note={`concurrency ${concurrency}`}
              />
            </MetricRow>

            <Card>
              <CardHeader
                title="Channel results"
                subtitle={`${(job as { completed?: number }).completed ?? 0} of ${
                  (job as { total?: number }).total ?? items.length
                } channels analysed · ${job.channel_name}`}
                actions={
                  <>
                    <label className="relative">
                      <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-faint">
                        <IconSearch size={14} />
                      </span>
                      <input
                        className="input max-w-[220px] pl-8"
                        placeholder="Filter channels"
                        value={filter}
                        onChange={(e) => setFilter(e.target.value)}
                      />
                    </label>
                    <select
                      className="input max-w-[190px]"
                      value={sortKey}
                      onChange={(e) => setSortKey(e.target.value as SortKey)}
                      aria-label="Sort channels"
                    >
                      <option value="risk_score">Sort by risk score</option>
                      <option value="channel_name">Sort by channel</option>
                      <option value="status">Sort by status</option>
                    </select>
                  </>
                }
              />

              <div className="px-5 pb-3">
                <ProgressBar value={job.progress ?? 0} tone="spectrum" />
              </div>

              {visible.length === 0 ? (
                <EmptyState
                  title="No channel matches this filter"
                  detail="Clear the filter to see every row in the batch."
                />
              ) : (
                <div className="overflow-x-auto">
                  <table className="table table-hover">
                    <thead>
                      <tr>
                        <th>#</th>
                        <th>Channel</th>
                        <th>Status</th>
                        <th>Verdict</th>
                        <th>Responsible party</th>
                        <th>Risk</th>
                        <th>Worst ratio</th>
                        <th>Incidents</th>
                        <th>Primary root cause</th>
                      </tr>
                    </thead>
                    <tbody>
                      {visible.map((item) => (
                        <tr key={item.row_index}>
                          <td className="font-mono text-micro text-ink-faint">{item.row_index}</td>
                          <td className="text-body font-semibold text-ink">{item.channel_name}</td>
                          <td className="text-micro text-ink-muted">{item.status}</td>
                          <td className="text-micro text-ink-soft">{item.verdict_status ?? '—'}</td>
                          <td>
                            {item.owner_label ? (
                              <span className="chip-blue">{item.owner_label}</span>
                            ) : (
                              <span className="text-micro text-ink-faint">—</span>
                            )}
                          </td>
                          <td
                            className={cx(
                              'font-mono text-body font-semibold tabular-nums',
                              riskTone(item.risk_score),
                            )}
                          >
                            {item.risk_score ?? '—'}
                          </td>
                          <td className="font-mono text-micro tabular-nums text-ink-soft">
                            {ratio(item.worst_ratio)}
                          </td>
                          <td className="font-mono text-micro tabular-nums text-ink-soft">
                            {item.incident_count ?? '—'}
                          </td>
                          <td className="max-w-[420px] text-micro text-ink-muted">
                            {item.headline ?? item.error ?? '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          </>
        )}
      </PageBody>
    </>
  )
}
