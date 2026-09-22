/**
 * Application shell.
 *
 * The rail and header are persistent; only the body changes. Tabs are hidden with CSS
 * rather than unmounted, so a running Realtime session keeps its player, its socket and
 * every sample it has collected while the operator looks at another screen.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { endpoints } from './api/client'
import { Sidebar, useRailCollapsed, type TabId } from './components/layout/Sidebar'
import { AgingTab } from './tabs/Aging'
import { AllChannelsTab } from './tabs/AllChannels'
import { BulkTab, type BulkPrefill } from './tabs/Bulk'
import { AutomatedBatchTab } from './tabs/AutomatedBatch'
import { CascadaDataTab } from './tabs/CascadaData'
import { ChannelsTab } from './tabs/Channels'
import { RealtimeTab } from './tabs/Realtime'
import { ReportsTab } from './tabs/Reports'
import { SettingsTab } from './tabs/Settings'
import type { ChannelPrefill } from './lib/prefill'

const DEGRADED_NOTE: Record<string, string> = {
  ffmpeg: 'ffmpeg is absent, so the decode-error and quality detectors do not run.',
  ffprobe: 'ffprobe is absent, so the decode-error and quality detectors do not run.',
  playwright: 'Playwright is absent, so PDF export is unavailable. HTML export works.',
  database: 'The database does not answer, so jobs and reports are not persisted.',
}

export default function App() {
  const [tab, setTab] = useState<TabId>('realtime')
  const [collapsed, setCollapsed] = useRailCollapsed()
  /* The channel the Analysed channels tab has open, so a stopped run lands on its own page. */
  const [openChannel, setOpenChannel] = useState<string | null>(null)
  /*
   * A channel sent from All channels into Realtime or Aging. There is no router — tabs stay
   * mounted so a running session survives — so the channel travels as state. The token rises
   * on every send, which is what lets the same channel be sent twice.
   */
  const [prefill, setPrefill] = useState<Record<'realtime' | 'aging', ChannelPrefill | null>>({
    realtime: null,
    aging: null,
  })
  const sendToAnalysis = (
    target: 'realtime' | 'aging',
    channel: Omit<ChannelPrefill, 'token'>,
  ) => {
    setPrefill((current) => ({
      ...current,
      [target]: { ...channel, token: (current[target]?.token ?? 0) + 1 },
    }))
    setTab(target)
  }
  /*
   * A file of channels sent from CASCADA Data into Bulk analysis. It travels the same way a
   * channel does, for the same reason: the Bulk tab stays mounted with whatever batch it is
   * running, and the token is what lets the same set be sent twice.
   */
  const [bulkPrefill, setBulkPrefill] = useState<BulkPrefill | null>(null)
  const sendToBulk = (file: File) => {
    setBulkPrefill((current) => ({ file, token: (current?.token ?? 0) + 1 }))
    setTab('bulk')
  }
  const queryClient = useQueryClient()

  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: endpoints.health,
    refetchInterval: 30000,
  })
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: endpoints.settings })
  const thresholds = (settings?.thresholds as Record<string, number>) ?? {}

  const degraded = (health?.degraded as string[]) ?? []
  /*
   * Bento4 is optional rather than degrading: `cenc` — every TV Plus channel measured so far
   * — is decrypted in process with no binary at all. What it costs to be without it is one
   * scheme, so it is stated as a capability rather than counted as a failure.
   */
  const checks = (health?.checks as Record<string, { installed?: boolean }> | undefined) ?? {}
  const noBento4 = checks.mp4decrypt?.installed === false
  const railHealth = {
    ok: degraded.length === 0,
    label: degraded.length === 0 ? 'System healthy' : 'Degraded',
    detail: [
      degraded.length === 0
        ? 'Analyzer host · every dependency answers'
        : degraded.map((item) => DEGRADED_NOTE[item] ?? `${item} is unavailable.`).join(' '),
      noBento4 ? 'mp4decrypt is not installed, so cbcs tracks are not decrypted.' : '',
    ]
      .filter(Boolean)
      .join(' '),
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
          <RealtimeTab
            thresholds={thresholds}
            prefill={prefill.realtime}
            onArchived={(sessionId) => {
              void queryClient.invalidateQueries({ queryKey: ['channels'] })
              void queryClient.invalidateQueries({ queryKey: ['reports'] })
              setOpenChannel(sessionId)
              setTab('channels')
            }}
          />
        </div>
        <div className={tab === 'channels' ? '' : 'panel-hidden'}>
          <ChannelsTab
            thresholds={thresholds}
            openId={tab === 'channels' ? openChannel : null}
            onOpenChange={setOpenChannel}
          />
        </div>
        <div className={tab === 'catalogue' ? '' : 'panel-hidden'}>
          <AllChannelsTab onAnalyse={sendToAnalysis} />
        </div>
        <div className={tab === 'cascada' ? '' : 'panel-hidden'}>
          <CascadaDataTab onAnalyse={sendToAnalysis} onBulk={sendToBulk} />
        </div>
        <div className={tab === 'batch' ? '' : 'panel-hidden'}>
          <AutomatedBatchTab />
        </div>
        <div className={tab === 'aging' ? '' : 'panel-hidden'}>
          <AgingTab thresholds={thresholds} prefill={prefill.aging} />
        </div>
        <div className={tab === 'bulk' ? '' : 'panel-hidden'}>
          <BulkTab prefill={bulkPrefill} />
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
