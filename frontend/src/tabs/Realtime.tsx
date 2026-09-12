/**
 * Realtime tab.
 *
 * Paste a playback URL, analyse live, and generate a report at any moment from the data
 * collected so far. The player loads through the proxy; every player event goes back over
 * the session socket so a stall names its cause.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { endpoints, api } from '../api/client'
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
import { FindingsFeed } from '../components/FindingCard'
import { LadderTable, type LadderRow } from '../components/LadderTable'
import { ManifestDiff, ManifestViewer } from '../components/ManifestViewer'
import { Player } from '../components/Player'
import { UrlForm, emptyForm, toPayload, type UrlFormValue } from '../components/UrlForm'
import { VerdictBanner } from '../components/VerdictBanner'
import { REBUFFER_RATIO_THRESHOLD_DEFAULT } from '../lib/constants'
import { duration, localTime } from '../lib/format'
import { findingList, useSessionStore } from '../store/session'
import { useSessionSocket } from '../ws/useSessionSocket'
import type { VerdictData } from '../ws/messages'

type BottomTab = 'master' | 'variants' | 'ladder' | 'network' | 'events' | 'flow'

interface Props {
  thresholds: Record<string, number>
}

export function RealtimeTab({ thresholds }: Props) {
  const [form, setForm] = useState<UrlFormValue>(emptyForm(false))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [bottomTab, setBottomTab] = useState<BottomTab>('master')
  const [manifests, setManifests] = useState<
    { layer: string; variant: string; url: string; raw: string; at: string }[]
  >([])
  const [flow, setFlow] = useState<{ raw: string; diff: string[]; at: string; variant: string } | null>(
    null,
  )
  const [reportUrl, setReportUrl] = useState<string | null>(null)

  const store = useSessionStore()
  const { sendPlayerEvent } = useSessionSocket(store.sessionId)
  const findings = useMemo(() => findingList(store), [store])
  const ratioThreshold = thresholds.rebuffer_ratio_threshold ?? REBUFFER_RATIO_THRESHOLD_DEFAULT

  const running = store.sessionId != null && store.status !== 'STOPPED'

  const start = async () => {
    setError(null)
    setReportUrl(null)
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

  const stop = async () => {
    if (!store.sessionId) return
    setBusy(true)
    try {
      await endpoints.stopRealtime(store.sessionId)
      useSessionStore.setState({ status: 'STOPPED' })
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
        setBottomTab('flow')
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      }
    },
    [store.sessionId],
  )

  const declaredBandwidth = useMemo(() => {
    const out: Record<string, number | null> = {}
    for (const event of store.events) {
      if (event.kind !== 'master') continue
      const data = (event.data ?? {}) as { variants?: { id: string; bandwidth: number | null }[] }
      for (const variant of data.variants ?? []) out[variant.id] = variant.bandwidth
    }
    return out
  }, [store.events])

  const playlistStates = useMemo(
    () =>
      store.events
        .filter((event) => event.kind === 'playlist_state')
        .map((event) => ({
          at: event.ts,
          variant: String(event.variant ?? ''),
          state: String((event.data as Record<string, unknown>)?.to ?? ''),
        }))
        .reverse(),
    [store.events],
  )

  const ladderRows = useMemo<LadderRow[]>(() => {
    const verdict = store.verdict as (VerdictData & { ladder?: LadderRow[] }) | null
    return verdict?.ladder ?? []
  }, [store.verdict])

  const variantManifests = manifests.filter((m) => m.variant !== 'master')
  const masterManifest = manifests.find((m) => m.variant === 'master')

  return (
    <div className="space-y-4">
      <section className="card px-4 py-4">
        <UrlForm value={form} onChange={setForm} disabled={running || busy} />
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="btn-primary"
            onClick={start}
            disabled={busy || running || form.playback_url.trim().length < 8}
          >
            Analyse
          </button>
          <button type="button" className="btn-danger" onClick={stop} disabled={!running || busy}>
            Stop
          </button>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => generateReport('html')}
            disabled={!store.sessionId || busy}
          >
            Generate report (HTML)
          </button>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => generateReport('pdf')}
            disabled={!store.sessionId || busy}
          >
            Generate report (PDF)
          </button>

          <span className="ml-auto flex items-center gap-3 text-sm">
            <span
              className={`chip ${
                store.connected
                  ? 'border-pass bg-green-50 text-pass'
                  : 'border-slate-200 bg-slate-50 text-[var(--rba-muted)]'
              }`}
            >
              {store.status}
            </span>
            <span className="text-[var(--rba-muted)]">elapsed {duration(store.elapsed)}</span>
          </span>
        </div>

        {error && (
          <p className="mt-3 rounded border border-critical bg-red-50 px-3 py-2 text-sm text-critical">
            {error}
          </p>
        )}
        {reportUrl && (
          <p className="mt-3 rounded border border-pass bg-green-50 px-3 py-2 text-sm text-pass">
            The report is ready at{' '}
            <a className="underline" href={api.url(reportUrl.replace('/api', ''))} target="_blank" rel="noreferrer">
              {reportUrl}
            </a>
            .
          </p>
        )}
      </section>

      <div className="sticky top-2 z-20">
        <VerdictBanner verdict={store.verdict} threshold={ratioThreshold} elapsed={store.elapsed} />
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(380px,520px)_1fr]">
        <div className="space-y-4">
          <Player src={store.playerUrl} onEvent={sendPlayerEvent} />
          <section className="card">
            <div className="card-header">
              <h2 className="card-title">Findings</h2>
              <span className="text-xs text-[var(--rba-muted)]">
                {Object.entries(store.counts)
                  .filter(([, count]) => count > 0)
                  .map(([severity, count]) => `${severity} ${count}`)
                  .join(' · ') || 'none yet'}
              </span>
            </div>
            <div className="max-h-[70vh] space-y-2 overflow-y-auto p-2">
              <FindingsFeed findings={findings} />
            </div>
          </section>
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

      <section className="card">
        <div className="card-header">
          <nav className="flex flex-wrap gap-1" aria-label="Detail panels">
            {(
              [
                ['master', 'Master playlist'],
                ['variants', 'Child playlists'],
                ['ladder', 'Ladder audit'],
                ['network', 'Redirects and headers'],
                ['events', 'Event feed'],
                ['flow', 'Flow / time travel'],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                className={`tab-button ${bottomTab === id ? 'tab-button-active' : ''}`}
                onClick={() => setBottomTab(id)}
              >
                {label}
              </button>
            ))}
          </nav>
        </div>

        {/* Panels are hidden with CSS rather than unmounted, so the video ref survives. */}
        <div className={bottomTab === 'master' ? 'p-3' : 'panel-hidden'}>
          {masterManifest ? (
            <ManifestViewer
              title="Master playlist"
              raw={masterManifest.raw}
              url={masterManifest.url}
              at={masterManifest.at}
              highlight={(line) => line.startsWith('#EXT-X-STREAM-INF')}
            />
          ) : (
            <p className="py-6 text-sm text-[var(--rba-muted)]">
              The master playlist appears once the playback URL resolves.
            </p>
          )}
        </div>

        <div className={bottomTab === 'variants' ? 'grid gap-3 p-3 lg:grid-cols-2' : 'panel-hidden'}>
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
            <p className="py-6 text-sm text-[var(--rba-muted)]">
              Child playlists appear once the first poll completes.
            </p>
          )}
        </div>

        <div className={bottomTab === 'ladder' ? '' : 'panel-hidden'}>
          <LadderTable rows={ladderRows} />
        </div>

        <div className={bottomTab === 'network' ? 'p-3' : 'panel-hidden'}>
          <pre className="manifest-pane mono max-h-96 rounded border border-[var(--rba-line)] bg-slate-50 p-3">
            {JSON.stringify(
              store.events.filter((e) => e.kind === 'resolve' || e.kind === 'dns' || e.kind === 'tls'),
              null,
              2,
            )}
          </pre>
        </div>

        <div className={bottomTab === 'events' ? '' : 'panel-hidden'}>
          <EventFeed events={store.events} />
        </div>

        <div className={bottomTab === 'flow' ? 'p-3' : 'panel-hidden'}>
          {flow ? (
            <div className="space-y-3">
              <p className="text-sm">
                <span className="font-semibold">{flow.variant}</span> as fetched at{' '}
                {localTime(flow.at)}.
              </p>
              <ManifestViewer title="Snapshot" raw={flow.raw} at={flow.at} />
              <div className="card">
                <div className="card-header">
                  <h3 className="card-title">Diff against the previous poll</h3>
                </div>
                <ManifestDiff diff={flow.diff} />
              </div>
            </div>
          ) : (
            <p className="py-6 text-sm text-[var(--rba-muted)]">
              Click a bar on the Downloads chart to open the playlist exactly as it was at that
              moment, with a diff against the poll before it.
            </p>
          )}
        </div>
      </section>
    </div>
  )
}
