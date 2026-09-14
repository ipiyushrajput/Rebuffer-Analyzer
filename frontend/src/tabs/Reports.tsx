/** Reports tab: every report generated, searchable, openable in place and re-downloadable. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { api, endpoints } from '../api/client'
import { PageBody, PageHeader } from '../components/layout/PageHeader'
import {
  Card,
  CardHeader,
  EmptyState,
  Field,
  MetricRow,
  MetricTile,
  cx,
} from '../components/ui'
import { IconClose, IconDownload, IconSearch } from '../components/ui/icons'
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

function verdictChip(status: string | null): string {
  if (status === 'NO STREAM-SIDE DEFECT') return 'chip-clean'
  if (status === 'REBUFFERING RISK — STREAM DEFECT') return 'chip-violet'
  if (status) return 'chip-pink'
  return 'chip-neutral'
}

export function ReportsTab() {
  const [channel, setChannel] = useState('')
  const [owner, setOwner] = useState('')
  const [verdict, setVerdict] = useState('')
  const [since, setSince] = useState('')
  const [open, setOpen] = useState<ReportRow | null>(null)
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
    onSuccess: () => {
      setOpen(null)
      void queryClient.invalidateQueries({ queryKey: ['reports'] })
    },
  })

  const reports = (reportsQuery.data?.reports ?? []) as unknown as ReportRow[]

  const defects = reports.filter(
    (report) => report.verdict_status && report.verdict_status !== 'NO STREAM-SIDE DEFECT',
  )
  const channels = new Set(reports.map((report) => report.channel_name ?? '—')).size
  const totalBytes = reports.reduce((sum, report) => sum + report.size_bytes, 0)
  const highestRisk = reports.length
    ? Math.max(...reports.map((report) => report.risk_score ?? 0))
    : null

  /* --- one report ------------------------------------------------------------ */
  if (open) {
    const href = api.url(open.url.replace('/api', ''))
    /* The panel renders the document itself, so it asks for the inline disposition. */
    const inlineHref = `${href}?inline=1`
    return (
      <>
        <PageHeader
          title={open.channel_name ?? 'Report'}
          subtitle={`${open.job_type} report · generated ${localDateTime(open.created_at)}`}
          onBack={() => setOpen(null)}
          backLabel="Back to library"
          status={<span className={verdictChip(open.verdict_status)}>{open.verdict_status ?? 'No verdict'}</span>}
          actions={
            <>
              <a className="btn-ghost btn-sm" href={inlineHref} target="_blank" rel="noreferrer">
                Open in a new tab
              </a>
              <a className="btn-primary btn-sm" href={href} download>
                <IconDownload size={14} />
                Download
              </a>
            </>
          }
        />
        <PageBody>
          <MetricRow>
            <MetricTile
              label="Risk score"
              value={open.risk_score ?? '—'}
              suffix="/ 100"
              tone={(open.risk_score ?? 0) >= 70 ? 'pink' : 'blue'}
            />
            <MetricTile
              label="Responsible party"
              value={open.owner ? (OWNER_LABEL[open.owner] ?? open.owner) : '—'}
            />
            <MetricTile label="Format" value={open.format.toUpperCase()} />
            <MetricTile label="Size" value={bytes(open.size_bytes)} />
            <MetricTile label="Job type" value={open.job_type} />
            <MetricTile
              label="Job"
              value={<span className="text-body">{open.job_id.slice(0, 8)}</span>}
              note={open.job_id}
            />
          </MetricRow>

          <Card className="overflow-hidden">
            <CardHeader
              title="Report"
              subtitle="The stored document, exactly as it was generated."
            />
            {open.exists && open.format === 'html' ? (
              <iframe
                title={`${open.channel_name ?? 'Report'} ${open.id}`}
                src={inlineHref}
                className="h-[72vh] w-full border-t border-surface-line bg-white"
              />
            ) : (
              <EmptyState
                title={
                  open.exists ? `This report is a ${open.format.toUpperCase()}` : 'No content is stored'
                }
                detail={
                  open.exists
                    ? 'Open it in a new tab or download it; the browser renders it outside this panel.'
                    : 'The row remains so the verdict stays searchable, and the document itself is no longer stored. Generate a new report from the job.'
                }
              />
            )}
          </Card>
        </PageBody>
      </>
    )
  }

  /* --- the library ----------------------------------------------------------- */
  return (
    <>
      <PageHeader
        title="Reports"
        subtitle="Every report this deployment has generated, with the verdict it carries."
        status={<span className="chip-neutral">{reports.length} stored</span>}
      />

      <PageBody>
        <MetricRow>
          <MetricTile label="Reports" value={reports.length} note="matching these filters" />
          <MetricTile
            label="Channels"
            value={channels}
            note="distinct channels covered"
            tone="blue"
          />
          <MetricTile
            label="With a defect"
            value={defects.length}
            note="verdict names a stream-side defect"
            tone={defects.length > 0 ? 'pink' : 'clean'}
          />
          <MetricTile
            label="Highest risk"
            value={highestRisk ?? '—'}
            suffix="/ 100"
            tone="pink"
          />
          <MetricTile label="Stored" value={bytes(totalBytes)} note="held in the database" />
          <MetricTile
            label="Newest"
            value={
              <span className="text-body">
                {reports[0] ? localDateTime(reports[0].created_at) : '—'}
              </span>
            }
          />
        </MetricRow>

        <Card className="card-pad">
          <div className="grid gap-3 md:grid-cols-4">
            <Field label="Channel" htmlFor="filter-channel">
              <div className="relative">
                <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-faint">
                  <IconSearch size={14} />
                </span>
                <input
                  id="filter-channel"
                  className="input pl-8"
                  placeholder="Channel name contains"
                  value={channel}
                  onChange={(e) => setChannel(e.target.value)}
                />
              </div>
            </Field>
            <Field label="Responsible party" htmlFor="filter-owner">
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
            </Field>
            <Field label="Verdict" htmlFor="filter-verdict">
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
            </Field>
            <Field label="Generated since" htmlFor="filter-since">
              <input
                id="filter-since"
                className="input"
                type="date"
                value={since}
                onChange={(e) => setSince(e.target.value)}
              />
            </Field>
          </div>
          {(channel || owner || verdict || since) && (
            <button
              type="button"
              className="btn-quiet mt-3"
              onClick={() => {
                setChannel('')
                setOwner('')
                setVerdict('')
                setSince('')
              }}
            >
              <IconClose size={12} />
              Clear filters
            </button>
          )}
        </Card>

        <Card>
          <CardHeader
            title="Library"
            subtitle="Newest first. Opening a report renders it in place."
            actions={<span className="chip-neutral">{reports.length}</span>}
          />
          {reports.length === 0 ? (
            <EmptyState
              title="No report matches these filters"
              detail="Reports are generated from the Realtime, Aging and Bulk tabs and appear here as soon as they are written."
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="table table-hover">
                <thead>
                  <tr>
                    <th>Channel</th>
                    <th>Type</th>
                    <th>Verdict</th>
                    <th>Responsible party</th>
                    <th>Risk</th>
                    <th>Format</th>
                    <th>Size</th>
                    <th>Generated</th>
                    <th className="text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {reports.map((report) => (
                    <tr key={report.id} className={cx(!report.exists && 'opacity-60')}>
                      <td>
                        <button
                          type="button"
                          onClick={() => setOpen(report)}
                          className="text-left text-body font-semibold text-ink hover:text-brand-600 hover:underline"
                        >
                          {report.channel_name ?? '—'}
                        </button>
                      </td>
                      <td className="text-micro capitalize text-ink-muted">{report.job_type}</td>
                      <td>
                        <span className={verdictChip(report.verdict_status)}>
                          {report.verdict_status ?? '—'}
                        </span>
                      </td>
                      <td className="text-micro text-ink-soft">
                        {report.owner ? (OWNER_LABEL[report.owner] ?? report.owner) : '—'}
                      </td>
                      <td
                        className={cx(
                          'font-mono text-body font-semibold tabular-nums',
                          (report.risk_score ?? 0) >= 70 ? 'text-pink-600' : 'text-ink',
                        )}
                      >
                        {report.risk_score ?? '—'}
                      </td>
                      <td className="text-micro uppercase text-ink-muted">{report.format}</td>
                      <td className="font-mono text-micro tabular-nums text-ink-soft">
                        {bytes(report.size_bytes)}
                      </td>
                      <td
                        className="font-mono text-micro text-ink-soft"
                        title={utcTitle(report.created_at)}
                      >
                        {localDateTime(report.created_at)}
                      </td>
                      <td>
                        <div className="flex justify-end gap-1.5">
                          {report.exists ? (
                            <button
                              type="button"
                              className="btn-ghost btn-sm"
                              onClick={() => setOpen(report)}
                            >
                              Open
                            </button>
                          ) : (
                            <span className="chip-violet">No content</span>
                          )}
                          <button
                            type="button"
                            className="btn-accent btn-sm"
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
        </Card>
      </PageBody>
    </>
  )
}
