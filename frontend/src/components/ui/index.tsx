/**
 * Interface primitives.
 *
 * Panels are grouped by task, not by decoration. Every component here carries the same
 * spacing intervals, dividers and numeric alignment so dense screens keep one rhythm.
 */

import { useEffect, useState, type ReactNode } from 'react'
import { copyText } from '../../lib/clipboard'
import { SEVERITY_STYLE, type Severity } from '../../lib/constants'
import { IconArrowLeft, IconArrowRight, IconCheck } from './icons'

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ')
}

// --- surfaces ---------------------------------------------------------------

export function Card({
  children,
  className,
  as: Tag = 'section',
}: {
  children: ReactNode
  className?: string
  as?: 'section' | 'div' | 'article'
}) {
  return <Tag className={cx('card', className)}>{children}</Tag>
}

export function CardHeader({
  title,
  subtitle,
  actions,
  className,
}: {
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  className?: string
}) {
  return (
    <header className={cx('card-header', className)}>
      <div className="min-w-0">
        <h3 className="card-title">{title}</h3>
        {subtitle && <p className="card-subtitle mt-0.5">{subtitle}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </header>
  )
}

export function EmptyState({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="px-5 py-10 text-center">
      <p className="text-body font-semibold text-ink-soft">{title}</p>
      {detail && <p className="mx-auto mt-1 max-w-md text-small text-ink-muted">{detail}</p>}
    </div>
  )
}

// --- metrics ----------------------------------------------------------------

export type MetricTone = 'default' | 'blue' | 'violet' | 'pink' | 'clean'

const METRIC_TONE: Record<MetricTone, string> = {
  default: 'text-ink',
  blue: 'text-brand-600',
  violet: 'text-violet-500',
  pink: 'text-pink-600',
  clean: 'text-clean-600',
}

export function MetricTile({
  label,
  value,
  note,
  tone = 'default',
  suffix,
}: {
  label: string
  value: ReactNode
  note?: ReactNode
  tone?: MetricTone
  suffix?: ReactNode
}) {
  /* A measurement that has not been taken reads as absent, not as a value of its own. */
  const absent = value === '—' || value === null || value === undefined
  return (
    <div className="card card-pad">
      <p className="label">{label}</p>
      <p className={cx('metric mt-1.5', absent ? 'text-ink-faint' : METRIC_TONE[tone])}>
        {absent ? <span className="font-sans text-section font-normal">—</span> : value}
        {!absent && suffix && (
          <span className="ml-1 text-micro font-normal text-ink-faint">{suffix}</span>
        )}
      </p>
      {note && <p className="mt-1 text-micro text-ink-muted">{note}</p>}
    </div>
  )
}

export function MetricRow({ children }: { children: ReactNode }) {
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">{children}</div>
  )
}

// --- chips ------------------------------------------------------------------

export function SeverityChip({ severity }: { severity: Severity }) {
  const style = SEVERITY_STYLE[severity]
  return <span className={cx('chip', style.chip)}>{severity}</span>
}

export function LiveChip({ label, live }: { label: string; live: boolean }) {
  return (
    <span className={live ? 'chip-clean' : 'chip-neutral'}>
      <span
        className={cx(
          'inline-block h-1.5 w-1.5 rounded-full',
          live ? 'animate-pulse bg-clean-500' : 'bg-ink-faint',
        )}
      />
      {label}
    </span>
  )
}

// --- progress ---------------------------------------------------------------

export function ProgressBar({
  value,
  tone = 'blue',
  label,
}: {
  value: number
  tone?: 'blue' | 'spectrum'
  label?: string
}) {
  const percent = Math.max(0, Math.min(100, value * 100))
  return (
    <div>
      <div
        className="h-1.5 w-full overflow-hidden rounded-pill bg-surface-line"
        role="progressbar"
        aria-valuenow={Math.round(percent)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label}
      >
        <div
          className={cx(
            'h-full rounded-pill transition-[width] duration-500',
            tone === 'spectrum' ? 'bg-spectrum' : 'bg-brand-600',
          )}
          style={{ width: `${percent}%` }}
        />
      </div>
      {label && <p className="mt-1 text-micro text-ink-muted">{label}</p>}
    </div>
  )
}

// --- controls ---------------------------------------------------------------

export function SegmentedControl<T extends string | number>({
  options,
  value,
  onChange,
  ariaLabel,
}: {
  options: { value: T; label: string }[]
  value: T
  onChange: (value: T) => void
  ariaLabel?: string
}) {
  return (
    <div className="flex flex-wrap gap-1.5" role="group" aria-label={ariaLabel}>
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={String(option.value)}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(option.value)}
            className={cx(
              'rounded-tile border px-3 py-1.5 text-small font-semibold transition-colors duration-150',
              active
                ? 'border-brand-600 bg-brand-50 text-brand-600'
                : 'border-surface-line bg-white text-ink-muted hover:border-surface-lineStrong hover:text-ink',
            )}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: { id: T; label: ReactNode }[]
  value: T
  onChange: (id: T) => void
}) {
  return (
    <div className="flex flex-wrap gap-1 border-b border-surface-line" role="tablist">
      {tabs.map((tab) => {
        const active = tab.id === value
        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(tab.id)}
            className={cx(
              '-mb-px border-b-2 px-3.5 py-2.5 text-body font-semibold transition-colors duration-150',
              active
                ? 'border-brand-600 text-brand-600'
                : 'border-transparent text-ink-muted hover:text-ink',
            )}
          >
            {tab.label}
          </button>
        )
      })}
    </div>
  )
}

export function Field({
  label,
  htmlFor,
  hint,
  children,
  className,
}: {
  label: string
  htmlFor?: string
  hint?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <div className={className}>
      <label className="field-label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {hint && <p className="mt-1 text-micro text-ink-muted">{hint}</p>}
    </div>
  )
}

export function Stepper({
  steps,
  current,
}: {
  steps: string[]
  current: number
}) {
  return (
    <ol className="flex flex-wrap items-center gap-2">
      {steps.map((step, index) => {
        const done = index < current
        const active = index === current
        return (
          <li key={step} className="flex flex-1 min-w-[150px] items-center gap-2">
            <span
              className={cx(
                'flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-micro font-semibold',
                done && 'bg-clean-500 text-white',
                active && 'bg-brand-600 text-white',
                !done && !active && 'border border-surface-lineStrong bg-white text-ink-faint',
              )}
            >
              {done ? <IconCheck size={13} /> : index + 1}
            </span>
            <span
              className={cx(
                'text-small',
                active ? 'font-semibold text-ink' : 'text-ink-muted',
              )}
            >
              {step}
            </span>
            {index < steps.length - 1 && (
              <span className="ml-1 hidden h-px flex-1 bg-surface-line sm:block" />
            )}
          </li>
        )
      })}
    </ol>
  )
}

// --- messages ---------------------------------------------------------------

/** A stated condition in the flow of the page: what happened, in its own colour. */
export function InlineAlert({
  tone,
  children,
}: {
  tone: 'error' | 'warn' | 'clean' | 'info'
  children: ReactNode
}) {
  const style = {
    error: 'border-pink-200 bg-pink-50 text-pink-600',
    warn: 'border-violet-200 bg-violet-50 text-violet-500',
    clean: 'border-clean-100 bg-clean-50 text-clean-600',
    info: 'border-brand-200 bg-brand-50 text-brand-600',
  }[tone]
  return (
    <p className={cx('rounded-tile border px-3.5 py-2.5 text-small', style)}>{children}</p>
  )
}

/**
 * Copy-to-clipboard button that confirms in place rather than through a toast, and says so
 * when the copy did not happen rather than looking like it did.
 */
export function CopyButton({
  text,
  label = 'Copy',
  variant = 'ghost',
  className,
}: {
  text: string | (() => string)
  label?: string
  variant?: 'ghost' | 'primary'
  className?: string
}) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')

  useEffect(() => {
    if (state === 'idle') return undefined
    const timer = window.setTimeout(() => setState('idle'), 1800)
    return () => window.clearTimeout(timer)
  }, [state])

  return (
    <button
      type="button"
      className={cx(
        variant === 'primary' ? 'btn-primary' : 'btn-ghost',
        'btn-sm',
        state === 'failed' && 'text-pink-600',
        className,
      )}
      onClick={async () => {
        const copied = await copyText(typeof text === 'function' ? text() : text)
        setState(copied ? 'copied' : 'failed')
      }}
    >
      {state === 'copied' ? (
        <>
          <IconCheck size={13} />
          Copied
        </>
      ) : state === 'failed' ? (
        'Copy blocked — select and copy'
      ) : (
        label
      )}
    </button>
  )
}

/**
 * Page controls for a long table.
 *
 * Rendered above and below the rows: a hundred-row page is taller than the viewport, and an
 * operator who has read to the bottom should not have to scroll back up to move on — nor
 * scroll down to move on after reading the top. Both copies drive the same state, so the
 * pair always agrees.
 */
export function Pagination({
  label,
  hasPrevious,
  hasNext,
  busy,
  onPrevious,
  onNext,
  className,
}: {
  label: string
  hasPrevious: boolean
  hasNext: boolean
  busy?: boolean
  onPrevious: () => void
  onNext: () => void
  className?: string
}) {
  return (
    <div className={cx('flex flex-wrap items-center justify-between gap-3', className)}>
      <span className="font-mono text-micro text-ink-muted">{label}</span>
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="btn-ghost btn-sm"
          disabled={!hasPrevious || busy}
          onClick={onPrevious}
        >
          <IconArrowLeft size={14} />
          Previous
        </button>
        <button
          type="button"
          className="btn-ghost btn-sm"
          disabled={!hasNext || busy}
          onClick={onNext}
        >
          Next
          <IconArrowRight size={14} />
        </button>
      </div>
    </div>
  )
}
