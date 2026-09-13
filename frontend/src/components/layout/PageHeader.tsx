/**
 * The page header.
 *
 * Persistent context: what this screen is, what state it is in, and who is operating it.
 * Actions sit on the right so the eye travels title → state → action.
 */

import type { ReactNode } from 'react'
import { OPERATOR, initials } from '../../lib/constants'
import { cx } from '../ui'
import { IconArrowLeft } from '../ui/icons'

export function OperatorChip() {
  return (
    <div className="flex items-center gap-2.5 rounded-pill border border-surface-line bg-white py-1 pl-1 pr-3.5">
      <span className="flex h-7 w-7 items-center justify-center rounded-full bg-spectrum-soft text-[11px] font-semibold text-white">
        {initials(OPERATOR.name)}
      </span>
      <span className="leading-tight">
        <span className="block text-micro font-semibold text-ink">{OPERATOR.name}</span>
        <span className="block text-[11px] text-ink-muted">{OPERATOR.team}</span>
      </span>
    </div>
  )
}

export function PageHeader({
  title,
  subtitle,
  status,
  actions,
  onBack,
  backLabel,
}: {
  title: ReactNode
  subtitle?: ReactNode
  status?: ReactNode
  actions?: ReactNode
  onBack?: () => void
  backLabel?: string
}) {
  return (
    <header className="sticky top-0 z-20 border-b border-surface-line bg-white">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3.5 sm:px-6 sm:py-4">
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            className="btn-ghost btn-sm shrink-0"
            aria-label={backLabel ?? 'Go back'}
          >
            <IconArrowLeft size={15} />
            {backLabel ?? 'Back'}
          </button>
        )}
        <div className="min-w-0 flex-1 basis-52">
          <h1 className="truncate text-page text-ink">{title}</h1>
          {subtitle && <p className="mt-0.5 truncate text-small text-ink-muted">{subtitle}</p>}
        </div>
        {/* The action group wraps onto its own line rather than pushing the page sideways. */}
        <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto sm:justify-end sm:gap-3">
          {actions}
          {status}
          <OperatorChip />
        </div>
      </div>
    </header>
  )
}

export function PageBody({
  children,
  className,
}: {
  children: ReactNode
  className?: string
}) {
  return <div className={cx('space-y-4 px-4 py-4 sm:px-6 sm:py-5', className)}>{children}</div>
}
