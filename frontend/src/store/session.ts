/**
 * Realtime session state.
 *
 * Series are capped so a long session cannot grow without bound; the report renders from the
 * stored samples on the backend, so trimming here loses nothing.
 */

import { create } from 'zustand'
import type {
  EventData,
  FindingData,
  PlaylistSnapshotData,
  SamplingState,
  SegmentResultData,
  ServerMessage,
  VerdictData,
} from '../ws/messages'

const SERIES_LIMIT = 4000
const EVENT_LIMIT = 1000

export interface BufferPoint {
  t: number
  level: number
  source: 'player' | 'vpb'
  variant: string
}

export interface StallBand {
  start: number
  end: number | null
  source: 'player' | 'vpb'
  variant: string
}

/** One engine event as the store keeps it: the payload, plus what the envelope carried. */
export type SessionEvent = EventData & {
  ts: string
  variant_id: string | null
  layer: string | null
}

export interface PlayerSample {
  t: number
  buffer_s?: number
  level?: number
  bitrate?: number
  dropped?: number
}

export interface SessionState {
  sessionId: string | null
  status: string
  channelName: string
  playerUrl: string | null
  startedAt: number | null
  elapsed: number
  progress: number

  findings: Record<string, FindingData>
  verdict: VerdictData | null
  counts: Record<string, number>
  sampling: SamplingState | null

  snapshots: PlaylistSnapshotData[]
  segments: SegmentResultData[]
  events: SessionEvent[]
  playerSamples: PlayerSample[]
  stalls: StallBand[]
  vpbPoints: BufferPoint[]

  connected: boolean

  start: (id: string, channelName: string, playerUrl: string) => void
  ingest: (message: ServerMessage) => void
  setConnected: (value: boolean) => void
  pushPlayerSample: (sample: PlayerSample) => void
  openStall: (variant: string, at: number) => void
  closeStall: (variant: string, at: number) => void
  reset: () => void
}

const initial = {
  sessionId: null,
  status: 'IDLE',
  channelName: '',
  playerUrl: null,
  startedAt: null,
  elapsed: 0,
  progress: 0,
  findings: {},
  verdict: null,
  counts: {},
  sampling: null,
  snapshots: [],
  segments: [],
  events: [],
  playerSamples: [],
  stalls: [],
  vpbPoints: [],
  connected: false,
} satisfies Omit<
  SessionState,
  'start' | 'ingest' | 'setConnected' | 'pushPlayerSample' | 'openStall' | 'closeStall' | 'reset'
>

function trim<T>(items: T[], limit = SERIES_LIMIT): T[] {
  return items.length > limit ? items.slice(items.length - limit) : items
}

export const useSessionStore = create<SessionState>((set, get) => ({
  ...initial,

  start: (id, channelName, playerUrl) =>
    set({ ...initial, sessionId: id, channelName, playerUrl, status: 'RESOLVING', startedAt: Date.now() }),

  setConnected: (value) => set({ connected: value }),

  reset: () => set({ ...initial }),

  pushPlayerSample: (sample) =>
    set((state) => ({ playerSamples: trim([...state.playerSamples, sample]) })),

  openStall: (variant, at) =>
    set((state) => ({ stalls: [...state.stalls, { start: at, end: null, source: 'player', variant }] })),

  closeStall: (_variant, at) =>
    set((state) => {
      const stalls = [...state.stalls]
      for (let i = stalls.length - 1; i >= 0; i -= 1) {
        if (stalls[i].end === null && stalls[i].source === 'player') {
          stalls[i] = { ...stalls[i], end: at }
          break
        }
      }
      return { stalls }
    }),

  ingest: (message) => {
    const state = get()
    switch (message.type) {
      case 'status': {
        const data = message.data as Record<string, unknown>
        set({
          status: String(data.state ?? data.status ?? state.status),
          channelName: String(data.channel ?? data.channel_name ?? state.channelName),
        })
        break
      }
      case 'finding': {
        const finding = message.data as unknown as FindingData
        const key = `${finding.rule_id}|${finding.stream_layer}|${finding.variant ?? ''}`
        set({ findings: { ...state.findings, [key]: finding } })
        break
      }
      case 'verdict': {
        set({ verdict: message.data as unknown as VerdictData })
        break
      }
      case 'metric': {
        const data = message.data as Record<string, unknown>
        set({
          progress: Number(data.progress ?? state.progress),
          elapsed: Number(data.elapsed_s ?? state.elapsed),
          counts: (data.counts as Record<string, number>) ?? state.counts,
          // Why sampling measured what it measured. Carried on the progress metric so the
          // screen can explain a zero instead of showing an empty grid.
          sampling: (data.sampling as SamplingState | undefined) ?? state.sampling,
        })
        break
      }
      case 'playlist_snapshot': {
        set({ snapshots: trim([...state.snapshots, message.data as unknown as PlaylistSnapshotData]) })
        break
      }
      case 'segment_result': {
        set({ segments: trim([...state.segments, message.data as unknown as SegmentResultData]) })
        break
      }
      case 'event': {
        const data = message.data as unknown as EventData
        // The rendition and layer live on the envelope, not in the payload. Keeping them
        // here is what lets a consumer say which rung an event belongs to.
        set({
          events: trim(
            [
              {
                ...data,
                ts: message.ts,
                variant_id: message.variant_id ?? null,
                layer: message.layer ?? null,
              },
              ...state.events,
            ],
            EVENT_LIMIT,
          ),
        })
        break
      }
      default:
        break
    }
  },
}))

export function findingList(state: SessionState): FindingData[] {
  const rank: Record<string, number> = { CRITICAL: 4, ERROR: 3, WARN: 2, INFO: 1, PASS: 0 }
  return Object.values(state.findings).sort(
    (a, b) => rank[b.severity] - rank[a.severity] || b.count - a.count || a.rule_id.localeCompare(b.rule_id),
  )
}
