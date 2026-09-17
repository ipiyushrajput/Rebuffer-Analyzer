/**
 * Handing the rebuffering channels to Bulk analysis.
 *
 * Bulk already parses a file of channels, validates every row and runs the batch; nothing of
 * that is rebuilt here. The CASCADA tab writes the channels it found into the columns that
 * parser already accepts, wraps them in a File, and the Bulk tab receives it exactly as if an
 * operator had chosen it — so the review step, the row errors and the consolidated report all
 * behave as they always have.
 *
 * A channel the catalogue lists with no playback URL cannot be analysed. It stays in the
 * country report, because it is still rebuffering, and it is counted out of the hand-off
 * rather than quietly dropped.
 */

import type { CascadaChannelRow } from '../api/client'

/** The canonical columns `app/bulk/parsers.py` maps, in the order the template writes them. */
export const BULK_COLUMNS = ['channel_name', 'playback_url', 'channel_id', 'country'] as const

/** A scan row carries the catalogue's playback URL alongside the measurement. */
export interface CascadaBulkRow extends CascadaChannelRow {
  playback_url: string
}

export interface BulkHandoff {
  /** The rows that will be analysed: above threshold and carrying a playback URL. */
  analysable: CascadaBulkRow[]
  /** Above threshold, but the catalogue lists no playback URL, so there is nothing to fetch. */
  withoutUrl: CascadaBulkRow[]
}

/** A CSV field, quoted only when it has to be, with embedded quotes doubled. */
function cell(value: string): string {
  return /[",\n\r]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value
}

/**
 * Split the rebuffering channels into the ones Bulk can run and the ones it cannot.
 *
 * Worst first, so a batch that is cut short has analysed the worst channels.
 */
export function partition(rows: CascadaBulkRow[]): BulkHandoff {
  const above = rows
    .filter((row) => row.above_threshold)
    .sort((a, b) => (b.average_pct ?? 0) - (a.average_pct ?? 0))
  return {
    analysable: above.filter((row) => row.playback_url.trim().length > 0),
    withoutUrl: above.filter((row) => row.playback_url.trim().length === 0),
  }
}

/** The channels as a Bulk input file. */
export function toCsv(rows: CascadaBulkRow[]): string {
  const lines = [BULK_COLUMNS.join(',')]
  for (const row of rows) {
    lines.push(
      [
        cell(row.channel_name),
        cell(row.playback_url),
        cell(row.service_id),
        cell(row.country),
      ].join(','),
    )
  }
  return `${lines.join('\n')}\n`
}

export function fileName(country: string, now = new Date()): string {
  const stamp = now.toISOString().slice(0, 10).replace(/-/g, '')
  return `rebuffering_channels_${country.toUpperCase()}_${stamp}.csv`
}

/** The file the Bulk tab receives, built in the browser from what the scan measured. */
export function toFile(rows: CascadaBulkRow[], country: string, now = new Date()): File {
  return new File([toCsv(rows)], fileName(country, now), { type: 'text/csv' })
}
