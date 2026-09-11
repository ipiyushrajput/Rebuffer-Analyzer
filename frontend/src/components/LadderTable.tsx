/** Ladder audit: what the manifest declares against what the bitstream carries. */

import { kbps } from '../lib/format'

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
  const differs =
    declared != null && measured != null && String(declared) !== String(measured)
  return (
    <td className={`px-3 py-2 ${differs ? 'bg-red-50 text-critical' : ''}`}>
      <div className="font-mono text-[12px]">{shown(declared)}</div>
      {measured !== undefined && (
        <div className="font-mono text-[11px] text-[var(--rba-muted)]">{shown(measured)}</div>
      )}
    </td>
  )
}

export function LadderTable({ rows }: { rows: LadderRow[] }) {
  if (rows.length === 0) {
    return (
      <p className="px-4 py-6 text-sm text-[var(--rba-muted)]">
        The ladder table fills in once a segment has been sampled on each rung.
      </p>
    )
  }

  const refFrames = rows
    .map((row) => row.measured.max_num_ref_frames)
    .filter((value): value is number => value != null)
  const refFramesDiffer = new Set(refFrames).size > 1

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="table-head">
            <th className="px-3 py-2">Rung</th>
            <th className="px-3 py-2">Bandwidth</th>
            <th className="px-3 py-2">Resolution</th>
            <th className="px-3 py-2">Frame rate</th>
            <th className="px-3 py-2">Codecs</th>
            <th className="px-3 py-2">Profile / level</th>
            <th className="px-3 py-2">Ref frames</th>
            <th className="px-3 py-2">Scan</th>
            <th className="px-3 py-2">Peak measured</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--rba-line)]">
          {rows.map((row) => (
            <tr key={`${row.layer}:${row.variant}`}>
              <td className="px-3 py-2 font-semibold">{row.variant}</td>
              <Cell declared={kbps(row.declared.bandwidth)} measured={undefined} />
              <Cell declared={row.declared.resolution} measured={row.measured.resolution} />
              <Cell declared={row.declared.frame_rate} measured={row.measured.frame_rate?.toFixed(3)} />
              <Cell declared={row.declared.codecs} measured={row.measured.audio_codec} />
              <Cell
                declared={`${row.measured.profile ?? '—'} ${row.measured.level ?? ''}`.trim()}
                measured={undefined}
              />
              <td
                className={`px-3 py-2 font-mono text-[12px] ${
                  refFramesDiffer ? 'bg-red-50 text-critical' : ''
                }`}
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
      {refFramesDiffer && (
        <p className="border-t border-critical bg-red-50 px-3 py-2 text-xs text-critical">
          The rungs declare different reference frame counts. Every ABR switch between them
          reallocates the decoded picture buffer.
        </p>
      )}
      <p className="px-3 py-2 text-xs text-[var(--rba-muted)]">
        The upper value in each cell is the manifest declaration; the lower value is what the
        bitstream carries.
      </p>
    </div>
  )
}
