/**
 * CASCADA Data: which channels viewers are actually rebuffering on.
 *
 * Every other tab measures a channel from the outside — RBA polls the ladder and models the
 * player. This one reads what real televisions reported, so the field signal picks the
 * targets and the rest of the product explains the cause.
 *
 * The channel list is the same one All channels shows, fetched by the backend through the
 * same catalogue module, production only. The rebuffering figures come from CASCADA, also
 * through the backend: the API needs a session cookie and sends no CORS headers, so the
 * browser never calls it.
 *
 * A channel is a rebuffering channel on its **average** over the window, never on a single
 * minute — that is what the country scan is for, and one spike does not qualify a channel.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import {
  ApiError,
  endpoints,
  type CascadaChannel,
  type CascadaChannelQuery,
  type CascadaScan,
  type CatalogueChannel,
  type CataloguePage,
} from '../api/client'
import { CascadaSessionBanner } from '../components/CascadaSession'
import { PageBody, PageHeader } from '../components/layout/PageHeader'
import { RebufferingModal, formatPct, utcLabel } from '../components/RebufferingModal'
import {
  Card,
  CardHeader,
  EmptyState,
  InlineAlert,
  Pagination,
  ProgressBar,
  cx,
} from '../components/ui'
import {
  IconAging,
  IconBulk,
  IconDownload,
  IconPulse,
  IconSearch,
  IconStop,
} from '../components/ui/icons'
import { partition, toFile, type CascadaBulkRow } from '../lib/cascadaBulk'
import type { ChannelPrefill } from '../lib/prefill'

/** CASCADA reports on what viewers watch, which is production. There is no environment here. */
const ENVIRONMENT = 'PRD'

const SCAN_POLL_MS = 2000

interface Search {
  country: string
  page: number
  today?: string
}

interface Props {
  onAnalyse: (target: 'realtime' | 'aging', prefill: Omit<ChannelPrefill, 'token'>) => void
  /** Hands a file of channels to the Bulk analysis tab and switches to it. */
  onBulk: (file: File) => void
}

function message(error: unknown): string {
  if (error instanceof ApiError) {
    const detail = (error.detail as { detail?: string } | string | undefined) ?? undefined
    if (typeof detail === 'string') return detail
    if (detail?.detail) return detail.detail
    return error.message
  }
  return error instanceof Error ? error.message : String(error)
}

function isSessionError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401
}

export function CascadaDataTab({ onAnalyse, onBulk }: Props) {
  const queryClient = useQueryClient()
  const [country, setCountry] = useState('US')
  const [search, setSearch] = useState<Search | null>(null)
  const [filter, setFilter] = useState('')
  const [scanId, setScanId] = useState<string | null>(null)
  const [open, setOpen] = useState<CascadaChannelQuery | null>(null)
  const [showComparison, setShowComparison] = useState(true)
  const [handoffNote, setHandoffNote] = useState<string | null>(null)

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

  const selectCountry = (next: string) => {
    setCountry(next)
    // A different country is a different list and a different scan.
    load.reset()
    setSearch(null)
    setFilter('')
    setScanId(null)
    setHandoffNote(null)
  }

  // --- the scan -------------------------------------------------------------

  const startScan = useMutation({
    mutationFn: () => endpoints.startCascadaScan({ country }),
    onSuccess: (scan: CascadaScan) => setScanId(scan.scan_id),
  })

  const scan = useQuery({
    queryKey: ['cascada-scan', scanId],
    queryFn: () => endpoints.readCascadaScan(scanId as string),
    enabled: scanId !== null,
    // The scan is polled while it runs and left alone once it has stopped.
    refetchInterval: (query) =>
      (query.state.data as CascadaScan | undefined)?.status === 'RUNNING' ? SCAN_POLL_MS : false,
  })

  const cancelScan = useMutation({
    mutationFn: () => endpoints.cancelCascadaScan(scanId as string),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['cascada-scan', scanId] })
    },
  })

  const scanState = scan.data ?? null
  const running = scanState?.status === 'RUNNING'

  /** Every measured channel by service id, so the table fills in as results arrive. */
  const measured = useMemo(() => {
    const map = new Map<string, CascadaBulkRow>()
    for (const row of scanState?.above ?? []) map.set(row.service_id, row as CascadaBulkRow)
    return map
  }, [scanState])

  // --- one channel ----------------------------------------------------------

  const channel = useQuery({
    queryKey: ['cascada-channel', open?.service_id, open?.channel_name],
    queryFn: () => endpoints.cascadaChannel(open as CascadaChannelQuery),
    enabled: open !== null,
  })

  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase()
    if (!page) return []
    if (!needle) return page.channels
    return page.channels.filter(
      (item) =>
        item.name.toLowerCase().includes(needle) ||
        item.service_id.toLowerCase().includes(needle) ||
        item.number.toLowerCase().includes(needle),
    )
  }, [page, filter])

  const prefillFor = (item: CatalogueChannel) => ({
    playback_url: item.playback_url,
    channel_name: item.name,
    service_id: item.service_id,
    country: item.country,
  })

  const sendToBulk = () => {
    if (!scanState) return
    const { analysable, withoutUrl } = partition(scanState.above as CascadaBulkRow[])
    if (analysable.length === 0) {
      setHandoffNote(
        'No rebuffering channel in this country carries a playback URL, so there is nothing ' +
          'for Bulk analysis to fetch.',
      )
      return
    }
    onBulk(toFile(analysable, scanState.country))
    setHandoffNote(
      `${analysable.length} channel(s) sent to Bulk analysis` +
        (withoutUrl.length > 0
          ? `. ${withoutUrl.length} above-threshold channel(s) were left out because the ` +
            'catalogue lists no playback URL for them; they are still in the country report.'
          : '.'),
    )
  }

  const scanError = startScan.isError ? startScan.error : scan.isError ? scan.error : null
  const sessionProblem =
    isSessionError(scanError) || isSessionError(channel.error) || scanState?.status === 'AUTH_FAILED'

  const pageLabel = page
    ? page.total_pages
      ? `Page ${page.page} of ${page.total_pages}`
      : `Page ${page.page}`
    : ''

  return (
    <>
      <PageHeader
        title="CASCADA Data"
        subtitle="Rebuffering measured on real televisions, week on week, for the channels TV Plus runs."
        status={
          scanState ? (
            <span className="font-mono text-micro text-ink-muted">
              {scanState.above_count} above {scanState.threshold_pct} % of{' '}
              {scanState.measured_count} measured
            </span>
          ) : undefined
        }
      />

      <PageBody>
        {sessionProblem && (
          <CascadaSessionBanner
            detail={
              scanState?.error ??
              message(scanError ?? channel.error ?? 'CASCADA refused the call.')
            }
          />
        )}

        {/* --- country and search --------------------------------------------- */}
        <Card>
          <div className="flex flex-wrap items-end gap-4 px-5 py-4">
            <div className="min-w-56 flex-1 basis-56">
              <label className="field-label" htmlFor="cascada-country">
                Country
              </label>
              <select
                id="cascada-country"
                className="input"
                value={country}
                disabled={countries.isPending}
                onChange={(e) => selectCountry(e.target.value)}
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
              className="btn-primary"
              disabled={load.isPending || countries.isPending}
              onClick={() => runSearch({ country, page: 1 })}
            >
              <IconSearch size={15} />
              {load.isPending && !page ? 'Searching' : 'Search'}
            </button>

            {page && (
              <div className="min-w-48 flex-1 basis-48">
                <label className="field-label" htmlFor="cascada-filter">
                  Filter this page
                </label>
                <input
                  id="cascada-filter"
                  className="input"
                  placeholder="Channel name, service ID or number"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                />
              </div>
            )}
          </div>

          {countries.isError && (
            <div className="px-5 pb-4">
              <InlineAlert tone="error">{message(countries.error)}</InlineAlert>
            </div>
          )}
          {load.isError && (
            <div className="px-5 pb-4">
              <InlineAlert tone="error">{message(load.error)}</InlineAlert>
            </div>
          )}
        </Card>

        {/* --- the country scan ------------------------------------------------ */}
        {page && (
          <Card>
            <CardHeader
              title="Rebuffering scan"
              subtitle={
                scanState
                  ? `Every channel in ${scanState.country}, measured over ${scanState.window.days.toFixed(1)} days to ${utcLabel(scanState.window.end)} UTC.`
                  : 'Measures every channel in this country, across every page, and marks the ones whose average is above the threshold.'
              }
              actions={
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    className="btn-primary btn-sm"
                    disabled={running || startScan.isPending}
                    onClick={() => startScan.mutate()}
                  >
                    {running
                      ? 'Scanning'
                      : startScan.isPending
                        ? 'Starting'
                        : 'Scan rebuffering (all channels)'}
                  </button>
                  {running && (
                    <button
                      type="button"
                      className="btn-ghost btn-sm"
                      disabled={cancelScan.isPending}
                      onClick={() => cancelScan.mutate()}
                    >
                      <IconStop size={13} />
                      Cancel
                    </button>
                  )}
                </div>
              }
            />

            <div className="space-y-3 px-5 pb-5">
              {scanError && !sessionProblem && (
                <InlineAlert tone="error">{message(scanError)}</InlineAlert>
              )}

              {scanState && (
                <>
                  <ProgressBar
                    value={scanState.progress}
                    label={`${scanState.done} / ${scanState.total} channels`}
                  />
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={scanState.above_count > 0 ? 'chip-pink' : 'chip-clean'}>
                      {scanState.above_count} above threshold
                    </span>
                    <span className="chip-neutral">{scanState.below_count} below</span>
                    {scanState.failures.length > 0 && (
                      <span className="chip-violet">{scanState.failures.length} failed</span>
                    )}
                    <span className="font-mono text-micro text-ink-muted">
                      status {scanState.status}
                    </span>
                  </div>

                  {scanState.error && !sessionProblem && (
                    <InlineAlert tone="error">{scanState.error}</InlineAlert>
                  )}

                  <div className="flex flex-wrap items-center gap-2">
                    <a
                      className={cx('btn-ghost btn-sm', scanState.above_count === 0 && 'pointer-events-none opacity-50')}
                      href={endpoints.cascadaScanReportUrl(scanState.scan_id, 'csv')}
                    >
                      <IconDownload size={13} />
                      Report CSV
                    </a>
                    <a
                      className={cx('btn-ghost btn-sm', scanState.above_count === 0 && 'pointer-events-none opacity-50')}
                      href={endpoints.cascadaScanReportUrl(scanState.scan_id, 'xlsx')}
                    >
                      <IconDownload size={13} />
                      Report XLSX
                    </a>
                    <button
                      type="button"
                      className="btn-ghost btn-sm"
                      disabled={scanState.above_count === 0}
                      onClick={sendToBulk}
                    >
                      <IconBulk size={13} />
                      Bulk analyse rebuffering channels
                    </button>
                  </div>

                  {running && (
                    <InlineAlert tone="warn">
                      The scan is still running, so a report downloaded now lists only the
                      channels measured so far.
                    </InlineAlert>
                  )}
                  {handoffNote && <InlineAlert tone="info">{handoffNote}</InlineAlert>}

                  {scanState.failures.length > 0 && (
                    <details className="rounded-tile border border-surface-line px-3.5 py-2.5">
                      <summary className="cursor-pointer text-small font-semibold text-ink-soft">
                        {scanState.failures.length} channel(s) CASCADA did not answer for
                      </summary>
                      <ul className="mt-2 space-y-1">
                        {scanState.failures.map((failure) => (
                          <li key={failure.service_id} className="text-micro text-ink-muted">
                            <span className="font-mono">{failure.service_id}</span>{' '}
                            {failure.channel_name} — {failure.reason}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </>
              )}
            </div>
          </Card>
        )}

        {/* --- the channel list ------------------------------------------------ */}
        <Card>
          <CardHeader
            title={page ? `${page.country.name} · ${ENVIRONMENT}` : 'Channels'}
            subtitle={
              page
                ? `${pageLabel} · searched for ${page.today}`
                : 'Choose a country, then search.'
            }
            actions={
              page && (
                <span className="chip-neutral">
                  {visible.length === page.channels.length
                    ? `${page.channels.length}`
                    : `${visible.length} of ${page.channels.length}`}
                </span>
              )
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

          {load.isPending ? (
            <div className="px-5 py-10 text-center text-small text-ink-muted">
              Fetching the channel list from the catalogue…
            </div>
          ) : !page ? (
            <EmptyState
              title="No channel list has been fetched yet"
              detail="Pick a country and press Search. The rebuffering figures fill in once a scan has run."
            />
          ) : visible.length === 0 ? (
            <EmptyState
              title={
                page.channels.length === 0
                  ? 'The catalogue returned no channel for this country'
                  : 'No channel on this page matches the filter'
              }
              detail={
                page.channels.length === 0
                  ? 'Another country may carry it.'
                  : 'Clear the filter to see the whole page.'
              }
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
                    <th className="text-right">Avg rebuffering (7d)</th>
                    <th className="text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((item, index) => (
                    <ChannelRow
                      /* The backend drops rows the catalogue publishes twice; the index keeps
                         the key unique even if an origin finds a new way to repeat one. */
                      key={`${item.service_id}-${item.number}-${index}`}
                      channel={item}
                      measured={measured.get(item.service_id) ?? null}
                      onOpen={() =>
                        setOpen({
                          service_id: item.service_id,
                          channel_name: item.name,
                          country: item.country,
                        })
                      }
                      onAnalyse={(target) => onAnalyse(target, prefillFor(item))}
                    />
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
      </PageBody>

      {open && (
        <RebufferingModal
          query={open}
          channel={(channel.data as CascadaChannel | undefined) ?? null}
          loading={channel.isPending}
          error={
            channel.isError && !isSessionError(channel.error) ? message(channel.error) : null
          }
          showComparison={showComparison}
          onToggleComparison={setShowComparison}
          onClose={() => setOpen(null)}
          analysable={Boolean(
            page?.channels.find((item) => item.service_id === open.service_id)?.analysable,
          )}
          onAnalyse={(target) => {
            const found = page?.channels.find((item) => item.service_id === open.service_id)
            if (found) onAnalyse(target, prefillFor(found))
            setOpen(null)
          }}
        />
      )}
    </>
  )
}

function ChannelRow({
  channel,
  measured,
  onOpen,
  onAnalyse,
}: {
  channel: CatalogueChannel
  /** The scan's result for this channel, once it has one. */
  measured: CascadaBulkRow | null
  onOpen: () => void
  onAnalyse: (target: 'realtime' | 'aging') => void
}) {
  const above = measured?.above_threshold ?? false
  return (
    /* An above-threshold row is tinted, and it also carries the word — colour is never the
       only signal. */
    <tr className={above ? 'bg-pink-50' : undefined}>
      <td className="font-mono text-small text-ink-soft">{channel.number}</td>
      <td className="font-mono text-small text-ink-soft">{channel.service_id}</td>
      <td className="font-mono text-small text-ink-soft">{channel.country}</td>
      <td className="max-w-64 truncate font-medium text-ink" title={channel.name}>
        {channel.name}
      </td>
      <td className="text-right">
        {measured ? (
          <span
            className={cx(
              'font-mono text-small font-semibold',
              above ? 'text-pink-600' : 'text-ink-soft',
            )}
          >
            {formatPct(measured.average_pct)}
            {above && <span className="ml-1.5 chip-pink">Above threshold</span>}
          </span>
        ) : (
          <span className="text-micro text-ink-faint">not scanned</span>
        )}
      </td>
      <td>
        <div className="flex items-center justify-end gap-2">
          <button type="button" className="btn-ghost btn-sm" onClick={onOpen}>
            Rebuffering Data
          </button>
          <button
            type="button"
            className="btn-ghost btn-sm"
            disabled={!channel.analysable}
            title={
              channel.analysable
                ? 'Open Realtime with this channel filled in'
                : 'This channel has no playback URL to analyse'
            }
            onClick={() => onAnalyse('realtime')}
          >
            <IconPulse size={13} />
            Realtime
          </button>
          <button
            type="button"
            className="btn-ghost btn-sm"
            disabled={!channel.analysable}
            title={
              channel.analysable
                ? 'Open Aging with this channel filled in'
                : 'This channel has no playback URL to analyse'
            }
            onClick={() => onAnalyse('aging')}
          >
            <IconAging size={13} />
            Aging
          </button>
        </div>
      </td>
    </tr>
  )
}
