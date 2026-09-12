/**
 * Manifest viewer.
 *
 * Pause freezes the display only — the analysis keeps running behind it, so a paused view
 * never costs a measurement. Long manifests render virtualised so a 10 000-line playlist
 * scrolls without blocking.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { localTime, utcTitle } from '../lib/format'

const LINE_HEIGHT = 18
const OVERSCAN = 20

interface Props {
  title: string
  raw: string
  url?: string
  at?: string
  highlight?: (line: string) => boolean
}

export function ManifestViewer({ title, raw, url, at, highlight }: Props) {
  const [paused, setPaused] = useState(false)
  const [frozen, setFrozen] = useState(raw)
  const [scrollTop, setScrollTop] = useState(0)
  const [height, setHeight] = useState(320)
  const containerRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!paused) setFrozen(raw)
  }, [raw, paused])

  const lines = useMemo(() => frozen.split('\n'), [frozen])
  const first = Math.max(0, Math.floor(scrollTop / LINE_HEIGHT) - OVERSCAN)
  const visible = Math.ceil(height / LINE_HEIGHT) + OVERSCAN * 2
  const slice = lines.slice(first, first + visible)

  return (
    <div className="card overflow-hidden">
      <div className="flex flex-wrap items-start justify-between gap-3 px-4 pb-2.5 pt-3.5">
        <div className="min-w-0">
          <h3 className="card-title">{title}</h3>
          {url && (
            <p className="truncate font-mono text-[11px] text-ink-faint" title={url}>
              {url}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {at && (
            <span className="font-mono text-micro text-ink-muted" title={utcTitle(at)}>
              {localTime(at)}
            </span>
          )}
          <span className="text-micro text-ink-faint">{lines.length} lines</span>
          <button
            type="button"
            className="btn-ghost btn-sm"
            onClick={() => setPaused((value) => !value)}
            title="Pausing freezes this view only; the analysis continues"
          >
            {paused ? 'Resume view' : 'Pause view'}
          </button>
        </div>
      </div>
      <div
        ref={containerRef}
        className="manifest-pane border-t border-surface-line bg-surface-raised"
        style={{ height }}
        onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
        onMouseUp={() => setHeight(containerRef.current?.clientHeight ?? height)}
      >
        <div style={{ height: lines.length * LINE_HEIGHT, position: 'relative' }}>
          <div style={{ position: 'absolute', top: first * LINE_HEIGHT, left: 0, right: 0 }}>
            {slice.map((line, index) => {
              const number = first + index + 1
              const marked = highlight?.(line) ?? false
              return (
                <div
                  key={number}
                  className={`flex gap-3 px-3 font-mono text-micro ${
                    marked ? 'border-l-[3px] border-violet-500 bg-violet-50' : 'border-l-[3px] border-transparent'
                  }`}
                  style={{ height: LINE_HEIGHT, lineHeight: `${LINE_HEIGHT}px` }}
                >
                  <span className="w-10 shrink-0 select-none text-right text-ink-faint">{number}</span>
                  <span
                    className={
                      line.startsWith('#EXT-X-')
                        ? 'text-brand-600'
                        : line.startsWith('#')
                          ? 'text-ink-faint'
                          : 'text-ink-soft'
                    }
                  >
                    {line || ' '}
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      </div>
      {paused && (
        <p className="border-t border-violet-200 bg-violet-50 px-4 py-2 text-micro text-violet-500">
          This view is frozen. The analysis continues collecting.
        </p>
      )}
    </div>
  )
}

export function ManifestDiff({ diff }: { diff: string[] }) {
  if (diff.length === 0) {
    return (
      <p className="px-5 pb-4 text-small text-ink-muted">
        This snapshot matches the one before it.
      </p>
    )
  }
  return (
    <pre className="manifest-pane mx-5 mb-5 max-h-72 rounded-tile border border-surface-line bg-surface-raised px-3 py-2 font-mono text-micro">
      {diff.map((line, index) => (
        <div
          key={index}
          className={
            line.startsWith('+') && !line.startsWith('+++')
              ? 'bg-clean-50 text-clean-600'
              : line.startsWith('-') && !line.startsWith('---')
                ? 'bg-pink-50 text-pink-600'
                : line.startsWith('@@')
                  ? 'text-brand-600'
                  : 'text-ink-soft'
          }
        >
          {line}
        </div>
      ))}
    </pre>
  )
}
