/**
 * hls.js player.
 *
 * The stream is loaded through `/api/proxy` because a browser cannot disable TLS
 * verification and CDN responses frequently carry no CORS headers. Every player event is
 * sent back over the session socket so a stall is correlated against the fetch events
 * measured at the same instant.
 *
 * The overlay labels its numbers "measured from the analyzer host": they reflect this
 * machine's network path, not a TV's.
 */

import Hls, { type ErrorData, Events, type FragLoadedData, type LevelSwitchedData } from 'hls.js'
import { useEffect, useRef, useState } from 'react'
import { PLAYER_METRICS_NOTE } from '../lib/constants'
import { kbps } from '../lib/format'
import { useSessionStore } from '../store/session'
import type { PlayerEventOut } from '../ws/messages'

interface Props {
  src: string | null
  onEvent: (event: PlayerEventOut) => void
}

interface Overlay {
  level: string
  bitrate: number | null
  buffer: number
  dropped: number
  stalls: number
}

export function Player({ src, onEvent }: Props) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const hlsRef = useRef<Hls | null>(null)
  const stallStartRef = useRef<number | null>(null)
  const startedAtRef = useRef<number>(0)
  const [overlay, setOverlay] = useState<Overlay>({
    level: '—',
    bitrate: null,
    buffer: 0,
    dropped: 0,
    stalls: 0,
  })
  const [error, setError] = useState<string | null>(null)
  const pushPlayerSample = useSessionStore((s) => s.pushPlayerSample)
  const openStall = useSessionStore((s) => s.openStall)
  const closeStall = useSessionStore((s) => s.closeStall)

  useEffect(() => {
    const video = videoRef.current
    if (!video || !src) return undefined

    setError(null)
    startedAtRef.current = performance.now()
    let currentVariant = ''

    const cleanup = () => {
      hlsRef.current?.destroy()
      hlsRef.current = null
    }

    if (Hls.isSupported()) {
      const hls = new Hls({
        lowLatencyMode: false,
        enableWorker: true,
        backBufferLength: 30,
      })
      hlsRef.current = hls
      hls.attachMedia(video)
      hls.loadSource(src)

      hls.on(Events.MANIFEST_PARSED, () => {
        void video.play().catch(() => {
          /* autoplay is blocked until the user interacts; the controls are visible */
        })
      })

      hls.on(Events.LEVEL_SWITCHED, (_e, data: LevelSwitchedData) => {
        const level = hls.levels[data.level]
        currentVariant = level ? `v${level.height}p@${Math.round(level.bitrate / 1000)}k` : ''
        setOverlay((o) => ({
          ...o,
          level: level ? `${level.width}x${level.height}` : '—',
          bitrate: level?.bitrate ?? null,
        }))
        onEvent({
          event: 'level_switched',
          level: data.level,
          bitrate: level?.bitrate,
          variant: currentVariant,
        })
      })

      hls.on(Events.FRAG_LOADED, (_e, data: FragLoadedData) => {
        onEvent({
          event: 'frag_loaded',
          variant: currentVariant,
          bitrate: data.frag.stats?.bwEstimate,
        })
      })

      hls.on(Events.ERROR, (_e, data: ErrorData) => {
        if (data.fatal) setError(`${data.type}: ${data.details}`)
        onEvent({
          event: 'error',
          error_type: data.type,
          details: data.details,
          fatal: data.fatal,
          variant: currentVariant,
        })
      })
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = src
    } else {
      setError('This browser cannot play HLS natively and hls.js reports no support.')
    }

    const onWaiting = () => {
      if (stallStartRef.current !== null) return
      stallStartRef.current = performance.now()
      openStall(currentVariant, Date.now())
      onEvent({ event: 'stall_start', variant: currentVariant })
    }

    const onPlaying = () => {
      if (startedAtRef.current) {
        onEvent({ event: 'startup', startup_ms: performance.now() - startedAtRef.current })
        startedAtRef.current = 0
      }
      if (stallStartRef.current === null) return
      const duration = (performance.now() - stallStartRef.current) / 1000
      stallStartRef.current = null
      closeStall(currentVariant, Date.now())
      setOverlay((o) => ({ ...o, stalls: o.stalls + 1 }))
      onEvent({ event: 'stall_end', stall_duration_s: duration, variant: currentVariant })
    }

    video.addEventListener('waiting', onWaiting)
    video.addEventListener('playing', onPlaying)

    const sampler = window.setInterval(() => {
      const buffered = video.buffered
      const ahead =
        buffered.length > 0 ? Math.max(0, buffered.end(buffered.length - 1) - video.currentTime) : 0
      const quality = video.getVideoPlaybackQuality?.()
      const dropped = quality?.droppedVideoFrames ?? 0
      setOverlay((o) => ({ ...o, buffer: ahead, dropped }))
      pushPlayerSample({ t: Date.now(), buffer_s: ahead, dropped })
      onEvent({ event: 'buffer', buffer_s: ahead, variant: currentVariant })
      if (dropped > 0) {
        onEvent({
          event: 'dropped_frames',
          dropped,
          total_frames: quality?.totalVideoFrames,
          variant: currentVariant,
        })
      }
    }, 1000)

    return () => {
      window.clearInterval(sampler)
      video.removeEventListener('waiting', onWaiting)
      video.removeEventListener('playing', onPlaying)
      cleanup()
    }
  }, [src, onEvent, pushPlayerSample, openStall, closeStall])

  return (
    <div className="card overflow-hidden">
      <div className="card-header">
        <h2 className="card-title">Player</h2>
        <span className="chip border-slate-200 bg-slate-50 text-[var(--rba-muted)]">
          {PLAYER_METRICS_NOTE}
        </span>
      </div>
      <div className="relative bg-black">
        {/* The element is never unmounted while a session runs, so playback survives a tab change. */}
        <video ref={videoRef} controls muted playsInline className="aspect-video w-full" />
        <div className="pointer-events-none absolute left-2 top-2 rounded bg-black/70 px-2 py-1 font-mono text-[11px] text-white">
          <div>rung {overlay.level}</div>
          <div>{kbps(overlay.bitrate)}</div>
          <div>buffer {overlay.buffer.toFixed(1)} s</div>
          <div>dropped {overlay.dropped}</div>
          <div>stalls {overlay.stalls}</div>
        </div>
      </div>
      {error && (
        <p className="border-t border-critical bg-red-50 px-4 py-2 text-sm text-critical">
          The player stopped: {error}
        </p>
      )}
      {!src && (
        <p className="px-4 py-6 text-sm text-[var(--rba-muted)]">
          Start an analysis to load the stream through the proxy.
        </p>
      )}
    </div>
  )
}
