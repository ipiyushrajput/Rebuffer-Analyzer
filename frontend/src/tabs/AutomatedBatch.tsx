/**
 * Automated Batch: one country, the whole pipeline, run by the backend.
 *
 * Two screens, because they are one workflow. **Start** is the catalogue listing with a
 * button on it. **Playground** is every batch this deployment has run — and the reason it
 * exists is that a batch is not a page: it scans, analyses and reports on the server, so the
 * browser that started it can close, and the run has to be findable again afterwards.
 *
 * Nothing here holds the run. The list, the progress, the log and the report all come from
 * the database, so a reload, a different browser and a restarted backend all see the same
 * batch.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import {
  ApiError,
  endpoints,
  type Batch,
  type BatchEstimate,
  type CatalogueChannel,
  type CataloguePage,
} from '../api/client'
import { PageBody, PageHeader } from '../components/layout/PageHeader'
import {
  Card,
  CardHeader,
  EmptyState,
  InlineAlert,
  Pagination,
  ProgressBar,
  Tabs,
  cx,
} from '../components/ui'
import {
  IconArrowRight,
  IconBulk,
  IconDownload,
  IconFile,
  IconSearch,
  IconStop,
} from '../components/ui/icons'
import { LIVE_POLL_MS } from '../lib/constants'

/** CASCADA reports on what viewers watch, which is production. There is no environment here. */
const ENVIRONMENT = 'PRD'

type Screen = 'start' | 'playground'

const SCREENS: { id: Screen; label: string }[] = [
  { id: 'start', label: 'Start a batch' },
  { id: 'playground', label: 'Playground' },
]

interface Search {
  country: string
  page: number
  today?: string
}

function message(error: unknown): string {
  if (error instanceof ApiError) {
    const detail = (error.detail as { detail?: unknown } | string | undefined) ?? undefined
    if (typeof detail === 'string') return detail
    const inner = (detail as { detail?: unknown } | undefined)?.detail
    if (typeof inner === 'string') return inner
    if (inner && typeof inner === 'object' && 'message' in inner) {
      return String((inner as { message: unknown }).message)
    }
    return error.message
  }
  return error instanceof Error ? error.message : String(error)
}

function utc(iso: string | null): string {
  return iso ? iso.replace('T', ' ').replace('+00:00', '').slice(0, 16) : '—'
}

function dateOnly(iso: string | null): string {
  return iso ? iso.slice(0, 10) : '—'
}

/** How far a running batch has got, in the words the operator needs. */
function progressOf(batch: Batch): { fraction: number; label: string } {
  if (batch.phase === 'SCANNING') {
    const total = batch.channels_listed || 0
    const done = batch.channels_scanned || 0
    return {
      fraction: total ? done / total : 0,
      label: total ? `Scanning ${done} / ${total}` : 'Scanning',
    }
  }
  if (batch.phase === 'ANALYSING') {
    const total = batch.channels_above || 0
    const done = batch.channels_analysed || 0
    return {
      fraction: total ? done / total : 0,
      label: total ? `Analysing ${done} / ${total}` : 'Analysing',
    }
  }
  if (batch.phase === 'REPORTING') return { fraction: 0.99, label: 'Generating report' }
  if (batch.running) return { fraction: 0, label: 'Queued' }
  return { fraction: 1, label: batch.status.replace(/_/g, ' ').toLowerCase() }
}

export function AutomatedBatchTab() {
  const queryClient = useQueryClient()
  const [screen, setScreen] = useState<Screen>('start')
  const [country, setCountry] = useState('GB')
  const [search, setSearch] = useState<Search | null>(null)
  const [confirming, setConfirming] = useState<BatchEstimate | null>(null)
  const [openId, setOpenId] = useState<string | null>(null)
  const [logFor, setLogFor] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const countries = useQuery({
    queryKey: ['catalogue-countries'],
    queryFn: endpoints.catalogueCountries,
    staleTime: Infinity,
  })

  const load = useMutation({
    mutationFn: (next: Search) =>
      endpoints.catalogueChannels({
        country: next.country,
        env: ENVIRONMENT,
        page: next.page,
        today: next.today,
      }),
    onSuccess: (page: CataloguePage, next: Search) => setSearch({ ...next, today: page.today }),
  })
  const page = load.data ?? null

  const runSearch = (next: Search) => {
    setSearch(next)
    load.mutate(next)
  }

  // --- batches --------------------------------------------------------------

  const batches = useQuery({
    queryKey: ['batches'],
    queryFn: endpoints.listBatches,
    // A finished list does not change on its own; a running batch does.
    refetchInterval: (query) =>
      ((query.state.data as { batches?: Batch[] } | undefined)?.batches ?? []).some(
        (batch) => batch.running,
      )
        ? LIVE_POLL_MS
        : false,
  })
  const rows = batches.data?.batches ?? []
  const runningFor = (code: string) => rows.find((batch) => batch.running && batch.country === code)

  const estimate = useMutation({
    mutationFn: () => endpoints.batchEstimate(country),
    onSuccess: (data) => {
      setError(null)
      setConfirming(data)
    },
    onError: (err: Error) => setError(message(err)),
  })

  const start = useMutation({
    mutationFn: () => endpoints.startBatch(country),
    onSuccess: (batch) => {
      setError(null)
      setConfirming(null)
      setOpenId(batch.id)
      setScreen('playground')
      void queryClient.invalidateQueries({ queryKey: ['batches'] })
    },
    onError: (err: Error) => setError(message(err)),
  })

  const cancel = useMutation({
    mutationFn: (id: string) => endpoints.cancelBatch(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['batches'] }),
    onError: (err: Error) => setError(message(err)),
  })

  const rerun = useMutation({
    mutationFn: (id: string) => endpoints.rerunBatch(id),
    onSuccess: (batch) => {
      setError(null)
      setOpenId(batch.id)
      void queryClient.invalidateQueries({ queryKey: ['batches'] })
    },
    onError: (err: Error) => setError(message(err)),
  })

  const detail = useQuery({
    queryKey: ['batch', openId],
    queryFn: () => endpoints.readBatch(openId as string),
    enabled: openId !== null,
    refetchInterval: (query) =>
      (query.state.data as Batch | undefined)?.running ? LIVE_POLL_MS : false,
  })

  const log = useQuery({
    queryKey: ['batch-log', logFor],
    queryFn: () => endpoints.batchLog(logFor as string),
    enabled: logFor !== null,
    // An open log on a running batch is what an operator watches to see the scan working, so
    // it follows the same rule as the rest: it polls while that batch runs and then stops.
    refetchInterval: () =>
      rows.some((batch) => batch.id === logFor && batch.running) ? LIVE_POLL_MS : false,
  })

  const pageLabel = page
    ? page.total_pages
      ? `Page ${page.page} of ${page.total_pages}`
      : `Page ${page.page}`
    : ''

  const existing = runningFor(country)

  return (
    <>
      <PageHeader
        title="Automated Batch"
        subtitle="Scan a country, analyse every channel rebuffering above the threshold, and file the report — on the analyzer, not in this tab."
        status={
          rows.some((batch) => batch.running) ? (
            <span className="chip-blue">
              {rows.filter((batch) => batch.running).length} running
            </span>
          ) : (
            <span className="chip-neutral">none running</span>
          )
        }
      />

      <PageBody>
        <Card>
          <div className="px-2 pt-1">
            <Tabs tabs={SCREENS} value={screen} onChange={setScreen} />
          </div>
        </Card>

        {error && <InlineAlert tone="error">{error}</InlineAlert>}

        {/* --- start ---------------------------------------------------------- */}
        <div className={screen === 'start' ? 'space-y-4' : 'panel-hidden'}>
          <Card>
            <div className="flex flex-wrap items-end gap-4 px-5 py-4">
              <div className="min-w-56 flex-1 basis-56">
                <label className="field-label" htmlFor="batch-country">
                  Country
                </label>
                <select
                  id="batch-country"
                  className="input"
                  value={country}
                  disabled={countries.isPending}
                  onChange={(e) => {
                    setCountry(e.target.value)
                    load.reset()
                    setSearch(null)
                    setConfirming(null)
                  }}
                >
                  {(countries.data?.countries ?? []).map((item) => (
                    <option key={item.code} value={item.code}>
                      {item.code} – {item.name}
                    </option>
                  ))}
                </select>
              </div>

              <button
                type="button"
                className="btn-ghost"
                disabled={load.isPending || countries.isPending}
                onClick={() => runSearch({ country, page: 1 })}
              >
                <IconSearch size={15} />
                {load.isPending && !page ? 'Searching' : 'Search'}
              </button>

              <button
                type="button"
                className="btn-primary"
                disabled={estimate.isPending || start.isPending}
                onClick={() => (existing ? setScreen('playground') : estimate.mutate())}
              >
                <IconBulk size={15} />
                {existing
                  ? 'Open the running batch'
                  : estimate.isPending
                    ? 'Checking'
                    : 'Start Batch'}
              </button>
            </div>

            {existing && (
              <div className="px-5 pb-4">
                <InlineAlert tone="warn">
                  A batch for {country} is already {existing.status.toLowerCase()}. Open it in
                  Playground rather than starting a second one over the same channels.
                </InlineAlert>
              </div>
            )}
            {load.isError && (
              <div className="px-5 pb-4">
                <InlineAlert tone="error">{message(load.error)}</InlineAlert>
              </div>
            )}
          </Card>

          {/* The confirmation: what will run, before it runs. */}
          {confirming && (
            <Card>
              <CardHeader
                title={`Start a batch for ${confirming.country}?`}
                subtitle="This runs on the analyzer. You can close this tab; the batch carries on."
              />
              <div className="space-y-3 px-5 pb-5">
                <dl className="grid gap-3 sm:grid-cols-4">
                  <div>
                    <dt className="field-label">Channels to scan</dt>
                    <dd className="font-mono text-body text-ink">{confirming.channels_listed}</dd>
                  </div>
                  <div>
                    <dt className="field-label">Analysis per channel</dt>
                    <dd className="font-mono text-body text-ink">
                      {confirming.analysis_duration_minutes} min
                    </dd>
                  </div>
                  <div>
                    <dt className="field-label">Analysed in parallel</dt>
                    <dd className="font-mono text-body text-ink">
                      {confirming.analysis_concurrency}
                    </dd>
                  </div>
                  <div>
                    <dt className="field-label">Rebuffering window</dt>
                    <dd className="font-mono text-body text-ink">{confirming.window_days} days</dd>
                  </div>
                </dl>

                <InlineAlert tone="info">
                  Only channels whose <strong>average</strong> rebuffering ratio over the window
                  is above the threshold are analysed, so how long this takes is not known until
                  the scan has run. If every one of the {confirming.channels_listed} channels
                  qualified it would take about{' '}
                  {Math.round(confirming.worst_case_runtime_minutes)} minutes; the cap is{' '}
                  {confirming.max_runtime_minutes} minutes, which fits{' '}
                  {confirming.channels_within_runtime} channels.
                </InlineAlert>

                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    className="btn-primary"
                    disabled={start.isPending}
                    onClick={() => start.mutate()}
                  >
                    {start.isPending ? 'Starting' : 'Start the batch'}
                    <IconArrowRight size={14} />
                  </button>
                  <button
                    type="button"
                    className="btn-ghost btn-sm"
                    onClick={() => setConfirming(null)}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </Card>
          )}

          {/* The catalogue listing, so an operator sees what the country holds. */}
          <Card>
            <CardHeader
              title={page ? `${page.country.name} · ${ENVIRONMENT}` : 'Channels'}
              subtitle={
                page
                  ? `${pageLabel} · searched for ${page.today}`
                  : 'Choose a country and search to see what a batch would scan.'
              }
            />

            {page && (
              <Pagination
                className="border-b border-surface-line px-5 py-2.5"
                label={pageLabel}
                hasPrevious={page.has_previous}
                hasNext={page.has_next}
                busy={load.isPending}
                onPrevious={() => search && runSearch({ ...search, page: page.page - 1 })}
                onNext={() => search && runSearch({ ...search, page: page.page + 1 })}
              />
            )}

            {!page ? (
              <EmptyState
                title="No channel list has been fetched yet"
                detail="A batch scans every page for the country, not just this one."
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="table table-hover">
                  <thead>
                    <tr>
                      <th>Channel</th>
                      <th>Service ID</th>
                      <th>Country</th>
                      <th>Channel name</th>
                      <th>Playback URL</th>
                    </tr>
                  </thead>
                  <tbody>
                    {page.channels.map((channel: CatalogueChannel, index: number) => (
                      <tr key={`${channel.service_id}-${channel.number}-${index}`}>
                        <td className="font-mono text-small text-ink-soft">{channel.number}</td>
                        <td className="font-mono text-small text-ink-soft">{channel.service_id}</td>
                        <td className="font-mono text-small text-ink-soft">{channel.country}</td>
                        <td className="max-w-64 truncate font-medium text-ink" title={channel.name}>
                          {channel.name}
                        </td>
                        <td className="max-w-80 truncate font-mono text-micro text-ink-muted">
                          {channel.analysable ? (
                            channel.playback_url
                          ) : (
                            <span className="text-ink-faint">no playback URL</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {page && (
              <Pagination
                className="border-t border-surface-line px-5 py-3"
                label={pageLabel}
                hasPrevious={page.has_previous}
                hasNext={page.has_next}
                busy={load.isPending}
                onPrevious={() => search && runSearch({ ...search, page: page.page - 1 })}
                onNext={() => search && runSearch({ ...search, page: page.page + 1 })}
              />
            )}
          </Card>
        </div>

        {/* --- playground ----------------------------------------------------- */}
        <div className={screen === 'playground' ? 'space-y-4' : 'panel-hidden'}>
          <Card>
            <CardHeader
              title="Batches"
              subtitle="Every batch this analyzer has run, manual and scheduled. A running one updates without a refresh."
            />
            {rows.length === 0 ? (
              <EmptyState
                title="No batch has run yet"
                detail="Start one from the Start a batch screen, or wait for the weekly schedule."
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="table table-hover">
                  <thead>
                    <tr>
                      <th>Country</th>
                      <th>Type</th>
                      <th>Window covered</th>
                      <th>Run</th>
                      <th>Status</th>
                      <th className="text-right">Scanned / above / failed</th>
                      <th className="text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((batch) => {
                      const progress = progressOf(batch)
                      return (
                        <tr
                          key={batch.id}
                          className={batch.id === openId ? 'bg-brand-50' : undefined}
                        >
                          <td className="font-mono text-small text-ink-soft">{batch.country}</td>
                          <td>
                            <span
                              className={batch.kind === 'scheduled' ? 'chip-violet' : 'chip-neutral'}
                            >
                              {batch.kind}
                            </span>
                          </td>
                          <td className="font-mono text-micro text-ink-muted">
                            {dateOnly(batch.window_from)} – {dateOnly(batch.window_to)}
                          </td>
                          <td className="font-mono text-micro text-ink-muted">
                            {utc(batch.started_at)}
                            <br />
                            {utc(batch.finished_at)}
                          </td>
                          <td className="min-w-48">
                            <span
                              className={cx(
                                batch.running
                                  ? 'chip-blue'
                                  : batch.status === 'COMPLETED'
                                    ? 'chip-clean'
                                    : batch.status === 'CANCELLED'
                                      ? 'chip-neutral'
                                      : 'chip-pink',
                              )}
                            >
                              {batch.status.replace(/_/g, ' ')}
                            </span>
                            {batch.running && (
                              <div className="mt-1.5">
                                <ProgressBar value={progress.fraction} label={progress.label} />
                              </div>
                            )}
                          </td>
                          <td className="text-right font-mono text-small text-ink-soft">
                            {batch.channels_scanned} / {batch.channels_above} /{' '}
                            <span className={batch.channels_failed ? 'text-pink-600' : ''}>
                              {batch.channels_failed}
                            </span>
                          </td>
                          <td>
                            <div className="flex flex-wrap items-center justify-end gap-2">
                              <a
                                className="btn-ghost btn-sm"
                                href={endpoints.batchReportUrl(batch.id, 'xlsx')}
                              >
                                <IconDownload size={13} />
                                XLSX
                              </a>
                              <a
                                className="btn-ghost btn-sm"
                                href={endpoints.batchReportUrl(batch.id, 'csv')}
                              >
                                <IconDownload size={13} />
                                CSV
                              </a>
                              <button
                                type="button"
                                className="btn-ghost btn-sm"
                                onClick={() => setOpenId(batch.id === openId ? null : batch.id)}
                              >
                                Details
                              </button>
                              <button
                                type="button"
                                className="btn-ghost btn-sm"
                                onClick={() => setLogFor(batch.id === logFor ? null : batch.id)}
                              >
                                <IconFile size={13} />
                                Log
                              </button>
                              {batch.running ? (
                                <button
                                  type="button"
                                  className="btn-accent btn-sm"
                                  disabled={cancel.isPending}
                                  onClick={() => cancel.mutate(batch.id)}
                                >
                                  <IconStop size={12} />
                                  Cancel
                                </button>
                              ) : (
                                <button
                                  type="button"
                                  className="btn-ghost btn-sm"
                                  disabled={rerun.isPending}
                                  onClick={() => rerun.mutate(batch.id)}
                                >
                                  Re-run
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          {/* One batch's channels. */}
          {openId && detail.data && (
            <Card>
              <CardHeader
                title={`${detail.data.country} · ${detail.data.kind} · ${detail.data.status.replace(/_/g, ' ')}`}
                subtitle={
                  detail.data.error ??
                  `${detail.data.channels_above} channel(s) above threshold of ${detail.data.channels_scanned} scanned.`
                }
                actions={
                  <button
                    type="button"
                    className="btn-ghost btn-sm"
                    onClick={() => setOpenId(null)}
                  >
                    Close
                  </button>
                }
              />
              {(detail.data.items ?? []).length === 0 ? (
                <EmptyState
                  title="No channel was above the threshold"
                  detail="Every channel in this country averaged below the rebuffering threshold over the window."
                />
              ) : (
                <div className="overflow-x-auto">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Channel</th>
                        <th>Service ID</th>
                        <th className="text-right">Avg rebuffering</th>
                        <th>Status</th>
                        <th>What the analysis found</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(detail.data.items ?? []).map((row) => (
                        <tr key={row.service_id}>
                          <td className="max-w-56 truncate font-medium text-ink" title={row.channel_name}>
                            {row.channel_name}
                          </td>
                          <td className="font-mono text-small text-ink-soft">{row.service_id}</td>
                          <td className="text-right font-mono text-small text-pink-600">
                            {row.average_pct === null ? '—' : `${row.average_pct.toFixed(3)} %`}
                          </td>
                          <td>
                            <span
                              className={
                                row.status === 'ANALYSED'
                                  ? 'chip-clean'
                                  : row.status === 'FAILED'
                                    ? 'chip-pink'
                                    : 'chip-neutral'
                              }
                            >
                              {row.status}
                            </span>
                          </td>
                          <td className="max-w-160 text-small text-ink-soft">
                            {row.summary || row.error || '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          )}

          {/* The log. */}
          {logFor && (
            <Card>
              <CardHeader
                title="Batch log"
                subtitle="Every step the batch took, including the channels it could not measure."
                actions={
                  <>
                    <a className="btn-ghost btn-sm" href={endpoints.batchLogUrl(logFor)}>
                      <IconDownload size={13} />
                      Download
                    </a>
                    <button
                      type="button"
                      className="btn-ghost btn-sm"
                      onClick={() => setLogFor(null)}
                    >
                      Close
                    </button>
                  </>
                }
              />
              <div className="max-h-96 overflow-y-auto px-5 pb-5">
                {(log.data?.lines ?? []).length === 0 ? (
                  <p className="py-6 text-center text-small text-ink-muted">
                    This batch has written no log line yet.
                  </p>
                ) : (
                  <ol className="space-y-1">
                    {(log.data?.lines ?? []).map((line, index) => (
                      <li key={`${line.at}-${index}`} className="font-mono text-micro">
                        <span className="text-ink-faint">{utc(line.at)}</span>{' '}
                        <span
                          className={
                            line.level === 'ERROR'
                              ? 'text-pink-600'
                              : line.level === 'WARN'
                                ? 'text-violet-500'
                                : 'text-ink-muted'
                          }
                        >
                          {line.level}
                        </span>{' '}
                        <span className="text-ink-soft">{line.message}</span>
                      </li>
                    ))}
                  </ol>
                )}
              </div>
            </Card>
          )}
        </div>
      </PageBody>
    </>
  )
}
