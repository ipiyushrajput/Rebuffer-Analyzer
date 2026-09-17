/**
 * Aging tab.
 *
 * Jobs run server-side and survive the browser closing and a backend restart. Opening a job
 * shows the verdict issued from its stored samples, and opening a finding inside it shows the
 * same investigation the Realtime tab does.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api, endpoints, type JobSummary } from '../api/client'
import { FindingsList } from '../components/FindingCard'
import { FindingInvestigation } from '../components/FindingInvestigation'
import { PageBody, PageHeader } from '../components/layout/PageHeader'
import { SessionVerdict } from '../components/SessionVerdict'
import { UrlForm, emptyForm, toPayload, type UrlFormValue } from '../components/UrlForm'
import {
  Card,
  CardHeader,
  EmptyState,
  Field,
  InlineAlert,
  MetricRow,
  MetricTile,
  ProgressBar,
  SegmentedControl,
  SeverityChip,
  cx,
} from '../components/ui'
import { IconDownload, IconFile, IconPulse, IconStop } from '../components/ui/icons'
import {
  AGING_PRESETS,
  LIVE_POLL_MS,
  REBUFFER_RATIO_THRESHOLD_DEFAULT,
  isTerminal,
  type Severity,
} from '../lib/constants'
import { duration, localDateTime, utcTitle } from '../lib/format'
import { usePrefill, type ChannelPrefill } from '../lib/prefill'
import type { FindingData, VerdictData } from '../ws/messages'

const ACTIVE = new Set(['PENDING', 'RUNNING', 'CANCELLING'])

const STATUS_CHIP: Record<string, string> = {
  RUNNING: 'chip-blue',
  PENDING: 'chip-violet',
  CANCELLING: 'chip-violet',
  COMPLETED: 'chip-clean',
  FAILED: 'chip-pink',
  CANCELLED: 'chip-neutral',
}

function JobStatusChip({ status }: { status: string }) {
  return <span className={STATUS_CHIP[status] ?? 'chip-neutral'}>{status}</span>
}

interface Props {
  thresholds: Record<string, number>
  /** A channel sent from All channels. It seeds the form; it never starts the job. */
  prefill?: ChannelPrefill | null
}

export function AgingTab({ thresholds, prefill = null }: Props) {
  const [form, setForm] = useState<UrlFormValue>(emptyForm(true))
  usePrefill(prefill, setForm, true)
  const [durationMinutes, setDurationMinutes] = useState(60)
  const [custom, setCustom] = useState('')
  const [openJob, setOpenJob] = useState<JobSummary | null>(null)
  const [openFinding, setOpenFinding] = useState<FindingData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const queryClient = useQueryClient()

  const jobsQuery = useQuery({
    queryKey: ['aging-jobs'],
    queryFn: endpoints.listAging,
    // A list of finished jobs does not change on its own; only a running one does.
    refetchInterval: (query) =>
      ((query.state.data as { jobs?: JobSummary[] } | undefined)?.jobs ?? []).some(
        (job) => !isTerminal(job.status),
      )
        ? LIVE_POLL_MS
        : false,
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
  const active = jobs.filter((job) => ACTIVE.has(job.status))

  if (openFinding && openJob) {
    return (
      <>
        <PageHeader
          title={openFinding.title}
          subtitle={`${openFinding.rule_id} · ${openJob.channel_name}`}
          onBack={() => setOpenFinding(null)}
          backLabel="Back to job"
          status={<SeverityChip severity={openFinding.severity as Severity} />}
        />
        <PageBody>
          <FindingInvestigation finding={openFinding} />
        </PageBody>
      </>
    )
  }

  if (openJob) {
    return (
      <>
        <PageHeader
          title={openJob.channel_name}
          subtitle={`Aging job · ${duration(openJob.elapsed_s)} elapsed`}
          onBack={() => setOpenJob(null)}
          backLabel="Back to jobs"
          status={<JobStatusChip status={openJob.status} />}
          actions={
            !ACTIVE.has(openJob.status) && (
              <>
                <a
                  className="btn-ghost btn-sm"
                  href={api.url(`/aging/jobs/${openJob.id}/report.html`)}
                  target="_blank"
                  rel="noreferrer"
                >
                  <IconFile size={14} />
                  HTML report
                </a>
                <a
                  className="btn-ghost btn-sm"
                  href={api.url(`/aging/jobs/${openJob.id}/report.pdf`)}
                  target="_blank"
                  rel="noreferrer"
                >
                  <IconDownload size={14} />
                  PDF report
                </a>
              </>
            )
          }
        />
        <PageBody>
          <JobDetail
            jobId={openJob.id}
            status={openJob.status}
            threshold={thresholds.rebuffer_ratio_threshold ?? REBUFFER_RATIO_THRESHOLD_DEFAULT}
            onOpenFinding={setOpenFinding}
          />
        </PageBody>
      </>
    )
  }

  return (
    <>
      <PageHeader
        title="Aging analysis"
        subtitle="Long-running jobs held on the server. Closing this browser does not stop them."
        status={
          <span className={active.length > 0 ? 'chip-blue' : 'chip-neutral'}>
            {active.length} running
          </span>
        }
      />

      <PageBody>
        <Card>
          <div className="px-5 pb-4 pt-4">
            <UrlForm value={form} onChange={setForm} disabled={create.isPending} />

            <div className="mt-4 grid gap-3 border-t border-surface-line pt-4 lg:grid-cols-[1fr_160px]">
              <Field label="Duration">
                <SegmentedControl
                  ariaLabel="Job duration"
                  value={custom ? -1 : durationMinutes}
                  onChange={(minutes) => {
                    setDurationMinutes(minutes)
                    setCustom('')
                  }}
                  options={AGING_PRESETS.map((preset) => ({
                    value: preset.minutes,
                    label: preset.label,
                  }))}
                />
              </Field>
              <Field label="Custom minutes" htmlFor="custom-minutes">
                <input
                  id="custom-minutes"
                  className="input"
                  type="number"
                  min={1}
                  max={1440}
                  placeholder="minutes"
                  value={custom}
                  onChange={(e) => setCustom(e.target.value)}
                />
              </Field>
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="btn-primary"
                onClick={() => create.mutate()}
                disabled={create.isPending || form.playback_url.trim().length < 8}
              >
                <IconPulse size={15} />
                Start aging run
              </button>
              <span className="text-micro text-ink-muted">
                The job resumes from its own state after a backend restart.
              </span>
            </div>

            {error && (
              <div className="mt-3">
                <InlineAlert tone="error">{error}</InlineAlert>
              </div>
            )}
          </div>
        </Card>

        <Card>
          <CardHeader
            title="Jobs"
            subtitle="Every run this deployment holds, newest first."
            actions={<span className="chip-neutral">{jobs.length}</span>}
          />
          {jobs.length === 0 ? (
            <EmptyState
              title="No aging job has been started yet"
              detail="Start a run above; it appears here immediately and keeps running on the server."
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="table table-hover">
                <thead>
                  <tr>
                    <th>Channel</th>
                    <th>Status</th>
                    <th className="w-52">Progress</th>
                    <th>Remaining</th>
                    <th>Severity counts</th>
                    <th>Verdict</th>
                    <th>Started</th>
                    <th className="text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((job) => (
                    <JobRow
                      key={job.id}
                      job={job}
                      onCancel={() => cancel.mutate(job.id)}
                      onOpen={() => setOpenJob(job)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </PageBody>
    </>
  )
}

function JobRow({
  job,
  onCancel,
  onOpen,
}: {
  job: JobSummary
  onCancel: () => void
  onOpen: () => void
}) {
  const verdict = job.verdict as VerdictData | null
  const counts = Object.entries(job.counts ?? {}).filter(([, count]) => count > 0)

  return (
    <tr>
      <td>
        <button
          type="button"
          onClick={onOpen}
          className="text-left text-body font-semibold text-ink hover:text-brand-600 hover:underline"
        >
          {job.channel_name}
        </button>
        <p className="font-mono text-[11px] text-ink-faint">{job.id}</p>
      </td>
      <td>
        <JobStatusChip status={job.status} />
      </td>
      <td>
        <ProgressBar value={job.progress} />
        <span className="mt-1 block font-mono text-[11px] text-ink-faint">
          {duration(job.elapsed_s)} elapsed
        </span>
      </td>
      <td className="font-mono text-micro text-ink-soft">{duration(job.remaining_s)}</td>
      <td>
        <div className="flex flex-wrap gap-1">
          {counts.length === 0 && <span className="text-micro text-ink-faint">—</span>}
          {counts.map(([severity, count]) => (
            <SeverityCount key={severity} severity={severity} count={count} />
          ))}
        </div>
      </td>
      <td className="text-micro text-ink-soft">{verdict?.status ?? '—'}</td>
      <td className="font-mono text-micro text-ink-soft" title={utcTitle(job.started_at)}>
        {localDateTime(job.started_at)}
      </td>
      <td>
        <div className="flex flex-wrap justify-end gap-1.5">
          <button type="button" className="btn-ghost btn-sm" onClick={onOpen}>
            Open
          </button>
          {ACTIVE.has(job.status) ? (
            <button type="button" className="btn-accent btn-sm" onClick={onCancel}>
              <IconStop size={12} />
              Cancel
            </button>
          ) : (
            <a className="btn-ghost btn-sm" href={api.url(`/jobs/${job.id}/evidence.zip`)}>
              Evidence
            </a>
          )}
        </div>
      </td>
    </tr>
  )
}

function SeverityCount({ severity, count }: { severity: string; count: number }) {
  const tone =
    severity === 'CRITICAL' || severity === 'ERROR'
      ? 'chip-pink'
      : severity === 'WARN'
        ? 'chip-violet'
        : severity === 'INFO'
          ? 'chip-blue'
          : 'chip-clean'
  return (
    <span className={cx(tone, 'tabular')}>
      {severity} {count}
    </span>
  )
}

function JobDetail({
  jobId,
  status,
  threshold,
  onOpenFinding,
}: {
  jobId: string
  status: string
  threshold: number
  onOpenFinding: (finding: FindingData) => void
}) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['aging-result', jobId],
    queryFn: () => api.get<Record<string, unknown>>(`/aging/jobs/${jobId}/result`),
    // A finished job's result is final, so it is read once and left alone.
    refetchInterval: isTerminal(status) ? false : 8000,
  })

  if (isLoading) {
    return (
      <Card>
        <EmptyState
          title="Reading the stored samples"
          detail="The job's playlist polls, segment fetches and virtual buffer series are loading."
        />
      </Card>
    )
  }
  if (error) {
    return <InlineAlert tone="error">{error instanceof Error ? error.message : String(error)}</InlineAlert>
  }

  const verdict = (data?.verdict as VerdictData | null) ?? null
  const findings = (data?.findings as FindingData[]) ?? []
  const vpb = (data?.vpb as Record<string, { series?: { at: string; level_s: number }[] }>) ?? {}
  const active = findings.filter((finding) => finding.severity !== 'PASS')
  /* Samples are written when the job settles, so a running job reports its live counts only. */
  const settled = !ACTIVE.has(status)

  return (
    <div className="space-y-4">
      <SessionVerdict
        verdict={verdict}
        threshold={threshold}
        elapsed={verdict?.window_seconds ?? 0}
      />

      <MetricRow>
        <MetricTile
          label="Active findings"
          value={active.length}
          note={`${findings.length} rules recorded`}
          tone={active.length > 0 ? 'pink' : 'clean'}
        />
        <MetricTile
          label="Incidents"
          value={verdict?.incident_count ?? 0}
          note={duration(verdict?.incident_seconds ?? 0)}
        />
        <MetricTile
          label="Playlists checked"
          value={verdict?.playlists_checked ?? 0}
          note="across every rendition"
        />
        <MetricTile
          label="Segments checked"
          value={verdict?.segments_checked ?? 0}
          note="fetched and demuxed"
        />
        <MetricTile
          label="Virtual buffer"
          value={Object.keys(vpb).length}
          note="renditions modelled"
        />
        <MetricTile
          label="Window"
          value={duration(verdict?.window_seconds ?? 0)}
          note={verdict?.worst_variant ?? 'every rendition'}
        />
      </MetricRow>

      <Card>
        <CardHeader
          title="Prioritized findings"
          subtitle="Ranked by viewer impact, then by how often the rule fired."
          actions={<span className="chip-neutral">{findings.length}</span>}
        />
        <div className="px-4 pb-4">
          <FindingsList
            findings={findings}
            onOpen={onOpenFinding}
            emptyTitle={
              settled ? 'This job recorded no finding' : 'No finding has been stored yet'
            }
            emptyDetail={
              settled
                ? 'Every check that ran returned clean for the whole window.'
                : 'The job writes its findings and samples as each window settles. The counts in the jobs table are live.'
            }
          />
        </div>
      </Card>

      {Object.keys(vpb).length > 0 && (
        <Card>
          <CardHeader
            title="Virtual Player Buffer"
            subtitle="Stored samples, one series per rendition."
          />
          <div className="overflow-x-auto">
            <table className="table table-hover">
              <thead>
                <tr>
                  <th>Rendition</th>
                  <th>Samples</th>
                  <th>Lowest level</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(vpb).map(([variant, series]) => {
                  const levels = (series.series ?? []).map((point) => point.level_s)
                  return (
                    <tr key={variant}>
                      <td className="font-mono text-micro text-ink">{variant}</td>
                      <td className="font-mono text-micro tabular-nums text-ink-soft">
                        {series.series?.length ?? 0}
                      </td>
                      <td className="font-mono text-micro tabular-nums text-ink-soft">
                        {levels.length > 0 ? `${Math.min(...levels).toFixed(1)} s` : '—'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  )
}
