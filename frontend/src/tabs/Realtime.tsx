/**
 * Realtime tab.
 *
 * Paste a playback URL, analyse live, and generate a report at any moment from the data
 * collected so far. The player loads through the proxy; every player event goes back over
 * the session socket so a stall names its cause.
 *
 * Opening a finding replaces the page body with its investigation. Nothing is unmounted —
 * the session panel is hidden with CSS — so the player, the socket and every sample survive
 * the trip into the evidence and back.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, endpoints } from '../api/client'
import {
  AvSkewChart,
  BitrateChart,
  DiscontinuityChart,
  DownloadRatioChart,
  DownloadsGanttChart,
  DurationChart,
  FetchTimingChart,
  FreshnessChart,
  PlayedRungChart,
  PlayerBufferChart,
  PlaylistStateChart,
  RebufferRatioChart,
  SequenceLadderChart,
  StatusHeatmapChart,
  TrafficChart,
  VirtualBufferChart,
} from '../components/charts'
import { EventFeed } from '../components/EventFeed'
import { FindingsList } from '../components/FindingCard'
import { FindingInvestigation } from '../components/FindingInvestigation'
import { LadderTable, type LadderRow } from '../components/LadderTable'
import { PageBody, PageHeader } from '../components/layout/PageHeader'
import { ManifestDiff, ManifestViewer } from '../components/ManifestViewer'
import { Player } from '../components/Player'
import { SessionVerdict } from '../components/SessionVerdict'
import { UrlForm, emptyForm, toPayload, type UrlFormValue } from '../components/UrlForm'
import {
  Card,
  CardHeader,
  EmptyState,
  InlineAlert,
  LiveChip,
  MetricRow,
  MetricTile,
  SeverityChip,
  Tabs,
  cx,
} from '../components/ui'
import { IconDownload, IconFile, IconPulse, IconStop } from '../components/ui/icons'
import {
  PLAYER_METRICS_NOTE,
  REBUFFER_RATIO_THRESHOLD_DEFAULT,
  SEVERITIES,
  type Severity,
} from '../lib/constants'
import { duration, localTime, ratio } from '../lib/format'
import { findingList, useSessionStore } from '../store/session'
import type { FindingData, VerdictData } from '../ws/messages'
import { useSessionSocket } from '../ws/useSessionSocket'

type EvidenceTab = 'master' | 'variants' | 'ladder' | 'network' | 'events' | 'flow'

const EVIDENCE_TABS: { id: EvidenceTab; label: string }[] = [
  { id: 'master', label: 'Master playlist' },
  { id: 'variants', label: 'Child playlists' },
  { id: 'ladder', label: 'Ladder audit' },
  { id: 'network', label: 'Redirects and headers' },
  { id: 'events', label: 'Event feed' },
  { id: 'flow', label: 'Time travel' },
]

interface Props {
  thresholds: Record<string, number>
  /** Called with the session id once a stopped analysis has been filed. */
  onArchived: (sessionId: string) => void
}

export function RealtimeTab({ thresholds, onArchived }: Props) {
  const [form, setForm] = useState<UrlFormValue>(emptyForm(false))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [evidenceTab, setEvidenceTab] = useState<EvidenceTab>('master')
  const [manifests, setManifests] = useState<
    { layer: string; variant: string; url: string; raw: string; at: string }[]
  >([])
  const [flow, setFlow] = useState<
    { raw: string; diff: string[]; at: string; variant: string } | null
  >(null)
  const [reportUrl, setReportUrl] = useState<string | null>(null)
  const [openFinding, setOpenFinding] = useState<FindingData | null>(null)

  const store = useSessionStore()
  const { sendPlayerEvent } = useSessionSocket(store.sessionId)
  const findings = useMemo(() => findingList(store), [store])
  const ratioThreshold = thresholds.rebuffer_ratio_threshold ?? REBUFFER_RATIO_THRESHOLD_DEFAULT

  const running = store.sessionId != null && store.status !== 'STOPPED'

  const start = async () => {
    setError(null)
    setReportUrl(null)
    setOpenFinding(null)
    setBusy(true)
    try {
      const session = await endpoints.createRealtime(toPayload(form))
      store.start(session.id, session.channel_name, session.player_url)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  /**
   * Stopping ends the analysis rather than freezing it.
   *
   * The session is cancelled, a report is written from what was collected, and the tab is
   * returned to the state it was in before the run — no player, no samples, no verdict — so
   * the next analysis starts clean. The finished run is filed under Analysed channels, which
   * is where the operator is taken.
   */
  const stop = async () => {
    const sessionId = store.sessionId
    if (!sessionId) return
    setBusy(true)
    setError(null)
    try {
      await endpoints.stopRealtime(sessionId)

      /*
       * Stopping waits for the analysis to settle, so the result the report needs is ready
       * here. It is still best effort: a report that fails to render must not cost the
       * operator the run, which is filed either way with its verdict and findings.
       */
      try {
        await api.post(`/realtime/sessions/${sessionId}/report?format=html`)
      } catch {
        /* the analysis is filed regardless; only the report file is missing */
      }

      store.reset()
      setForm(emptyForm(false))
      setManifests([])
      setFlow(null)
      setReportUrl(null)
      setOpenFinding(null)
      setEvidenceTab('master')
      onArchived(sessionId)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const generateReport = async (format: 'html' | 'pdf') => {
    if (!store.sessionId) return
    setBusy(true)
    setError(null)
    try {
      const response = await api.post<{ id: number; url: string }>(
        `/realtime/sessions/${store.sessionId}/report?format=${format}`,
      )
      setReportUrl(response.url)
      window.open(api.url(response.url.replace('/api', '')), '_blank', 'noopener')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  // Manifests refresh on a slow cadence; the charts already carry the live signal.
  useEffect(() => {
    if (!store.sessionId) return undefined
    let cancelled = false
    const load = async () => {
      try {
        const data = await endpoints.realtimeManifests(store.sessionId!)
        if (!cancelled) setManifests(data.manifests)
      } catch {
        /* a transient manifest fetch failure never interrupts the session */
      }
    }
    void load()
    const timer = window.setInterval(load, 8000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [store.sessionId])

  const openFlow = useCallback(
    async (variant: string, at?: string) => {
      if (!store.sessionId) return
      try {
        const snapshot = await endpoints.realtimeSnapshot(store.sessionId, variant, at)
        setFlow({ raw: snapshot.raw, diff: snapshot.diff, at: snapshot.at, variant })
        setEvidenceTab('flow')
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      }
    },
    [store.sessionId],
  )

  /*
   * An event arrives flattened: the socket envelope's `data` is spread onto the record, so
   * a master event is `{kind, url, variants, renditions, ts}` with no nested `data`. Reading
   * `event.data.variants` found nothing, which left the declared series of the bitrate chart
   * and the whole playlist-state chart empty.
   */
  const declaredBandwidth = useMemo(() => {
    const out: Record<string, number | null> = {}
    for (const event of store.events) {
      if (event.kind !== 'master') continue
      const variants = (event.variants ?? []) as { id: string; bandwidth: number | null }[]
      for (const variant of variants) out[variant.id] = variant.bandwidth
    }
    return out
  }, [store.events])

  const playlistStates = useMemo(
    () =>
      store.events
        .filter((event) => event.kind === 'playlist_state')
        .map((event) => ({
          at: event.ts,
          // The rendition is on the envelope for a state change, not in the payload.
          variant: String(event.variant_id ?? event.variant ?? ''),
          state: String(event.to ?? ''),
        }))
        .filter((entry) => entry.state !== '')
        .reverse(),
    [store.events],
  )

  const ladderRows = useMemo<LadderRow[]>(() => {
    const verdict = store.verdict as (VerdictData & { ladder?: LadderRow[] }) | null
    return verdict?.ladder ?? []
  }, [store.verdict])

  const variantManifests = manifests.filter((m) => m.variant !== 'master')
  const masterManifest = manifests.find((m) => m.variant === 'master')

  /* The manifest the investigation shows is the one from the finding's own rendition. */
  const investigationManifest = useMemo(() => {
    if (!openFinding) return null
    const match =
      manifests.find((m) => m.variant === openFinding.variant) ??
      (openFinding.variant ? undefined : masterManifest)
    return match ? { title: match.variant, raw: match.raw, url: match.url } : null
  }, [openFinding, manifests, masterManifest])

  const activeFindings = findings.filter((finding) => finding.severity !== 'PASS')
  const latestBuffer = store.playerSamples.at(-1)?.buffer_s ?? null
  const windowSeconds = store.verdict?.window_seconds ?? store.snapshots.at(-1)?.window_s ?? null
  const segmentsChecked = store.verdict?.segments_checked ?? store.segments.length
  const worstSeverity = activeFindings[0]?.severity as Severity | undefined

  return (
    <>
      {/* --- investigation ---------------------------------------------------- */}
      <div className={openFinding ? '' : 'panel-hidden'}>
        {openFinding && (
          <>
            <PageHeader
              title={openFinding.title}
              subtitle={`${openFinding.rule_id} · ${store.channelName || 'Realtime session'}`}
              onBack={() => setOpenFinding(null)}
              backLabel="Back to session"
              status={<SeverityChip severity={openFinding.severity as Severity} />}
            />
            <PageBody>
              <FindingInvestigation finding={openFinding} manifest={investigationManifest} />
            </PageBody>
          </>
        )}
      </div>

      {/* --- the session ------------------------------------------------------ */}
      <div className={openFinding ? 'panel-hidden' : ''}>
        <PageHeader
          title="Realtime analysis"
          subtitle={
            store.channelName ||
            'Paste a playback URL; every parameter is sent verbatim and every redirect hop is recorded.'
          }
          status={
            <>
              <LiveChip label={running ? store.status : 'Idle'} live={running && store.connected} />
              <span className="font-mono text-micro text-ink-muted">
                {duration(store.elapsed)}
              </span>
            </>
          }
          actions={
            <>
              <button
                type="button"
                className="btn-ghost btn-sm"
                onClick={() => generateReport('html')}
                disabled={!store.sessionId || busy}
              >
                <IconFile size={14} />
                HTML report
              </button>
              <button
                type="button"
                className="btn-ghost btn-sm"
                onClick={() => generateReport('pdf')}
                disabled={!store.sessionId || busy}
              >
                <IconDownload size={14} />
                PDF report
              </button>
              <button
                type="button"
                className="btn-accent btn-sm"
                onClick={stop}
                disabled={!running || busy}
                title="Ends the analysis, files it under Analysed channels, and clears this tab"
              >
                <IconStop size={13} />
                {busy && running ? 'Stopping' : 'Stop and save'}
              </button>
            </>
          }
        />

        <PageBody>
          <Card>
            <div className="px-5 pb-4 pt-4">
              <UrlForm value={form} onChange={setForm} disabled={running || busy} />
              <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-surface-line pt-4">
                <button
                  type="button"
                  className="btn-primary"
                  onClick={start}
                  disabled={busy || running || form.playback_url.trim().length < 8}
                >
                  <IconPulse size={15} />
                  Analyse stream
                </button>
                <span className="text-micro text-ink-muted">
                  Player measurements are taken on the {PLAYER_METRICS_NOTE.toLowerCase()}, not on a
                  television. Stopping files the run under Analysed channels and clears this tab.
                </span>
              </div>
              {error && (
                <div className="mt-3">
                  <InlineAlert tone="error">{error}</InlineAlert>
                </div>
              )}
              {reportUrl && (
                <div className="mt-3">
                  <InlineAlert tone="clean">
                    The report is stored at{' '}
                    <a
                      className="font-mono underline"
                      href={api.url(reportUrl.replace('/api', ''))}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {reportUrl}
                    </a>
                  </InlineAlert>
                </div>
              )}
            </div>
          </Card>

          <SessionVerdict
            verdict={store.verdict}
            threshold={ratioThreshold}
            elapsed={store.elapsed}
          />

          <MetricRow>
            <MetricTile
              label="Rebuffer ratio"
              value={ratio(store.verdict?.measured_rebuffer_ratio ?? null)}
              note={`threshold ${ratioThreshold}`}
              tone={
                (store.verdict?.measured_rebuffer_ratio ?? 0) > ratioThreshold ? 'pink' : 'clean'
              }
            />
            <MetricTile
              label="Risk score"
              value={store.verdict?.risk_score ?? '—'}
              suffix="/ 100"
              note={store.verdict?.owner_label ?? 'no owner assigned yet'}
              tone={(store.verdict?.risk_score ?? 0) >= 70 ? 'pink' : 'blue'}
            />
            <MetricTile
              label="Active findings"
              value={activeFindings.length}
              note={
                SEVERITIES.filter((severity) => (store.counts[severity] ?? 0) > 0)
                  .map((severity) => `${severity.toLowerCase()} ${store.counts[severity]}`)
                  .join(' · ') || 'no rule has fired'
              }
              tone={
                worstSeverity === 'CRITICAL' || worstSeverity === 'ERROR'
                  ? 'pink'
                  : worstSeverity === 'WARN'
                    ? 'violet'
                    : 'default'
              }
            />
            <MetricTile
              label="Player buffer"
              value={latestBuffer == null ? '—' : latestBuffer.toFixed(1)}
              suffix="s"
              note={PLAYER_METRICS_NOTE}
            />
            <MetricTile
              label="Segments checked"
              value={segmentsChecked}
              note={`${store.verdict?.playlists_checked ?? store.snapshots.length} playlist polls`}
            />
            <MetricTile
              label="Live window"
              value={windowSeconds == null ? '—' : windowSeconds.toFixed(0)}
              suffix="s"
              note={store.verdict?.worst_variant ?? 'every rendition'}
            />
          </MetricRow>

          <div className="grid gap-4 2xl:grid-cols-[minmax(360px,420px)_1fr]">
            <div className="space-y-4">
              <Player src={store.playerUrl} onEvent={sendPlayerEvent} />

              <Card>
                <CardHeader
                  title="Prioritized findings"
                  subtitle="Ranked by viewer impact, then by how often the rule fired."
                  actions={
                    activeFindings.length > 0 && (
                      <span className="chip-neutral">{activeFindings.length}</span>
                    )
                  }
                />
                <div className="max-h-[62vh] space-y-2.5 overflow-y-auto px-4 pb-4">
                  <FindingsList findings={findings} onOpen={setOpenFinding} />
                </div>
              </Card>
            </div>

            <div className="grid gap-4 2xl:grid-cols-2">
              <PlayerBufferChart samples={store.playerSamples} stalls={store.stalls} />
              <RebufferRatioChart
                stalls={store.stalls}
                startedAt={store.startedAt}
                threshold={ratioThreshold}
              />
              <PlayedRungChart samples={store.playerSamples} />
              <DownloadRatioChart
                segments={store.segments}
                warn={thresholds.download_ratio_warn ?? 0.5}
                error={thresholds.download_ratio_error ?? 1}
              />
              <FetchTimingChart snapshots={store.snapshots} />
              <StatusHeatmapChart segments={store.segments} />
              <SequenceLadderChart snapshots={store.snapshots} />
              <DiscontinuityChart snapshots={store.snapshots} />
              <FreshnessChart snapshots={store.snapshots} />
              <BitrateChart segments={store.segments} declared={declaredBandwidth} />
              <AvSkewChart
                segments={store.segments}
                criticalMs={thresholds.av_pts_delta_critical_ms ?? 1000}
              />
              <DurationChart segments={store.segments} />
              <VirtualBufferChart points={store.vpbPoints} playerSamples={store.playerSamples} />
              <DownloadsGanttChart
                segments={store.segments}
                onSelect={(segment) => openFlow(segment.variant, segment.at)}
              />
              <TrafficChart segments={store.segments} />
              <PlaylistStateChart transitions={playlistStates} />
            </div>
          </div>

          {/* --- evidence ------------------------------------------------------ */}
          <Card>
            <div className="px-2 pt-1">
              <Tabs tabs={EVIDENCE_TABS} value={evidenceTab} onChange={setEvidenceTab} />
            </div>

            {/* Panels are hidden with CSS rather than unmounted, so the video ref survives. */}
            <div className={evidenceTab === 'master' ? 'p-4' : 'panel-hidden'}>
              {masterManifest ? (
                <ManifestViewer
                  title="Master playlist"
                  raw={masterManifest.raw}
                  url={masterManifest.url}
                  at={masterManifest.at}
                  highlight={(line) => line.startsWith('#EXT-X-STREAM-INF')}
                />
              ) : (
                <EmptyState
                  title="No master playlist has been fetched yet"
                  detail="It appears here the moment the playback URL resolves, with every redirect hop recorded."
                />
              )}
            </div>

            <div
              className={cx(
                evidenceTab === 'variants' ? 'grid gap-3 p-4 lg:grid-cols-2' : 'panel-hidden',
              )}
            >
              {variantManifests.map((manifest) => (
                <ManifestViewer
                  key={`${manifest.layer}:${manifest.variant}`}
                  title={`${manifest.variant} (${manifest.layer})`}
                  raw={manifest.raw}
                  url={manifest.url}
                  at={manifest.at}
                  highlight={(line) =>
                    line.startsWith('#EXT-X-DISCONTINUITY') || line.startsWith('#EXT-X-CUE')
                  }
                />
              ))}
              {variantManifests.length === 0 && (
                <div className="lg:col-span-2">
                  <EmptyState
                    title="No child playlist has been polled yet"
                    detail="Each rendition appears here once its first poll completes."
                  />
                </div>
              )}
            </div>

            <div className={evidenceTab === 'ladder' ? '' : 'panel-hidden'}>
              <LadderTable rows={ladderRows} />
            </div>

            <div className={evidenceTab === 'network' ? 'p-4' : 'panel-hidden'}>
              <pre className="evidence manifest-pane max-h-96">
                {JSON.stringify(
                  store.events.filter(
                    (e) => e.kind === 'resolve' || e.kind === 'dns' || e.kind === 'tls',
                  ),
                  null,
                  2,
                )}
              </pre>
            </div>

            <div className={evidenceTab === 'events' ? '' : 'panel-hidden'}>
              <EventFeed events={store.events} />
            </div>

            <div className={evidenceTab === 'flow' ? 'p-4' : 'panel-hidden'}>
              {flow ? (
                <div className="space-y-3">
                  <p className="text-small text-ink-muted">
                    <span className="font-semibold text-ink">{flow.variant}</span> as fetched at{' '}
                    <span className="font-mono">{localTime(flow.at)}</span>.
                  </p>
                  <ManifestViewer title="Snapshot" raw={flow.raw} at={flow.at} />
                  <Card>
                    <CardHeader title="Diff against the previous poll" />
                    <ManifestDiff diff={flow.diff} />
                  </Card>
                </div>
              ) : (
                <EmptyState
                  title="No moment has been selected"
                  detail="Click a bar on the Downloads chart to open the playlist exactly as it was at that instant, with a diff against the poll before it."
                />
              )}
            </div>
          </Card>
        </PageBody>
      </div>
    </>
  )
}
