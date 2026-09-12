/**
 * Aging tab.
 *
 * Jobs run server-side and survive the browser closing and a backend restart. Clicking a
 * job renders the same charts from the stored samples.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api, endpoints, type JobSummary } from '../api/client'
import { FindingsFeed } from '../components/FindingCard'
import { UrlForm, emptyForm, toPayload, type UrlFormValue } from '../components/UrlForm'
import { VerdictBanner } from '../components/VerdictBanner'
import { AGING_PRESETS, REBUFFER_RATIO_THRESHOLD_DEFAULT } from '../lib/constants'
import { duration, localDateTime, utcTitle } from '../lib/format'
import type { FindingData, VerdictData } from '../ws/messages'

const ACTIVE = new Set(['PENDING', 'RUNNING', 'CANCELLING'])

function StatusChip({ status }: { status: string }) {
  const tone =
    status === 'RUNNING'
      ? 'border-info bg-blue-50 text-info'
      : status === 'COMPLETED'
        ? 'border-pass bg-green-50 text-pass'
        : status === 'FAILED'
          ? 'border-critical bg-red-50 text-critical'
          : 'border-slate-200 bg-slate-50 text-[var(--rba-muted)]'
  return <span className={`chip ${tone}`}>{status}</span>
}

function ProgressBar({ value }: { value: number }) {
  return (
    <div
      className="h-2 w-full rounded-full bg-slate-200"
      role="progressbar"
      aria-valuenow={Math.round(value * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className="h-2 rounded-full bg-brand-600 transition-[width]"
        style={{ width: `${Math.min(100, value * 100)}%` }}
      />
    </div>
  )
}

export function AgingTab() {
  const [form, setForm] = useState<UrlFormValue>(emptyForm(true))
  const [durationMinutes, setDurationMinutes] = useState(60)
  const [custom, setCustom] = useState('')
  const [openJob, setOpenJob] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const queryClient = useQueryClient()

  const jobsQuery = useQuery({
    queryKey: ['aging-jobs'],
    queryFn: endpoints.listAging,
    refetchInterval: 4000,
  })

  const create = useMutation({
    mutationFn: () =>
      endpoints.createAging({
        ...toPayload(form),
        duration_minutes: custom ? Number(custom) : durationMinutes,
      }),
    onSuccess: () => {
      setError(null)
      setForm(emptyForm(true))
      void queryClient.invalidateQueries({ queryKey: ['aging-jobs'] })
    },
    onError: (err: Error) => setError(err.message),
  })

  const cancel = useMutation({
    mutationFn: (id: string) => endpoints.cancelAging(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['aging-jobs'] }),
  })

  const jobs = jobsQuery.data?.jobs ?? []

  return (
    <div className="space-y-4">
      <section className="card px-4 py-4">
        <UrlForm value={form} onChange={setForm} disabled={create.isPending} />

        <fieldset className="mt-3">
          <legend className="label">Duration</legend>
          <div className="flex flex-wrap items-center gap-2">
            {AGING_PRESETS.map((preset) => (
              <button
                key={preset.minutes}
                type="button"
                className={`btn-secondary ${
                  !custom && durationMinutes === preset.minutes
                    ? '!border-brand-600 !bg-brand-50 !text-brand-700'
                    : ''
                }`}
                onClick={() => {
                  setDurationMinutes(preset.minutes)
                  setCustom('')
                }}
              >
                {preset.label}
              </button>
            ))}
            <label className="flex items-center gap-2 text-sm">
              <span className="text-[var(--rba-muted)]">Custom</span>
              <input
                className="input max-w-[110px]"
                type="number"
                min={1}
                max={1440}
                placeholder="minutes"
                value={custom}
                onChange={(e) => setCustom(e.target.value)}
              />
            </label>
          </div>
        </fieldset>

        <div className="mt-3 flex items-center gap-2">
          <button
            type="button"
            className="btn-primary"
            onClick={() => create.mutate()}
            disabled={create.isPending || form.playback_url.trim().length < 8}
          >
            Start aging analysis
          </button>
          <span className="text-xs text-[var(--rba-muted)]">
            The job runs on the server. Closing this browser does not stop it, and it resumes
            after a backend restart.
          </span>
        </div>

        {error && (
          <p className="mt-3 rounded border border-critical bg-red-50 px-3 py-2 text-sm text-critical">
            {error}
          </p>
        )}
      </section>

      <section className="card">
        <div className="card-header">
          <h2 className="card-title">Jobs</h2>
          <span className="text-xs text-[var(--rba-muted)]">{jobs.length} job(s)</span>
        </div>
        {jobs.length === 0 ? (
          <p className="px-4 py-6 text-sm text-[var(--rba-muted)]">
            No aging job has been started yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="table-head">
                  <th className="px-3 py-2">Channel</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2 w-48">Progress</th>
                  <th className="px-3 py-2">Remaining</th>
                  <th className="px-3 py-2">Severity counts</th>
                  <th className="px-3 py-2">Verdict</th>
                  <th className="px-3 py-2">Started</th>
                  <th className="px-3 py-2">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--rba-line)]">
                {jobs.map((job) => (
                  <JobRow
                    key={job.id}
                    job={job}
                    onCancel={() => cancel.mutate(job.id)}
                    onOpen={() => setOpenJob(openJob === job.id ? null : job.id)}
                    open={openJob === job.id}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {openJob && <JobDetail jobId={openJob} />}
    </div>
  )
}

function JobRow({
  job,
  onCancel,
  onOpen,
  open,
}: {
  job: JobSummary
  onCancel: () => void
  onOpen: () => void
  open: boolean
}) {
  const verdict = job.verdict as VerdictData | null
  const counts = Object.entries(job.counts ?? {})
    .filter(([, count]) => count > 0)
    .map(([severity, count]) => `${severity} ${count}`)
    .join(' · ')

  return (
    <tr className={open ? 'bg-brand-50/40' : ''}>
      <td className="px-3 py-2 font-semibold">{job.channel_name}</td>
      <td className="px-3 py-2">
        <StatusChip status={job.status} />
      </td>
      <td className="px-3 py-2">
        <ProgressBar value={job.progress} />
        <span className="text-xs text-[var(--rba-muted)]">
          {duration(job.elapsed_s)} elapsed
        </span>
      </td>
      <td className="px-3 py-2 font-mono text-xs">{duration(job.remaining_s)}</td>
      <td className="px-3 py-2 text-xs">{counts || '—'}</td>
      <td className="px-3 py-2 text-xs">{verdict?.status ?? '—'}</td>
      <td className="px-3 py-2 text-xs" title={utcTitle(job.started_at)}>
        {localDateTime(job.started_at)}
      </td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap gap-1">
          <button type="button" className="btn-secondary !px-2 !py-1 text-xs" onClick={onOpen}>
            {open ? 'Hide' : 'Open'}
          </button>
          {ACTIVE.has(job.status) && (
            <button type="button" className="btn-danger !px-2 !py-1 text-xs" onClick={onCancel}>
              Cancel
            </button>
          )}
          {!ACTIVE.has(job.status) && (
            <>
              <a
                className="btn-secondary !px-2 !py-1 text-xs"
                href={api.url(`/aging/jobs/${job.id}/report.html`)}
                target="_blank"
                rel="noreferrer"
              >
                HTML
              </a>
              <a
                className="btn-secondary !px-2 !py-1 text-xs"
                href={api.url(`/aging/jobs/${job.id}/report.pdf`)}
                target="_blank"
                rel="noreferrer"
              >
                PDF
              </a>
              <a
                className="btn-secondary !px-2 !py-1 text-xs"
                href={api.url(`/jobs/${job.id}/evidence.zip`)}
              >
                Evidence
              </a>
            </>
          )}
        </div>
      </td>
    </tr>
  )
}

function JobDetail({ jobId }: { jobId: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['aging-result', jobId],
    queryFn: () => api.get<Record<string, unknown>>(`/aging/jobs/${jobId}/result`),
    refetchInterval: 8000,
  })

  if (isLoading) {
    return (
      <section className="card px-4 py-6 text-sm text-[var(--rba-muted)]">
        Loading the stored samples for this job.
      </section>
    )
  }
  if (error) {
    return (
      <section className="card px-4 py-6 text-sm text-critical">
        {error instanceof Error ? error.message : String(error)}
      </section>
    )
  }

  const verdict = (data?.verdict as VerdictData | null) ?? null
  const findings = (data?.findings as FindingData[]) ?? []
  const vpb = (data?.vpb as Record<string, { series?: { at: string; level_s: number }[] }>) ?? {}

  return (
    <div className="space-y-4">
      <VerdictBanner
        verdict={verdict}
        threshold={REBUFFER_RATIO_THRESHOLD_DEFAULT}
        elapsed={verdict?.window_seconds ?? 0}
      />

      {Object.keys(vpb).length > 0 && (
        <section className="card">
          <div className="card-header">
            <h3 className="card-title">Virtual Player Buffer — stored samples</h3>
            <span className="text-xs text-[var(--rba-muted)]">
              {Object.keys(vpb).length} rendition(s)
            </span>
          </div>
          <div className="px-4 py-3 text-xs text-[var(--rba-muted)]">
            {Object.entries(vpb).map(([variant, series]) => (
              <div key={variant}>
                {variant}: {series.series?.length ?? 0} sample(s)
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="card">
        <div className="card-header">
          <h3 className="card-title">Findings</h3>
          <span className="text-xs text-[var(--rba-muted)]">{findings.length}</span>
        </div>
        <div className="max-h-[60vh] space-y-2 overflow-y-auto p-2">
          <FindingsFeed findings={findings} />
        </div>
      </section>
    </div>
  )
}
