/**
 * All channels: the TV Plus catalogue for one country in one environment.
 *
 * The list comes from the analyzer host, not the browser — the catalogue sends no CORS
 * headers and the environment mapping belongs on the server. A row goes straight into
 * Realtime or Aging with the channel filled in; it never starts the analysis, because the
 * operator still chooses the duration and the checks.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { ApiError, endpoints, type CatalogueChannel, type CataloguePage } from '../api/client'
import { PageBody, PageHeader } from '../components/layout/PageHeader'
import {
  Card,
  CardHeader,
  CopyButton,
  EmptyState,
  InlineAlert,
  Pagination,
  SegmentedControl,
} from '../components/ui'
import { IconAging, IconPulse, IconSearch } from '../components/ui/icons'
import type { ChannelPrefill } from '../lib/prefill'

/** What the operator has chosen but not yet searched for. */
interface Selection {
  country: string
  env: string
}

/** A selection that has been searched, with the date that search pinned. */
interface Search extends Selection {
  page: number
  today?: string
}

interface Props {
  /** Hands a channel to the Realtime or Aging tab and switches to it. */
  onAnalyse: (target: 'realtime' | 'aging', prefill: Omit<ChannelPrefill, 'token'>) => void
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

export function AllChannelsTab({ onAnalyse }: Props) {
  const [selection, setSelection] = useState<Selection>({ country: 'US', env: 'PRD' })
  const [search, setSearch] = useState<Search | null>(null)
  const [filter, setFilter] = useState('')

  const countries = useQuery({
    queryKey: ['catalogue-countries'],
    queryFn: endpoints.catalogueCountries,
    staleTime: Infinity,
  })

  /*
   * The list is fetched on Search and on a page change, never on a keystroke: each call
   * reaches an internal service, and changing country or environment is a new search rather
   * than a filter over what is already on screen.
   */
  const load = useMutation({
    mutationFn: (next: Search) =>
      endpoints.catalogueChannels({
        country: next.country,
        env: next.env,
        page: next.page,
        today: next.today,
      }),
    onSuccess: (page: CataloguePage, next: Search) => {
      // Every later page repeats the date the first search ran with.
      setSearch({ ...next, today: page.today })
    },
  })

  const page = load.data ?? null
  const runSearch = (next: Search) => {
    setSearch(next)
    load.mutate(next)
  }

  const select = (patch: Partial<Selection>) => {
    setSelection({ ...selection, ...patch })
    // A different country or environment is a different list, so paging starts over.
    load.reset()
    setSearch(null)
    setFilter('')
  }

  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase()
    if (!page) return []
    if (!needle) return page.channels
    return page.channels.filter(
      (channel) =>
        channel.name.toLowerCase().includes(needle) ||
        channel.service_id.toLowerCase().includes(needle) ||
        channel.number.toLowerCase().includes(needle),
    )
  }, [page, filter])

  const pageLabel = page
    ? page.total_pages
      ? `Page ${page.page} of ${page.total_pages}`
      : `Page ${page.page}`
    : ''

  return (
    <>
      <PageHeader
        title="All channels"
        subtitle="The TV Plus channel list for a country and environment, ready to analyse."
        status={
          page ? (
            <span className="font-mono text-micro text-ink-muted">
              {page.total != null ? `${page.total} channels` : `${page.channels.length} on this page`}
            </span>
          ) : undefined
        }
      />

      <PageBody>
        {/* --- filters -------------------------------------------------------- */}
        <Card>
          <div className="flex flex-wrap items-end gap-4 px-5 py-4">
            <div className="min-w-56 flex-1 basis-56">
              <label className="field-label" htmlFor="catalogue-country">
                Country
              </label>
              <select
                id="catalogue-country"
                className="input"
                value={selection.country}
                disabled={countries.isPending}
                onChange={(e) => select({ country: e.target.value })}
              >
                {(countries.data?.countries ?? []).map((country) => (
                  <option key={country.code} value={country.code}>
                    {country.code} – {country.name}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <span className="field-label">Environment</span>
              <SegmentedControl
                value={selection.env}
                onChange={(env) => select({ env })}
                options={(countries.data?.environments ?? ['PRD', 'STG']).map((env) => ({
                  value: env,
                  label: env,
                }))}
              />
            </div>

            <button
              type="button"
              className="btn-primary"
              disabled={load.isPending || countries.isPending}
              onClick={() => runSearch({ ...selection, page: 1 })}
            >
              <IconSearch size={15} />
              {load.isPending && !page ? 'Searching' : 'Search'}
            </button>

            {page && (
              <div className="min-w-48 flex-1 basis-48">
                <label className="field-label" htmlFor="catalogue-filter">
                  Filter this page
                </label>
                <input
                  id="catalogue-filter"
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

        {/* --- the list ------------------------------------------------------- */}
        <Card>
          <CardHeader
            title={page ? `${page.country.name} · ${page.environment}` : 'Channels'}
            subtitle={
              page
                ? `${pageLabel} · searched for ${page.today}`
                : 'Choose a country and an environment, then search.'
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
              detail="Pick a country and an environment, then press Search."
            />
          ) : visible.length === 0 ? (
            <EmptyState
              title={
                page.channels.length === 0
                  ? 'The catalogue returned no channel for this selection'
                  : 'No channel on this page matches the filter'
              }
              detail={
                page.channels.length === 0
                  ? 'Another environment or country may carry it.'
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
                    <th>Playback URL</th>
                    <th className="text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((channel, index) => (
                    <ChannelRow
                      /* The backend drops rows the catalogue publishes twice; the index keeps
                         the key unique even if an origin finds a new way to repeat one. */
                      key={`${channel.service_id}-${channel.number}-${index}`}
                      channel={channel}
                      onAnalyse={onAnalyse}
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
    </>
  )
}

function ChannelRow({
  channel,
  onAnalyse,
}: {
  channel: CatalogueChannel
  onAnalyse: Props['onAnalyse']
}) {
  const prefill = {
    playback_url: channel.playback_url,
    channel_name: channel.name,
    service_id: channel.service_id,
    country: channel.country,
  }
  return (
    <tr>
      <td className="font-mono text-small text-ink-soft">{channel.number}</td>
      <td className="font-mono text-small text-ink-soft">{channel.service_id}</td>
      <td className="font-mono text-small text-ink-soft">{channel.country}</td>
      <td className="max-w-64 truncate font-medium text-ink" title={channel.name}>
        {channel.name}
      </td>
      <td>
        {channel.analysable ? (
          <div className="flex items-center gap-2">
            <span
              className="block max-w-80 truncate font-mono text-micro text-ink-muted"
              title={channel.playback_url}
            >
              {channel.playback_url}
            </span>
            <CopyButton text={channel.playback_url} label="Copy" />
          </div>
        ) : (
          <span className="text-small text-ink-faint">
            The catalogue lists no playback URL for this channel
          </span>
        )}
      </td>
      <td>
        <div className="flex items-center justify-end gap-2">
          {/* A channel with no URL is still listed — it exists — but there is nothing to run. */}
          <button
            type="button"
            className="btn-ghost btn-sm"
            disabled={!channel.analysable}
            title={
              channel.analysable
                ? 'Open Realtime with this channel filled in'
                : 'This channel has no playback URL to analyse'
            }
            onClick={() => onAnalyse('realtime', prefill)}
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
            onClick={() => onAnalyse('aging', prefill)}
          >
            <IconAging size={13} />
            Aging
          </button>
        </div>
      </td>
    </tr>
  )
}
