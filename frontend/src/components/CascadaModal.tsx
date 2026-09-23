/**
 * The frame both CASCADA channel panels open in, and what they show while CASCADA answers.
 *
 * A week of per-minute data for two weeks is a large answer from a slow service, so the wait
 * is shown as a wait: a turning indicator, what is being fetched and for which channel, and
 * how long it has taken so far. A line of text on its own read as a page that had stalled.
 */

import { useEffect, type ReactNode } from 'react'
import { CardHeader, Spinner, useElapsedSeconds } from './ui'
import { IconClose } from './ui/icons'

export function CascadaModalFrame({
  label,
  title,
  subtitle,
  status,
  onClose,
  children,
}: {
  /** What the dialog is, for assistive technology. */
  label: string
  title: ReactNode
  subtitle: ReactNode
  /** The verdict chip, once there is one. */
  status?: ReactNode
  onClose: () => void
  children: ReactNode
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-ink/40 p-4 sm:p-8"
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div role="dialog" aria-modal="true" aria-label={label} className="card w-full max-w-5xl">
        <CardHeader
          title={title}
          subtitle={subtitle}
          actions={
            <>
              {status}
              <button
                type="button"
                className="btn-ghost btn-sm"
                onClick={onClose}
                aria-label="Close"
              >
                <IconClose size={14} />
                Close
              </button>
            </>
          }
        />
        <div className="space-y-4 px-5 pb-5">{children}</div>
      </div>
    </div>
  )
}

/**
 * The panel shown until CASCADA has answered: an indicator, what is on its way, and the
 * seconds waited, with the shape of the tiles and the chart held in place so the page does
 * not jump when the data lands.
 */
export function CascadaLoading({ what, channel }: { what: string; channel: string }) {
  const seconds = useElapsedSeconds()
  return (
    <div aria-live="polite" aria-busy="true" className="space-y-4">
      <div className="flex flex-col items-center gap-3 px-4 pb-2 pt-8 text-center">
        <Spinner size={34} label={`Fetching ${what} from CASCADA`} />
        <p className="text-body font-semibold text-ink">
          Fetching {what} for {channel} from CASCADA
        </p>
        <p className="max-w-md text-small text-ink-muted">
          This week and the week before it, minute by minute. The data appears here as soon as
          CASCADA answers.
        </p>
        <p className="font-mono text-micro text-ink-faint">{seconds} s</p>
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6" aria-hidden="true">
        {Array.from({ length: 6 }, (_, index) => (
          <div key={index} className="card card-pad">
            <div className="h-2.5 w-16 animate-pulse rounded bg-surface-line" />
            <div className="mt-3 h-6 w-20 animate-pulse rounded bg-surface-line" />
          </div>
        ))}
      </div>
      <div className="card h-72 animate-pulse bg-surface-sunken" aria-hidden="true" />
    </div>
  )
}
