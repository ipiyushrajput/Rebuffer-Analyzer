/**
 * Error Data: one channel's playback errors this week, and the week before it.
 *
 * The same panel as Rebuffering Data over a different quantity. `errors` is a count per
 * minute summed over every device, not a share of viewing time, so the tiles state a total as
 * well as an average, and the week-on-week change is given relative to last week — a count
 * moves with the audience, and a percentage change is what compares a large channel with a
 * small one.
 *
 * There is no error threshold until one is set in Settings. Without one, no minute is drawn
 * in pink and the panel states no above/below verdict, because none has been measured
 * against anything.
 */

import {
  type CascadaChannelQuery,
  type CascadaErrorChannel,
  endpoints,
} from '../api/client'
import { CHART_HEIGHT, CascadaChart, WeekAxesNote } from './CascadaChart'
import { CascadaLoading, CascadaModalFrame } from './CascadaModal'
import { localTitle, utcLabel } from './RebufferingModal'
import { InlineAlert, MetricRow, MetricTile } from './ui'
import { IconAging, IconDownload, IconPulse } from './ui/icons'

/** An error count as the panel writes it: grouped thousands, `digits` places at most. */
export function formatCount(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString('en-US', { maximumFractionDigits: digits })
}

/**
 * A large count shortened to fit a tile — `5.96M` — with the exact figure stated beside it.
 *
 * A week of errors on a busy channel runs to millions, and seven grouped digits in the tile's
 * type size do not fit its width.
 */
export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (Math.abs(value) < 1_000_000) return formatCount(value)
  return value.toLocaleString('en-US', { notation: 'compact', maximumFractionDigits: 2 })
}

/** A relative change with its sign, or a dash when there is no comparison week. */
export function formatChange(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${value > 0 ? '+' : ''}${value.toFixed(1)} %`
}

const tooltipCount = (value: number) => `${formatCount(value, 2)} errors`

interface Props {
  query: CascadaChannelQuery
  channel: CascadaErrorChannel | null
  loading: boolean
  error: string | null
  showComparison: boolean
  onToggleComparison: (next: boolean) => void
  onClose: () => void
  onAnalyse: (target: 'realtime' | 'aging') => void
  analysable: boolean
}

export function ErrorDataModal({
  query,
  channel,
  loading,
  error,
  showComparison,
  onToggleComparison,
  onClose,
  onAnalyse,
  analysable,
}: Props) {
  const threshold = channel?.threshold_per_min ?? null
  const change = channel?.week_over_week_change_pct ?? null
  const delta = channel?.week_over_week_delta_per_min ?? null

  return (
    <CascadaModalFrame
      label={`Error data for ${query.channel_name}`}
      title={query.channel_name}
      subtitle={
        channel ? (
          <span title={localTitle(channel.window.start)}>
            <span className="font-mono">{query.service_id}</span>
            {query.country && ` · ${query.country}`} ·{' '}
            <span className="font-mono">
              {utcLabel(channel.window.start)} → {utcLabel(channel.window.end)} UTC
            </span>{' '}
            ({channel.window.days.toFixed(1)} days)
          </span>
        ) : (
          <span className="font-mono">{query.service_id}</span>
        )
      }
      status={
        channel &&
        (threshold === null ? (
          <span className="chip-neutral">No error threshold set</span>
        ) : (
          <span className={channel.above_threshold ? 'chip-pink' : 'chip-clean'}>
            {channel.above_threshold ? 'Above threshold' : 'Below threshold'}
          </span>
        ))
      }
      onClose={onClose}
    >
      {error && <InlineAlert tone="error">{error}</InlineAlert>}

      {loading && !channel && <CascadaLoading what="error data" channel={query.channel_name} />}

      {channel && (
        <>
          {channel.truncated && (
            <InlineAlert tone="warn">
              CASCADA returned {channel.minutes_counted} measured minute(s) for a window of{' '}
              {channel.window.minutes}, so these figures cover less than the window above.
            </InlineAlert>
          )}

          <MetricRow>
            <MetricTile
              label="Average / min"
              value={formatCount(channel.average_per_min, 1)}
              tone={threshold === null ? 'default' : channel.above_threshold ? 'pink' : 'clean'}
              note={
                threshold === null ? 'no threshold set' : `threshold ${formatCount(threshold, 2)} / min`
              }
            />
            <MetricTile
              label="Maximum / min"
              value={formatCount(channel.max_per_min)}
              tone="violet"
              note={utcLabel(channel.max_at)}
            />
            <MetricTile
              label="Total errors"
              value={<span title={formatCount(channel.total)}>{formatCompact(channel.total)}</span>}
              note={`${formatCount(channel.total)} over ${formatCount(channel.minutes_counted)} measured minute(s)`}
            />
            <MetricTile
              label="Minutes above"
              value={threshold === null ? '—' : channel.minutes_above}
              tone={channel.minutes_above > 0 ? 'violet' : 'default'}
              note={
                threshold === null
                  ? 'set a threshold in Settings → CASCADA'
                  : `of ${channel.minutes_counted} measured`
              }
            />
            <MetricTile
              label="Previous week / min"
              value={formatCount(channel.previous_week_average_per_min, 1)}
              note="average, for comparison"
            />
            <MetricTile
              label="Week on week"
              value={formatChange(change)}
              tone={change === null ? 'default' : change > 0 ? 'pink' : 'clean'}
              note={
                delta === null
                  ? 'no comparison week'
                  : `${delta > 0 ? '+' : ''}${formatCount(delta, 1)} / min, ${
                      delta > 0 ? 'more than last week' : 'fewer than last week'
                    }`
              }
            />
          </MetricRow>

          <div className="card">
            <div className="flex flex-wrap items-center justify-between gap-2 px-4 pb-1 pt-3.5">
              <h3 className="text-body font-semibold text-ink">Errors per minute</h3>
              <label className="flex items-center gap-2 text-small text-ink-soft">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-brand-600"
                  checked={showComparison}
                  onChange={(e) => onToggleComparison(e.target.checked)}
                  disabled={channel.comparison.length === 0}
                />
                Previous week overlay
              </label>
            </div>
            <div className="px-1.5 pb-2">
              {channel.origin.length === 0 ? (
                <p
                  className="flex items-center justify-center px-4 text-center text-small text-ink-muted"
                  style={{ height: CHART_HEIGHT }}
                >
                  CASCADA measured no minute of this channel inside the window.
                </p>
              ) : (
                <CascadaChart
                  origin={channel.origin}
                  comparison={channel.comparison}
                  showComparison={showComparison}
                  threshold={threshold}
                  thresholdLabel={
                    threshold === null ? undefined : `threshold ${formatCount(threshold, 2)} / min`
                  }
                  yName="errors per minute"
                  formatValue={tooltipCount}
                />
              )}
            </div>
            <p className="px-4 pb-3 text-micro text-ink-faint">
              {threshold === null
                ? 'No error threshold is set, so no minute is marked; set one in Settings → CASCADA to draw it.'
                : `Minutes above ${formatCount(threshold, 2)} errors / min are drawn in pink; the rest in blue.`}{' '}
              A minute CASCADA reported nothing for is a gap, not a zero —{' '}
              {channel.minutes_missing} such minute(s) in this window.{' '}
              <WeekAxesNote showComparison={showComparison && channel.comparison.length > 0} />
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <a className="btn-ghost btn-sm" href={endpoints.cascadaErrorsReportUrl(query, 'csv')}>
              <IconDownload size={13} />
              Download CSV
            </a>
            <a className="btn-ghost btn-sm" href={endpoints.cascadaErrorsReportUrl(query, 'xlsx')}>
              <IconDownload size={13} />
              Download XLSX
            </a>
            <span className="flex-1" />
            <button
              type="button"
              className="btn-ghost btn-sm"
              disabled={!analysable}
              title={
                analysable
                  ? 'Open Realtime with this channel filled in'
                  : 'The catalogue lists no playback URL for this channel'
              }
              onClick={() => onAnalyse('realtime')}
            >
              <IconPulse size={13} />
              Realtime
            </button>
            <button
              type="button"
              className="btn-ghost btn-sm"
              disabled={!analysable}
              title={
                analysable
                  ? 'Open Aging with this channel filled in'
                  : 'The catalogue lists no playback URL for this channel'
              }
              onClick={() => onAnalyse('aging')}
            >
              <IconAging size={13} />
              Aging
            </button>
          </div>

          <p className="text-micro text-ink-faint">
            Measured at <span className="font-mono">{utcLabel(channel.fetched_at)}</span> UTC
            {channel.cached && ' · served from the stored window'}. CASCADA is queried with
            channel_country=ALL, so this is the channel&apos;s errors across every country it runs
            in, counted in {channel.unit}.
          </p>
        </>
      )}
    </CascadaModalFrame>
  )
}
