/**
 * The navigation rail.
 *
 * A stable dark anchor: it never scrolls, never changes width on its own, and always shows
 * where the operator is. Collapsing keeps the icons and the health state, so orientation
 * survives even at 72 px.
 */

import { useEffect, useState } from 'react'
import { cx } from '../ui'
import {
  BrandMark,
  IconAging,
  IconBulk,
  IconCollapse,
  IconExpand,
  IconRealtime,
  IconReports,
  IconSettings,
} from '../ui/icons'

export type TabId = 'realtime' | 'aging' | 'bulk' | 'reports' | 'settings'

const NAV: { id: TabId; label: string; Icon: (p: { size?: number }) => JSX.Element }[] = [
  { id: 'realtime', label: 'Realtime', Icon: IconRealtime },
  { id: 'aging', label: 'Aging', Icon: IconAging },
  { id: 'bulk', label: 'Bulk analysis', Icon: IconBulk },
  { id: 'reports', label: 'Reports', Icon: IconReports },
  { id: 'settings', label: 'Settings', Icon: IconSettings },
]

const STORAGE_KEY = 'rba.rail.collapsed'

/** Below this width the rail always shows icons only, whatever the stored preference is. */
const NARROW = '(max-width: 1023px)'

export function useRailCollapsed(): [boolean, (value: boolean) => void] {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return window.localStorage.getItem(STORAGE_KEY) === '1'
    } catch {
      return false
    }
  })
  const [narrow, setNarrow] = useState(() => window.matchMedia(NARROW).matches)

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, collapsed ? '1' : '0')
    } catch {
      /* a browser that refuses storage still renders; the choice just is not remembered */
    }
  }, [collapsed])

  useEffect(() => {
    const query = window.matchMedia(NARROW)
    const onChange = (event: MediaQueryListEvent) => setNarrow(event.matches)
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [])

  return [collapsed || narrow, setCollapsed]
}

export function Sidebar({
  active,
  onSelect,
  collapsed,
  onToggle,
  health,
}: {
  active: TabId
  onSelect: (id: TabId) => void
  collapsed: boolean
  onToggle: () => void
  health: { ok: boolean; label: string; detail: string }
}) {
  return (
    <nav
      aria-label="Sections"
      className={cx(
        'fixed inset-y-0 left-0 z-30 flex flex-col bg-rail text-rail-text transition-[width] duration-150',
        collapsed ? 'w-[72px]' : 'w-[232px]',
      )}
    >
      <div
        className={cx(
          'flex items-center gap-3 px-4 py-5',
          collapsed && 'justify-center px-0',
        )}
      >
        <BrandMark size={collapsed ? 32 : 34} />
        {!collapsed && (
          <div className="min-w-0">
            <p className="truncate text-[15px] font-semibold leading-tight tracking-tight">
              TV PLUS
            </p>
            <p className="truncate text-[10px] font-semibold uppercase tracking-[0.14em] text-rail-muted">
              Rebuffer Analyzer
            </p>
          </div>
        )}
      </div>

      <ul className="mt-2 flex-1 space-y-0.5 px-2">
        {NAV.map(({ id, label, Icon }) => {
          const isActive = id === active
          return (
            <li key={id}>
              <button
                type="button"
                onClick={() => onSelect(id)}
                aria-current={isActive ? 'page' : undefined}
                title={collapsed ? label : undefined}
                className={cx(
                  'relative flex w-full items-center gap-3 rounded-tile py-2.5 text-small font-semibold transition-colors duration-150',
                  collapsed ? 'justify-center px-0' : 'px-3',
                  isActive
                    ? 'bg-rail-active text-white'
                    : 'text-rail-muted hover:bg-rail-hover hover:text-rail-text',
                )}
              >
                {isActive && (
                  <span className="absolute inset-y-1 left-0 w-[3px] rounded-pill bg-pink-500" />
                )}
                <Icon size={18} />
                {!collapsed && <span className="truncate">{label}</span>}
              </button>
            </li>
          )
        })}
      </ul>

      <div className="px-2 pb-2">
        <button
          type="button"
          onClick={onToggle}
          aria-label={collapsed ? 'Expand the navigation rail' : 'Collapse the navigation rail'}
          className={cx(
            'flex w-full items-center gap-3 rounded-tile py-2 text-small font-semibold text-rail-muted transition-colors duration-150 hover:bg-rail-hover hover:text-rail-text',
            collapsed ? 'justify-center px-0' : 'px-3',
          )}
        >
          {collapsed ? <IconExpand size={18} /> : <IconCollapse size={18} />}
          {!collapsed && <span>Collapse</span>}
        </button>
      </div>

      <div className="border-t border-rail-border px-4 py-4">
        <span
          className={cx(
            'inline-flex items-center gap-1.5 rounded-pill px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.08em]',
            health.ok ? 'bg-clean-50 text-clean-600' : 'bg-pink-50 text-pink-600',
            collapsed && 'px-1.5',
          )}
          title={health.detail}
        >
          <span
            className={cx(
              'inline-block h-1.5 w-1.5 rounded-full',
              health.ok ? 'bg-clean-500' : 'bg-pink-500',
            )}
          />
          {!collapsed && health.label}
        </span>
        {!collapsed && (
          <p className="mt-2 line-clamp-3 text-[11px] leading-snug text-rail-muted" title={health.detail}>
            {health.detail}
          </p>
        )}
      </div>
    </nav>
  )
}
