/**
 * Analysed channels.
 *
 * Every analysis that has finished lands here — the Realtime tab clears itself the moment
 * the operator stops, so this is where the verdict, the findings and the reports are read
 * afterwards. The list comes from the database, so a channel analysed before the last
 * restart is still here, and deleting one takes its measurements and its report files with
 * it.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { api, endpoints, type AnalysedChannel, type ChannelReport } from '../api/client'
import { FindingsList } from '../components/FindingCard'
import { FindingInvestigation } from '../components/FindingInvestigation'
import { PageBody, PageHeader } from '../components/layout/PageHeader'
import { SessionVerdict } from '../components/SessionVerdict'
import {
  Card,
  CardHeader,
  EmptyState,
  InlineAlert,
  MetricRow,
  MetricTile,
  SeverityChip,
  cx,
} from '../components/ui'
import {
  IconClose,
  IconDownload,
  IconFile,
  IconSearch,
  IconTrash,
} from '../components/ui/icons'
import { REBUFFER_RATIO_THRESHOLD_DEFAULT, type Severity } from '../lib/constants'
import { bytes, duration, localDateTime, utcTitle } from '../lib/format'
import type { FindingData, VerdictData } from '../ws/messages'

const STATUS_CHIP: Record<string, string> = {
  COMPLETED: 'chip-clean',
  CANCELLED: 'chip-neutral',
  FAILED: 'chip-pink',
}

function verdictChip(status: string | null | undefined): string {
  if (status === 'NO STREAM-SIDE DEFECT') return 'chip-clean'
  if (status === 'REBUFFERING RISK — STREAM DEFECT') return 'chip-violet'
  if (status) return 'chip-pink'
  return 'chip-neutral'
}

function ReportLinks({ reports }: { reports: ChannelReport[] }) {
  if (reports.length === 0) {
    return <span className="text-micro text-ink-faint">none</span>
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {reports.map((report) => (
        <a
          key={report.id}
          className={cx('btn-ghost btn-sm', !report.exists && 'pointer-events-none opacity-50')}
          href={`${api.url(report.url.replace('/api', ''))}?inline=1`}
          target="_blank"
          rel="noreferrer"
          title={`${bytes(report.size_bytes)} · ${localDateTime(report.created_at)}`}
        >
          {report.format === 'pdf' ? <IconDownload size={13} /> : <IconFile size={13} />}
          {report.format.toUpperCase()}
        </a>
      ))}
    </div>
  )
}

interface Props {
  thresholds: Record<string, number>
  /** Set when the operator has just stopped an analysis, so it opens where they left off. */
  openId: string | null
  onOpenChange: (id: string | null) => void
}

export function ChannelsTab({ thresholds, openId, onOpenChange }: Props) {
  const [filter, setFilter] = useState('')
  const [confirming, setConfirming] = useState<string | null>(null)
  const [openFinding, setOpenFinding] = useState<FindingData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const queryClient = useQueryClient()

  const listQuery = useQuery({
    queryKey: ['channels'],
    queryFn: endpoints.listChannels,
    refetchInterval: 20000,
  })

  const remove = useMutation({
    mutationFn: (id: string) => endpoints.deleteChannel(id),
    onSuccess: (_data, id) => {
      setError(null)
      setConfirming(null)
      if (openId === id) onOpenChange(null)
      void queryClient.invalidateQueries({ queryKey: ['channels'] })
      void queryClient.invalidateQueries({ queryKey: ['reports'] })
    },
    onError: (err: Error) => setError(err.message),
  })

  const channels = listQuery.data?.channels ?? []
  const visible = channels.filter((channel) =>
    filter
      ? `${channel.channel_name} ${channel.verdict?.status ?? ''} ${channel.type}`
          .toLowerCase()
          .includes(filter.toLowerCase())
      : true,
  )

  // Opening a different channel starts from its summary, not from a stale finding.
  useEffect(() => {
    setOpenFinding(null)
  }, [openId])

  if (openId) {
    return (
      <ChannelDetail
        channelId={openId}
        thresholds={thresholds}
        openFinding={openFinding}
        onOpenFinding={setOpenFinding}
        onBack={() => onOpenChange(null)}
        onDelete={() => remove.mutate(openId)}
      />
    )
  }

  const withDefect = channels.filter(
    (channel) => channel.verdict?.status && channel.verdict.status !== 'NO STREAM-SIDE DEFECT',
  ).length
  const reportCount = channels.reduce((total, channel) => total + channel.reports.length, 0)

  return (
    <>
      <PageHeader
        title="Analysed channels"
        subtitle="Every finished analysis, with the verdict it reached and the reports it produced."
        status={<span className="chip-neutral">{channels.length} stored</span>}
      />

      <PageBody>
        <MetricRow>
          <MetricTile label="Channels" value={channels.length} note="analyses kept" />
          <MetricTile
            label="With a defect"
            value={withDefect}
            note="verdict names a stream-side defect"
            tone={withDefect > 0 ? 'pink' : 'clean'}
          />
          <MetricTile
            label="Clean"
            value={
              channels.filter((c) => c.verdict?.status === 'NO STREAM-SIDE DEFECT').length
            }
            note="no stream-side defect"
            tone="clean"
          />
          <MetricTile
            label="Highest risk"
            value={
              channels.length
                ? Math.max(...channels.map((c) => Number(c.verdict?.risk_score ?? 0)))
                : '—'
            }
            suffix="/ 100"
            tone="pink"
          />
          <MetricTile label="Reports" value={reportCount} note="stored against these runs" />
          <MetricTile
            label="Newest"
            value={
              <span className="text-body">
                {channels[0] ? localDateTime(channels[0].created_at) : '—'}
              </span>
            }
          />
        </MetricRow>

        {error && <InlineAlert tone="error">{error}</InlineAlert>}

        <Card>
          <CardHeader
            title="Channels"
            subtitle="Newest first. Opening a channel shows its findings, its incidents and its reports."
            actions={
              <label className="relative">
                <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-faint">
                  <IconSearch size={14} />
                </span>
                <input
                  className="input max-w-[240px] pl-8"
                  placeholder="Filter channels"
                  value={filter}
                  onChange={(event) => setFilter(event.target.value)}
                />
              </label>
            }
          />

          {visible.length === 0 ? (
            <EmptyState
              title={
                channels.length === 0
                  ? 'No channel has been analysed yet'
                  : 'No channel matches this filter'
              }
              detail={
                channels.length === 0
                  ? 'Run an analysis from the Realtime or Aging tab. Stopping it files the result here with its verdict and reports.'
                  : 'Clear the filter to see every analysed channel.'
              }
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="table table-hover">
                <thead>
                  <tr>
                    <th>Channel</th>
                    <th>Verdict</th>
                    <th>Responsible party</th>
                    <th>Risk</th>
                    <th>Findings</th>
                    <th>Analysed</th>
                    <th>Reports</th>
                    <th className="text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((channel) => (
                    <ChannelRow
                      key={channel.id}
                      channel={channel}
                      confirming={confirming === channel.id}
                      onOpen={() => onOpenChange(channel.id)}
                      onAskDelete={() => setConfirming(channel.id)}
                      onCancelDelete={() => setConfirming(null)}
                      onConfirmDelete={() => remove.mutate(channel.id)}
                      deleting={remove.isPending}
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

function ChannelRow({
  channel,
  confirming,
  deleting,
  onOpen,
  onAskDelete,
  onCancelDelete,
  onConfirmDelete,
}: {
  channel: AnalysedChannel
  confirming: boolean
  deleting: boolean
  onOpen: () => void
  onAskDelete: () => void
  onCancelDelete: () => void
  onConfirmDelete: () => void
}) {
  const verdict = channel.verdict as VerdictData | null
  const counts = Object.entries(channel.counts ?? {}).filter(
    ([severity, count]) => count > 0 && severity !== 'PASS',
  )
  const total = counts.reduce((sum, [, count]) => sum + count, 0)

  return (
    <tr>
      <td>
        <button
          type="button"
          onClick={onOpen}
          className="text-left text-body font-semibold text-ink hover:text-brand-600 hover:underline"
        >
          {channel.channel_name}
        </button>
        <p className="mt-0.5 flex items-center gap-1.5 text-[11px] text-ink-faint">
          <span className={STATUS_CHIP[channel.status] ?? 'chip-neutral'}>{channel.type}</span>
          <span className="font-mono">{duration(channel.elapsed_s)}</span>
        </p>
      </td>
      <td>
        <span className={verdictChip(verdict?.status)}>{verdict?.status ?? 'no verdict'}</span>
      </td>
      <td className="text-micro text-ink-soft">{verdict?.owner_label ?? '—'}</td>
      <td
        className={cx(
          'font-mono text-body font-semibold tabular-nums',
          Number(verdict?.risk_score ?? 0) >= 70 ? 'text-pink-600' : 'text-ink',
        )}
      >
        {verdict?.risk_score ?? '—'}
      </td>
      <td className="font-mono text-micro tabular-nums text-ink-soft">{total || '—'}</td>
      <td className="font-mono text-micro text-ink-soft" title={utcTitle(channel.created_at)}>
        {localDateTime(channel.created_at)}
      </td>
      <td>
        <ReportLinks reports={channel.reports} />
      </td>
      <td>
        <div className="flex justify-end gap-1.5">
          {confirming ? (
            <>
              <button
                type="button"
                className="btn-accent btn-sm"
                onClick={onConfirmDelete}
                disabled={deleting}
              >
                Delete for good
              </button>
              <button type="button" className="btn-ghost btn-sm" onClick={onCancelDelete}>
                <IconClose size={12} />
              </button>
            </>
          ) : (
            <>
              <button type="button" className="btn-ghost btn-sm" onClick={onOpen}>
                Open
              </button>
              <button
                type="button"
                className="btn-ghost btn-sm text-pink-600"
                onClick={onAskDelete}
                aria-label={`Delete ${channel.channel_name}`}
              >
                <IconTrash size={13} />
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  )
}

function ChannelDetail({
  channelId,
  thresholds,
  openFinding,
  onOpenFinding,
  onBack,
  onDelete,
}: {
  channelId: string
  thresholds: Record<string, number>
  openFinding: FindingData | null
  onOpenFinding: (finding: FindingData | null) => void
  onBack: () => void
  onDelete: () => void
}) {
  const [confirming, setConfirming] = useState(false)
  const { data, isLoading, error } = useQuery({
    queryKey: ['channel', channelId],
    queryFn: () => endpoints.readChannel(channelId),
  })

  if (isLoading) {
    return (
      <>
        <PageHeader title="Analysed channel" onBack={onBack} backLabel="Back to channels" />
        <PageBody>
          <Card>
            <EmptyState
              title="Reading the stored analysis"
              detail="The verdict, the findings and the virtual-buffer series are loading."
            />
          </Card>
        </PageBody>
      </>
    )
  }

  if (error || !data) {
    return (
      <>
        <PageHeader title="Analysed channel" onBack={onBack} backLabel="Back to channels" />
        <PageBody>
          <InlineAlert tone="error">
            {error instanceof Error ? error.message : 'This channel could not be read.'}
          </InlineAlert>
        </PageBody>
      </>
    )
  }

  const verdict = (data.verdict as VerdictData | null) ?? null
  const findings = ((data.findings as FindingData[]) ?? []).slice().sort((a, b) => {
    const rank: Record<string, number> = { CRITICAL: 4, ERROR: 3, WARN: 2, INFO: 1, PASS: 0 }
    return rank[b.severity] - rank[a.severity] || b.count - a.count
  })
  const incidents = (data.incidents as Record<string, unknown>[]) ?? []
  const vpb = (data.vpb as Record<string, { series?: { level_s: number }[] }>) ?? {}
  const reports = data.reports ?? []
  const active = findings.filter((finding) => finding.severity !== 'PASS')

  if (openFinding) {
    return (
      <>
        <PageHeader
          title={openFinding.title}
          subtitle={`${openFinding.rule_id} · ${data.channel_name}`}
          onBack={() => onOpenFinding(null)}
          backLabel="Back to channel"
          status={<SeverityChip severity={openFinding.severity as Severity} />}
        />
        <PageBody>
          <FindingInvestigation finding={openFinding} />
        </PageBody>
      </>
    )
  }

  return (
    <>
      <PageHeader
        title={data.channel_name}
        subtitle={`${data.type} analysis · ${duration(data.elapsed_s)} · ${localDateTime(
          data.created_at,
        )}`}
        onBack={onBack}
        backLabel="Back to channels"
        status={<span className={STATUS_CHIP[data.status] ?? 'chip-neutral'}>{data.status}</span>}
        actions={
          confirming ? (
            <>
              <span className="text-small text-ink-muted">
                Delete this analysis and its {reports.length} report(s)?
              </span>
              <button type="button" className="btn-accent btn-sm" onClick={onDelete}>
                Delete for good
              </button>
              <button
                type="button"
                className="btn-ghost btn-sm"
                onClick={() => setConfirming(false)}
              >
                Keep
              </button>
            </>
          ) : (
            <>
              <ReportLinks reports={reports} />
              <button
                type="button"
                className="btn-ghost btn-sm text-pink-600"
                onClick={() => setConfirming(true)}
              >
                <IconTrash size={13} />
                Delete
              </button>
            </>
          )
        }
      />

      <PageBody>
        <SessionVerdict
          verdict={verdict}
          threshold={thresholds.rebuffer_ratio_threshold ?? REBUFFER_RATIO_THRESHOLD_DEFAULT}
          elapsed={data.elapsed_s}
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
            value={verdict?.incident_count ?? incidents.length}
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
          <MetricTile label="Reports" value={reports.length} note="stored for this run" />
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
              emptyTitle="This analysis recorded no finding"
              emptyDetail="Every check that ran returned clean for the whole window."
            />
          </div>
        </Card>

        {incidents.length > 0 && (
          <Card>
            <CardHeader
              title="Incidents"
              subtitle="Each rebuffer window, with the finding it was correlated against."
            />
            <div className="overflow-x-auto">
              <table className="table table-hover">
                <thead>
                  <tr>
                    <th>Kind</th>
                    <th>Rendition</th>
                    <th>Started</th>
                    <th>Duration</th>
                    <th>Cause</th>
                  </tr>
                </thead>
                <tbody>
                  {incidents.map((incident, index) => (
                    <tr key={index}>
                      <td className="text-micro text-ink-soft">{String(incident.kind)}</td>
                      <td className="font-mono text-micro text-ink-soft">
                        {String(incident.variant ?? '—')}
                      </td>
                      <td
                        className="font-mono text-micro text-ink-soft"
                        title={utcTitle(String(incident.started_at))}
                      >
                        {localDateTime(String(incident.started_at))}
                      </td>
                      <td className="font-mono text-micro tabular-nums text-ink-soft">
                        {duration(Number(incident.duration_s))}
                      </td>
                      <td className="max-w-[420px] text-micro text-ink-muted">
                        {String(incident.detail ?? '—')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}

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
      </PageBody>
    </>
  )
}
