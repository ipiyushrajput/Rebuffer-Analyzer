/** Application shell: header, tab bar, and the health strip. */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { endpoints } from './api/client'
import { AgingTab } from './tabs/Aging'
import { BulkTab } from './tabs/Bulk'
import { RealtimeTab } from './tabs/Realtime'
import { ReportsTab } from './tabs/Reports'
import { SettingsTab } from './tabs/Settings'

type TabId = 'realtime' | 'aging' | 'bulk' | 'reports' | 'settings'

const TABS: { id: TabId; label: string; description: string }[] = [
  { id: 'realtime', label: 'Realtime', description: 'Analyse a channel live' },
  { id: 'aging', label: 'Aging', description: 'Long-running server-side analysis' },
  { id: 'bulk', label: 'Bulk Analysis', description: 'Many channels from a file' },
  { id: 'reports', label: 'Reports', description: 'Every report generated' },
  { id: 'settings', label: 'Settings', description: 'Thresholds and limits' },
]

function HealthStrip() {
  const { data } = useQuery({
    queryKey: ['health'],
    queryFn: endpoints.health,
    refetchInterval: 30000,
  })
  if (!data) return null

  const degraded = (data.degraded as string[]) ?? []
  if (degraded.length === 0) return null

  const notes: Record<string, string> = {
    ffmpeg: 'ffmpeg is not installed, so the decode-error and quality detectors do not run.',
    ffprobe: 'ffprobe is not installed, so the decode-error and quality detectors do not run.',
    playwright: 'Playwright is not installed, so PDF export is unavailable; HTML export works.',
    database: 'The database does not answer, so jobs and reports are not persisted.',
  }

  return (
    <div className="border-b border-warn bg-amber-50 px-4 py-1.5 text-xs text-warn">
      {degraded.map((item) => notes[item] ?? `${item} is unavailable.`).join(' ')}
    </div>
  )
}

export default function App() {
  const [tab, setTab] = useState<TabId>('realtime')
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: endpoints.settings })
  const thresholds = (settings?.thresholds as Record<string, number>) ?? {}

  return (
    <div className="flex min-h-full flex-col">
      <header className="border-b border-[var(--rba-line)] bg-white">
        <HealthStrip />
        <div className="mx-auto flex max-w-[1800px] flex-wrap items-center gap-4 px-4 py-3">
          <img src="/tvplus-logo.png" alt="" width={32} height={32} className="rounded" />
          <div className="min-w-0">
            <h1 className="text-base font-bold tracking-tight">TV Plus Rebuffer Analyzer</h1>
            <p className="text-xs text-[var(--rba-muted)]">
              Stream quality — channel-wise root cause, evidence, owner and fix
            </p>
          </div>
          <nav className="ml-auto flex flex-wrap gap-1 rounded-lg bg-slate-100 p-1" aria-label="Sections">
            {TABS.map((item) => (
              <button
                key={item.id}
                type="button"
                title={item.description}
                aria-current={tab === item.id ? 'page' : undefined}
                className={`tab-button ${tab === item.id ? 'tab-button-active' : ''}`}
                onClick={() => setTab(item.id)}
              >
                {item.label}
              </button>
            ))}
          </nav>
        </div>
      </header>

      <main className="mx-auto w-full max-w-[1800px] flex-1 px-4 py-4">
        {/* Tabs are hidden with CSS, not unmounted, so a running Realtime session keeps its
            player and its socket while the user looks at another tab. */}
        <div className={tab === 'realtime' ? '' : 'panel-hidden'}>
          <RealtimeTab thresholds={thresholds} />
        </div>
        <div className={tab === 'aging' ? '' : 'panel-hidden'}>
          <AgingTab />
        </div>
        <div className={tab === 'bulk' ? '' : 'panel-hidden'}>
          <BulkTab />
        </div>
        <div className={tab === 'reports' ? '' : 'panel-hidden'}>
          <ReportsTab />
        </div>
        <div className={tab === 'settings' ? '' : 'panel-hidden'}>
          <SettingsTab />
        </div>
      </main>

      <footer className="border-t border-[var(--rba-line)] bg-white px-4 py-2 text-xs text-[var(--rba-muted)]">
        Player metrics are measured from the analyzer host and reflect its network path.
        Timestamps are local; hover any timestamp for UTC.
      </footer>
    </div>
  )
}
