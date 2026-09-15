/**
 * The page header.
 *
 * Persistent context: what this screen is and what state it is in. Actions sit on the right
 * so the eye travels title → state → action.
 */

import type { ReactNode } from 'react'
import { cx } from '../ui'
import { IconArrowLeft } from '../ui/icons'

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
