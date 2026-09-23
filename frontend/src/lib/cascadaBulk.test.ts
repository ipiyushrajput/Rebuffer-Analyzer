import { describe, expect, it } from 'vitest'
import { BULK_COLUMNS, fileName, partition, toCsv } from './cascadaBulk'
import type { CascadaBulkRow } from './cascadaBulk'

function row(patch: Partial<CascadaBulkRow> & { service_id: string }): CascadaBulkRow {
  return {
    service_id: patch.service_id,
    channel_name: patch.channel_name ?? 'Channel',
    country: patch.country ?? 'IN',
    playback_url: patch.playback_url ?? 'https://cdn.example/live/index.m3u8',
    average_pct: patch.average_pct ?? 1.0,
    max_pct: patch.max_pct ?? 2.0,
    max_at: null,
    minutes_above: 10,
    minutes_counted: 100,
    minutes_missing: 0,
    percent_time_above: 10,
    previous_week_average_pct: 0.5,
    week_over_week_delta_pct: 0.5,
    threshold_pct: 0.25,
    above_threshold: patch.above_threshold ?? true,
    truncated: false,
    fetched_at: '2026-09-17T05:13:00+00:00',
    cached: false,
    source: 'realtime',
    granularity: 'minute',
  }
}

describe('handing the rebuffering channels to Bulk analysis', () => {
  it('keeps only channels above the threshold, worst first', () => {
    const { analysable } = partition([
      row({ service_id: 'IN2', channel_name: 'Second', average_pct: 0.9 }),
      row({ service_id: 'IN3', channel_name: 'Clean', average_pct: 0.01, above_threshold: false }),
      row({ service_id: 'IN1', channel_name: 'Worst', average_pct: 2.4 }),
    ])

    expect(analysable.map((r) => r.channel_name)).toEqual(['Worst', 'Second'])
  })

  it('counts out a rebuffering channel the catalogue gives no playback URL for', () => {
    // It is still rebuffering, so it belongs in the report; there is just nothing to fetch.
    const { analysable, withoutUrl } = partition([
      row({ service_id: 'IN1', channel_name: 'Playable' }),
      row({ service_id: 'IN2', channel_name: 'No URL', playback_url: '' }),
      row({ service_id: 'IN3', channel_name: 'Blank URL', playback_url: '   ' }),
    ])

    expect(analysable.map((r) => r.service_id)).toEqual(['IN1'])
    expect(withoutUrl.map((r) => r.service_id)).toEqual(['IN2', 'IN3'])
  })

  it('writes the columns the bulk parser already maps', () => {
    const csv = toCsv([row({ service_id: 'IN1', channel_name: 'Worst' })])
    const [header, first] = csv.trim().split('\n')

    expect(header).toBe(BULK_COLUMNS.join(','))
    expect(first).toBe('Worst,https://cdn.example/live/index.m3u8,IN1,IN')
    expect(csv.endsWith('\n')).toBe(true)
  })

  it('quotes a channel name carrying a comma or a quote rather than splitting the row', () => {
    const csv = toCsv([
      row({ service_id: 'IN1', channel_name: 'News, Sport & "More"' }),
    ])
    const rows = csv.trim().split('\n')

    expect(rows).toHaveLength(2)
    expect(rows[1]).toBe(
      '"News, Sport & ""More""",https://cdn.example/live/index.m3u8,IN1,IN',
    )
  })

  it('keeps every query parameter of a playback URL verbatim', () => {
    const url =
      'https://cdn.example/v1/master/index.m3u8?ads.device_did=%7BPSID%7D&ads.service_id=IN1'
    const csv = toCsv([row({ service_id: 'IN1', playback_url: url })])

    expect(csv).toContain(url)
  })

  it('names the file after the country and the day', () => {
    expect(fileName('in', new Date('2026-09-17T05:13:00Z'))).toBe(
      'rebuffering_channels_IN_20260917.csv',
    )
  })
})
