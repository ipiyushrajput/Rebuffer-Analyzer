/** Ladder audit: what the manifest declares against what the bitstream carries. */

import { kbps } from '../lib/format'
import { EmptyState, cx } from './ui'

export interface LadderRow {
  layer: string
  variant: string
  declared: {
    bandwidth: number | null
    average_bandwidth: number | null
    resolution: string | null
    frame_rate: number | null
    codecs: string | null
    video_range: string | null
  }
  /**
   * How the rung is packaged. `null` for a single-rendition channel, which has no master to
   * read a layout off. A demuxed rung's segments carry video only, by design — the reason
   * AUD-003 no longer fires on them.
   */
  layout: {
    layout: 'muxed' | 'demuxed'
    audio_group: string | null
    audio_variants: string[]
  } | null
  measured: {
    resolution: string | null
    profile: string | null
    level: string | null
    max_num_ref_frames: number | null
    frame_rate: number | null
    scan_type: string | null
    peak_kbps: number | null
    audio_codec: string | null
  }
}

function Cell({
  declared,
  measured,
}: {
  declared: string | number | null | undefined
  measured: string | number | null | undefined
}) {
  const shown = (value: string | number | null | undefined) =>
    value === null || value === undefined || value === '' ? '—' : String(value)
  const differs = declared != null && measured != null && String(declared) !== String(measured)
  return (
    <td className={cx(differs && 'bg-pink-50')}>
      <div className={cx('font-mono text-micro', differs ? 'text-pink-600' : 'text-ink')}>
        {shown(declared)}
      </div>
      {measured !== undefined && (
        <div className={cx('font-mono text-[11px]', differs ? 'text-pink-600' : 'text-ink-faint')}>
          {shown(measured)}
        </div>
      )}
    </td>
  )
}

export function LadderTable({ rows }: { rows: LadderRow[] }) {
  if (rows.length === 0) {
    return (
      <EmptyState
        title="No rung has been sampled yet"
        detail="The ladder table fills in once a segment has been fetched and demuxed on each rung."
      />
    )
  }

  const refFrames = rows
    .map((row) => row.measured.max_num_ref_frames)
    .filter((value): value is number => value != null)
  const refFramesDiffer = new Set(refFrames).size > 1

  return (
    <div>
      <div className="overflow-x-auto">
        <table className="table table-hover">
          <thead>
            <tr>
              <th>Rung</th>
              <th>Audio</th>
              <th>Bandwidth</th>
              <th>Resolution</th>
              <th>Frame rate</th>
              <th>Codecs</th>
              <th>Profile / level</th>
              <th>Ref frames</th>
              <th>Scan</th>
              <th>Peak measured</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.layer}:${row.variant}`}>
                <td className="font-semibold text-ink">{row.variant}</td>
                <td className="text-micro text-ink">
                  {row.layout ? row.layout.layout : '—'}
                  {row.layout && row.layout.audio_variants.length > 0 && (
                    <div className="font-mono text-[11px] text-ink-faint">
                      {row.layout.audio_variants.join(', ')}
                    </div>
                  )}
                </td>
                <Cell declared={kbps(row.declared.bandwidth)} measured={undefined} />
                <Cell declared={row.declared.resolution} measured={row.measured.resolution} />
                <Cell
                  declared={row.declared.frame_rate}
                  measured={row.measured.frame_rate?.toFixed(3)}
                />
                <Cell declared={row.declared.codecs} measured={row.measured.audio_codec} />
                <Cell
                  declared={`${row.measured.profile ?? '—'} ${row.measured.level ?? ''}`.trim()}
                  measured={undefined}
                />
                <td
                  className={cx(
                    'font-mono text-micro',
                    refFramesDiffer ? 'bg-pink-50 text-pink-600' : 'text-ink',
                  )}
                >
                  {row.measured.max_num_ref_frames ?? '—'}
                </td>
                <Cell declared={row.measured.scan_type} measured={undefined} />
                <Cell
                  declared={
                    row.measured.peak_kbps ? `${Math.round(row.measured.peak_kbps)} kbit/s` : '—'
                  }
                  measured={undefined}
                />
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {refFramesDiffer && (
        <p className="border-t border-pink-200 bg-pink-50 px-5 py-2.5 text-small text-pink-600">
          The rungs declare different reference frame counts. Every ABR switch between them
          reallocates the decoded picture buffer.
        </p>
      )}
      <p className="px-5 py-2.5 text-micro text-ink-muted">
        The upper value in each cell is the manifest declaration; the lower value is what the
        bitstream carries. A rung marked <span className="font-mono">demuxed</span> takes its
        audio from the rendition named beneath it, so its own segments carry video only.
      </p>
    </div>
  )
}
