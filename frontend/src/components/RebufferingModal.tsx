/**
 * Rebuffering Data: one channel's measured week, and the week before it.
 *
 * Every figure on this panel comes from the current week's rows. The previous week is drawn
 * behind them as a comparison and never enters an average, a maximum or a threshold check —
 * mixing the two would average a fortnight into a number labelled as this week.
 *
 * The chart, the frame and the loading state are shared with the Error Data panel; what is
 * particular to rebuffering is the unit, the threshold and the tiles.
 *
 * Two sources answer the same question. Realtime is one value per minute over the rolling
 * window; historical is one value per UTC day over the last seven complete days. The panel
 * opens on the source of the last scan and switches with the toggle; each view states which
 * source and which dates its figures come from.
 */

import {
  type CascadaChannel,
  type CascadaChannelQuery,
  type DataSource,
  endpoints,
} from '../api/client'
import { CHART_HEIGHT, CascadaChart, WeekAxesNote } from './CascadaChart'
import { CascadaLoading, CascadaModalFrame } from './CascadaModal'
import { InlineAlert, MetricRow, MetricTile, SegmentedControl } from './ui'
import { IconAging, IconDownload, IconPulse } from './ui/icons'

export { aboveOnly } from './CascadaChart'

export function formatPct(value: number | null | undefined, digits = 3): string {
  return value === null || value === undefined ? '—' : `${value.toFixed(digits)} %`
}

/** A UTC timestamp as the product writes them, with the viewer's own clock on hover. */
export function utcLabel(iso: string | null): string {
  if (!iso) return '—'
  return iso.replace('T', ' ').replace('+00:00', '').slice(0, 19)
}

export function localTitle(iso: string | null): string {
  if (!iso) return ''
  const parsed = new Date(iso)
  return Number.isNaN(parsed.valueOf()) ? '' : `Local time: ${parsed.toLocaleString()}`
}

/** A ratio as the tooltip writes it: CASCADA's own value, in percent. */
const tooltipPct = (value: number) => `${value} %`

export function RebufferingChart({
  channel,
  showComparison,
}: {
  channel: CascadaChannel
  showComparison: boolean
}) {
  return (
    <CascadaChart
      origin={channel.origin}
      comparison={channel.comparison}
      showComparison={showComparison}
      threshold={channel.threshold_pct}
      thresholdLabel={`threshold ${channel.threshold_pct} %`}
      yName="rebuffering ratio (%)"
      formatValue={tooltipPct}
    />
  )
}

export const SOURCE_OPTIONS: { value: DataSource; label: string }[] = [
  { value: 'realtime', label: 'Realtime' },
  { value: 'historical', label: 'Historical' },
]

/** A day as the panel writes it, from the midnight CASCADA dates it by. */
function dayOf(iso: string | null): string {
  return iso ? iso.slice(0, 10) : '—'
}

/** What a historical window covers, in the words every surface uses for it. */
export function historicalLabel(channel: CascadaChannel): string {
  return channel.returned_days?.label ?? channel.requested_days?.label ?? ''
}

interface Props {
  query: CascadaChannelQuery
  /** Which source the panel shows; `channel` is that source's answer. */
  source: DataSource
  onSourceChange: (next: DataSource) => void
  channel: CascadaChannel | null
  loading: boolean
  error: string | null
  showComparison: boolean
  onToggleComparison: (next: boolean) => void
  onClose: () => void
  onAnalyse: (target: 'realtime' | 'aging') => void
  analysable: boolean
}

export function RebufferingModal({
  query,
  source,
  onSourceChange,
  channel,
  loading,
  error,
  showComparison,
  onToggleComparison,
  onClose,
  onAnalyse,
  analysable,
}: Props) {
  const delta = channel?.week_over_week_delta_pct ?? null

  return (
    <CascadaModalFrame
      label={`Rebuffering data for ${query.channel_name}`}
      title={query.channel_name}
      subtitle={
        channel && channel.granularity === 'day' ? (
          <span>
            <span className="font-mono">{query.service_id}</span>
            {query.country && ` · ${query.country}`} · historical ·{' '}
            <span className="font-mono">{historicalLabel(channel)} UTC</span> (one value per day)
          </span>
        ) : channel ? (
          <span title={localTitle(channel.window.start)}>
            <span className="font-mono">{query.service_id}</span>
            {query.country && ` · ${query.country}`} · realtime ·{' '}
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
        (channel.insufficient ? (
          <span className="chip-neutral">Insufficient data</span>
        ) : (
          <span className={channel.above_threshold ? 'chip-pink' : 'chip-clean'}>
            {channel.above_threshold ? 'Above threshold' : 'Below threshold'}
          </span>
        ))
      }
      onClose={onClose}
    >
      <div className="flex flex-wrap items-center gap-3">
        <SegmentedControl
          options={SOURCE_OPTIONS}
          value={source}
          onChange={onSourceChange}
          ariaLabel="Data source"
        />
        <span className="text-micro text-ink-muted">
          {source === 'historical'
            ? 'One value per UTC day, the last 7 complete days.'
            : 'One value per minute, the rolling window to now.'}
        </span>
      </div>

      {error && <InlineAlert tone="error">{error}</InlineAlert>}

      {loading && !channel && (
        <CascadaLoading
          what={source === 'historical' ? 'historical rebuffering data' : 'rebuffering data'}
          channel={query.channel_name}
        />
      )}

      {channel && channel.granularity === 'day' && (
        <HistoricalBody
          query={query}
          channel={channel}
          analysable={analysable}
          onAnalyse={onAnalyse}
        />
      )}

      {channel && channel.granularity !== 'day' && (
        <>
          {channel.truncated && (
            <InlineAlert tone="warn">
              CASCADA returned {channel.minutes_counted} measured minute(s) for a window of{' '}
              {channel.window.minutes}, so these figures cover less than the window above.
            </InlineAlert>
          )}

          <MetricRow>
            <MetricTile
              label="Average"
              value={formatPct(channel.average_pct)}
              tone={channel.above_threshold ? 'pink' : 'clean'}
              note={`threshold ${channel.threshold_pct} %`}
            />
            <MetricTile
              label="Maximum"
              value={formatPct(channel.max_pct)}
              tone="violet"
              note={utcLabel(channel.max_at)}
            />
            <MetricTile
              label="Minutes above"
              value={channel.minutes_above}
              tone={channel.minutes_above > 0 ? 'violet' : 'default'}
              note={`of ${channel.minutes_counted} measured`}
            />
            <MetricTile
              label="Time above"
              value={
                channel.percent_time_above === null
                  ? '—'
                  : `${channel.percent_time_above.toFixed(2)} %`
              }
              note="of the measured week"
            />
            <MetricTile
              label="Previous week"
              value={formatPct(channel.previous_week_average_pct)}
              note="average, for comparison"
            />
            <MetricTile
              label="Week on week"
              value={
                delta === null
                  ? '—'
                  : `${delta > 0 ? '+' : ''}${delta.toFixed(3)}`
              }
              suffix="pp"
              tone={delta === null ? 'default' : delta > 0 ? 'pink' : 'clean'}
              note={
                delta === null ? 'no comparison week' : delta > 0 ? 'worse than last week' : 'better than last week'
              }
            />
          </MetricRow>

          <div className="card">
            <div className="flex flex-wrap items-center justify-between gap-2 px-4 pb-1 pt-3.5">
              <h3 className="text-body font-semibold text-ink">Rebuffering ratio per minute</h3>
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
                <RebufferingChart channel={channel} showComparison={showComparison} />
              )}
            </div>
            <p className="px-4 pb-3 text-micro text-ink-faint">
              Minutes above {channel.threshold_pct} % are drawn in pink; the rest in blue. A
              minute CASCADA reported nothing for is a gap, not a zero —{' '}
              {channel.minutes_missing} such minute(s) in this window.{' '}
              <WeekAxesNote showComparison={showComparison && channel.comparison.length > 0} />
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <a
              className="btn-ghost btn-sm"
              href={endpoints.cascadaChannelReportUrl(query, 'csv')}
            >
              <IconDownload size={13} />
              Download CSV
            </a>
            <a
              className="btn-ghost btn-sm"
              href={endpoints.cascadaChannelReportUrl(query, 'xlsx')}
            >
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
            channel_country=ALL, so this is the channel's rebuffering across every country it
            runs in.
          </p>
        </>
      )}
    </CascadaModalFrame>
  )
}

/**
 * The historical view: seven UTC days, one value each.
 *
 * Every figure is over the days that carry a value. A day with none is a shaded gap labelled
 * "no data" in the chart and an empty cell in the download, never a zero; and a channel with
 * fewer days than the minimum is stated as insufficient data rather than judged either way.
 */
function HistoricalBody({
  query,
  channel,
  analysable,
  onAnalyse,
}: {
  query: CascadaChannelQuery
  channel: CascadaChannel
  analysable: boolean
  onAnalyse: (target: 'realtime' | 'aging') => void
}) {
  const withData = channel.days_with_data ?? channel.minutes_counted
  const expected = channel.days_expected ?? 7
  const missing = expected - withData
  const renamed =
    channel.cascada_channel_name && channel.cascada_channel_name !== channel.channel_name
  return (
    <>
      {channel.insufficient && (
        <InlineAlert tone="warn">
          Insufficient data: {withData} of {expected} day(s) carry a value, below the minimum of{' '}
          {channel.min_days}. This channel is neither flagged nor passed.
        </InlineAlert>
      )}

      <MetricRow>
        <MetricTile
          label="Average"
          value={formatPct(channel.average_pct)}
          tone={channel.insufficient ? 'default' : channel.above_threshold ? 'pink' : 'clean'}
          note={`over ${withData} day(s) with data · threshold ${channel.threshold_pct} %`}
        />
        <MetricTile
          label="Maximum day"
          value={formatPct(channel.max_pct)}
          tone="violet"
          note={dayOf(channel.max_at)}
        />
        <MetricTile
          label="Days above"
          value={channel.minutes_above}
          tone={channel.minutes_above > 0 ? 'violet' : 'default'}
          note={`of ${withData} day(s) with data`}
        />
        <MetricTile
          label="Days with data"
          value={`${withData} / ${expected}`}
          tone={channel.insufficient ? 'pink' : 'default'}
          note={`minimum ${channel.min_days ?? '—'} to judge`}
        />
        <MetricTile
          label="Provider"
          value={<span className="text-body">{channel.provider_name || '—'}</span>}
          note={renamed ? `CASCADA name: ${channel.cascada_channel_name}` : 'as CASCADA lists it'}
        />
        <MetricTile
          label="Window"
          value={<span className="text-body">{historicalLabel(channel)}</span>}
          note="UTC, complete days, today excluded"
        />
      </MetricRow>

      <div className="card">
        <div className="flex flex-wrap items-center justify-between gap-2 px-4 pb-1 pt-3.5">
          <h3 className="text-body font-semibold text-ink">Rebuffering ratio per day</h3>
          <span className="chip-neutral">historical</span>
        </div>
        <div className="px-1.5 pb-2">
          <CascadaChart
            origin={channel.origin}
            comparison={[]}
            showComparison={false}
            threshold={channel.threshold_pct}
            thresholdLabel={`threshold ${channel.threshold_pct} %`}
            yName="rebuffering ratio (%)"
            formatValue={tooltipPct}
            granularity="day"
          />
        </div>
        <p className="px-4 pb-3 text-micro text-ink-faint">
          Days above {channel.threshold_pct} % are drawn in pink; the rest in blue. A day CASCADA
          reported nothing for is a shaded gap labelled &ldquo;no data&rdquo;, not a zero —{' '}
          {missing} such day(s) in this window. Dates are UTC.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <a className="btn-ghost btn-sm" href={endpoints.cascadaHistoricalReportUrl(query, 'csv')}>
          <IconDownload size={13} />
          Download CSV
        </a>
        <a className="btn-ghost btn-sm" href={endpoints.cascadaHistoricalReportUrl(query, 'xlsx')}>
          <IconDownload size={13} />
          Download XLSX
        </a>
        <span className="flex-1" />
        <button
          type="button"
          className="btn-ghost btn-sm"
          disabled={!analysable}
          onClick={() => onAnalyse('realtime')}
        >
          <IconPulse size={13} />
          Realtime
        </button>
        <button
          type="button"
          className="btn-ghost btn-sm"
          disabled={!analysable}
          onClick={() => onAnalyse('aging')}
        >
          <IconAging size={13} />
          Aging
        </button>
      </div>

      <p className="text-micro text-ink-faint">
        Measured at <span className="font-mono">{utcLabel(channel.fetched_at)}</span> UTC
        {channel.cached && ' · served from the stored window'}. CASCADA historical data, one value
        per UTC day, queried with channel_country=ALL, so this is the channel&apos;s rebuffering
        across every country it runs in.
      </p>
    </>
  )
}
