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
    <div className="card">
      <div className="card-header">
        <div className="min-w-0">
          <h3 className="card-title">{title}</h3>
          {url && (
            <p className="mono truncate text-[11px] text-[var(--rba-muted)]" title={url}>
              {url}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {at && (
            <span className="text-xs text-[var(--rba-muted)]" title={utcTitle(at)}>
              {localTime(at)}
            </span>
          )}
          <span className="text-xs text-[var(--rba-muted)]">{lines.length} lines</span>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => setPaused((value) => !value)}
            title="Pausing freezes this view only; the analysis continues"
          >
            {paused ? 'Resume view' : 'Pause view'}
          </button>
        </div>
      </div>
      <div
        ref={containerRef}
        className="manifest-pane"
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
                  className={`mono flex gap-3 px-3 ${marked ? 'bg-amber-50' : ''}`}
                  style={{ height: LINE_HEIGHT, lineHeight: `${LINE_HEIGHT}px` }}
                >
                  <span className="w-12 shrink-0 select-none text-right text-slate-400">{number}</span>
                  <span
                    className={
                      line.startsWith('#EXT-X-')
                        ? 'text-brand-700'
                        : line.startsWith('#')
                          ? 'text-[var(--rba-muted)]'
                          : ''
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
        <p className="border-t border-warn bg-amber-50 px-3 py-1.5 text-xs text-warn">
          This view is frozen. The analysis continues collecting.
        </p>
      )}
    </div>
  )
}

export function ManifestDiff({ diff }: { diff: string[] }) {
  if (diff.length === 0) {
    return <p className="px-3 py-2 text-sm text-[var(--rba-muted)]">This snapshot matches the one before it.</p>
  }
  return (
    <pre className="manifest-pane mono max-h-72 px-3 py-2">
      {diff.map((line, index) => (
        <div
          key={index}
          className={
            line.startsWith('+') && !line.startsWith('+++')
              ? 'bg-green-50 text-pass'
              : line.startsWith('-') && !line.startsWith('---')
                ? 'bg-red-50 text-critical'
                : line.startsWith('@@')
                  ? 'text-brand-700'
                  : ''
          }
        >
          {line}
        </div>
      ))}
    </pre>
  )
}
