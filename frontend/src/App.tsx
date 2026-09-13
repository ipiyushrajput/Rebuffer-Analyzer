/**
 * Application shell.
 *
 * The rail and header are persistent; only the body changes. Tabs are hidden with CSS
 * rather than unmounted, so a running Realtime session keeps its player, its socket and
 * every sample it has collected while the operator looks at another screen.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { endpoints } from './api/client'
import { Sidebar, useRailCollapsed, type TabId } from './components/layout/Sidebar'
import { AgingTab } from './tabs/Aging'
import { BulkTab } from './tabs/Bulk'
import { RealtimeTab } from './tabs/Realtime'
import { ReportsTab } from './tabs/Reports'
import { SettingsTab } from './tabs/Settings'

const DEGRADED_NOTE: Record<string, string> = {
  ffmpeg: 'ffmpeg is absent, so the decode-error and quality detectors do not run.',
  ffprobe: 'ffprobe is absent, so the decode-error and quality detectors do not run.',
  playwright: 'Playwright is absent, so PDF export is unavailable. HTML export works.',
  database: 'The database does not answer, so jobs and reports are not persisted.',
}

export default function App() {
  const [tab, setTab] = useState<TabId>('realtime')
  const [collapsed, setCollapsed] = useRailCollapsed()

  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: endpoints.health,
    refetchInterval: 30000,
  })
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: endpoints.settings })
  const thresholds = (settings?.thresholds as Record<string, number>) ?? {}

  const degraded = (health?.degraded as string[]) ?? []
  const railHealth = {
    ok: degraded.length === 0,
    label: degraded.length === 0 ? 'System healthy' : 'Degraded',
    detail:
      degraded.length === 0
        ? 'Analyzer host · every dependency answers'
        : degraded.map((item) => DEGRADED_NOTE[item] ?? `${item} is unavailable.`).join(' '),
  }

  return (
    <div className="min-h-full">
      <Sidebar
        active={tab}
        onSelect={setTab}
        collapsed={collapsed}
        onToggle={() => setCollapsed(!collapsed)}
        health={railHealth}
      />

      <main
        className="min-h-screen transition-[margin] duration-150"
        style={{ marginLeft: collapsed ? 72 : 232 }}
      >
        <div className={tab === 'realtime' ? '' : 'panel-hidden'}>
          <RealtimeTab thresholds={thresholds} />
        </div>
        <div className={tab === 'aging' ? '' : 'panel-hidden'}>
          <AgingTab thresholds={thresholds} />
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
    </div>
  )
}
