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
 *
 * A Widevine channel plays here too, and nothing about it is asked of the analyst. The
 * backend says what the channel needs when the session starts, and this configures hls.js
 * from that: EME on, and the licence acquired through `/api/drm/license`, which relays the
 * challenge to the licence server the deployment is configured with. The browser never sees
 * that server's address, and a licence server that sends no CORS headers still plays. A
 * deployment can choose to post the challenge straight from the browser instead, in
 * Settings -> DRM; the relay is the default because it works whatever the licence server's
 * CORS policy is.
 *
 * EME needs a secure context, and that is a property of the page's origin, not of the
 * stream. `navigator.requestMediaKeySystemAccess` is undefined on a plain `http://` origin
 * that is not localhost, so hls.js fails with `keySystemNoAccess` — a message that names
 * nothing an operator can act on. This checks for it first and says which origin is the
 * problem and what to do: open the app at `http://localhost:8080` on the deployment host,
 * trust the origin in Chrome, or serve the page over HTTPS. Nothing about the *stream* or
 * the *licence server* has to be HTTPS; only the page.
 */

import Hls, { type ErrorData, Events, type FragLoadedData, type LevelSwitchedData } from 'hls.js'
import { useEffect, useRef, useState } from 'react'
import { PLAYER_METRICS_NOTE } from '../lib/constants'
import { kbps } from '../lib/format'
import { useSessionStore } from '../store/session'
import type { PlayerDrm } from '../api/client'
import type { PlayerEventOut } from '../ws/messages'

interface Props {
  src: string | null
  onEvent: (event: PlayerEventOut) => void
  /** What the backend says this channel needs. Absent or unprotected means a clear channel. */
  drm?: PlayerDrm | null
}

/**
 * What an hls.js DRM error actually means, in a sentence an operator can act on.
 *
 * `keySystemNoAccess` is the one worth spelling out: it is almost always the page's origin
 * rather than anything about the channel, the key server or the licence server, and the raw
 * message says none of that.
 */
export function explainDrm(details: string, systemLabel: string): string {
  switch (details) {
    case 'keySystemNoAccess':
    case 'keySystemNoKeys':
      return (
        `The browser refused ${systemLabel} key-system access at ${window.location.origin}. ` +
        'Encrypted Media Extensions are available only on a secure context: an HTTPS origin, ' +
        'http://localhost or http://127.0.0.1. Open the app on the deployment host itself, ' +
        'trust this origin in Chrome, or serve the page over HTTPS. The analysis is ' +
        'unaffected either way — the segments are decrypted on the backend.'
      )
    case 'keySystemNoSession':
      return `The browser granted ${systemLabel} access and then created no key session.`
    case 'keySystemNoInitData':
      return 'The stream carries no initialisation data for this key system.'
    case 'keySystemLicenseRequestFailed':
    case 'keyLoadError':
      return (
        'The licence request failed. Check the licence URL in Settings -> DRM, and that this ' +
        'host has a route to it.'
      )
    case 'keyLoadTimeOut':
      return 'The licence server did not answer within the timeout.'
    case 'fragDecryptError':
      return 'A fragment did not decrypt with the licence the browser was issued.'
    default:
      return ''
  }
}

interface Overlay {
  level: string
  bitrate: number | null
  buffer: number
  dropped: number
  stalls: number
}

export function Player({ src, onEvent, drm }: Props) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const hlsRef = useRef<Hls | null>(null)
  const stallStartRef = useRef<number | null>(null)
  const startedAtRef = useRef<number>(0)
  /* The rung the player is on, carried into every sample so the played-rung chart has a series. */
  const bitrateRef = useRef<number | null>(null)
  const levelRef = useRef<number | null>(null)
  /* Fragments decrypted in the browser, so the first one is logged and the rest are not. */
  const decryptedRef = useRef<number>(0)
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
    if (!video) return undefined

    if (!src) {
      // The session ended. Stop the element and clear the overlay so nothing reads live.
      video.pause()
      video.removeAttribute('src')
      video.load()
      setOverlay({ level: '—', bitrate: null, buffer: 0, dropped: 0, stalls: 0 })
      setError(null)
      return undefined
    }

    setError(null)
    startedAtRef.current = performance.now()
    decryptedRef.current = 0
    let currentVariant = ''

    /*
     * Tearing down hls.js is not enough on its own. The element keeps whatever it last
     * loaded — and on a browser playing HLS natively it keeps playing it — so the element
     * is stopped and emptied too. Stopping the analysis has to stop the picture.
     */
    /*
     * Fired by the media element the moment it meets encrypted content, before hls.js has
     * said anything. On a page that cannot do EME at all this is the last thing that happens.
     */
    const onEncrypted = () => {
      onEvent({ event: 'drm', details: 'media_encrypted' })
    }

    const cleanup = () => {
      video.removeEventListener('encrypted', onEncrypted)
      hlsRef.current?.destroy()
      hlsRef.current = null
      video.pause()
      video.removeAttribute('src')
      video.load()
    }

    /*
     * EME is switched on only for a channel that needs it. Turning it on for every stream
     * makes a clear channel wait on a key session that never arrives, so a clear channel is
     * configured exactly as it was before DRM existed.
     */
    const protectedStream = Boolean(drm?.key_system && drm?.license_path)
    /*
     * `requestMediaKeySystemAccess` is undefined outside a secure context — hls.js checks
     * exactly this and turns it into `keySystemNoAccess`, which names nothing actionable.
     * Both conditions are read because a browser can withhold the method for reasons of its
     * own, and the message has to be true for whichever one applies.
     */
    const emeAvailable =
      window.isSecureContext && typeof navigator.requestMediaKeySystemAccess === 'function'

    if (protectedStream && !emeAvailable) {
      const reason = explainDrm('keySystemNoAccess', drm!.system_label)
      setError(reason)
      onEvent({ event: 'drm', details: 'secure_context_missing', error_type: reason })
      return () => {
        cleanup()
      }
    }

    if (Hls.isSupported()) {
      const hls = new Hls({
        lowLatencyMode: false,
        enableWorker: true,
        backBufferLength: 30,
        emeEnabled: protectedStream,
        ...(protectedStream
          ? {
              drmSystems: {
                [drm!.key_system]: {
                  licenseUrl: drm!.license_path,
                  /*
                   * hls.js defaults both to '', which asks the CDM for its own idea of a
                   * minimum. Naming them makes the request reproducible across browsers and
                   * keeps a software CDM from being refused for a level it never claimed.
                   */
                  videoRobustness: 'SW_SECURE_CRYPTO',
                  audioRobustness: 'SW_SECURE_CRYPTO',
                },
              },
              ...(drm!.license_request_path === 'direct'
                ? {
                    /*
                     * Posting the challenge straight from the browser. It is the deployment's
                     * choice and only works where the licence server allows a cross-origin
                     * POST — which is exactly what it proves. The relay is the default.
                     */
                    licenseXhrSetup: (xhr: XMLHttpRequest, url: string) => {
                      xhr.open('POST', url, true)
                      xhr.setRequestHeader('Content-Type', 'application/octet-stream')
                    },
                  }
                : {}),
            }
          : {}),
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
        bitrateRef.current = level?.bitrate ?? null
        levelRef.current = data.level
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
        /*
         * `bwEstimate` is what hls.js measured the network doing, not the rung being played.
         * It went into `bitrate` until a 2.8 Gbps estimate overflowed that column and took a
         * whole batch of samples with it — and until then it was drawing throughput spikes on
         * the played-rung chart. The rung comes from LEVEL_SWITCHED and nowhere else.
         */
        onEvent({
          event: 'frag_loaded',
          variant: currentVariant,
          bandwidth_bps: data.frag.stats?.bwEstimate,
        })
      })

      if (protectedStream) {
        /*
         * The licence lifecycle, in the run's own event log rather than only in the console.
         * A DRM failure is then evidence in the analysis — and so is a DRM success, which is
         * what tells an operator the relay and the key server worked on this channel.
         */
        hls.on(Events.KEY_LOADED, () => {
          onEvent({ event: 'drm', details: 'key_loaded', variant: currentVariant })
        })
        hls.on(Events.FRAG_DECRYPTED, () => {
          if (decryptedRef.current === 0) {
            onEvent({ event: 'drm', details: 'first_fragment_decrypted', variant: currentVariant })
          }
          decryptedRef.current += 1
        })
        video.addEventListener('encrypted', onEncrypted)
      }

      hls.on(Events.ERROR, (_e, data: ErrorData) => {
        // The raw `details` is a hls.js identifier; this is what it means here.
        const explained = explainDrm(String(data.details), drm?.system_label ?? 'the key system')
        if (data.fatal) setError(explained || `${data.type}: ${data.details}`)
        onEvent({
          event: 'error',
          error_type: data.type,
          details: explained ? `${data.details} — ${explained}` : String(data.details),
          fatal: data.fatal,
          variant: currentVariant,
        })
      })
    } else if (video.canPlayType('application/vnd.apple.mpegurl') && !protectedStream) {
      video.src = src
    } else if (protectedStream) {
      setError(
        `This browser plays HLS natively rather than through hls.js, and a ${drm!.system_label} ` +
          'stream needs hls.js to acquire its licence. The analysis is unaffected: it decrypts ' +
          'the segments on the backend.',
      )
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
      pushPlayerSample({
        t: Date.now(),
        buffer_s: ahead,
        dropped,
        bitrate: bitrateRef.current ?? undefined,
        level: levelRef.current ?? undefined,
      })
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
  }, [src, onEvent, pushPlayerSample, openStall, closeStall, drm])

  return (
    <div className="card overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pb-2.5 pt-3.5">
        <h3 className="card-title">Player</h3>
        <span className="chip-neutral">{PLAYER_METRICS_NOTE}</span>
      </div>
      <div className="relative bg-ink">
        {/* The element is never unmounted while a session runs, so playback survives a tab change. */}
        <video ref={videoRef} controls muted playsInline className="aspect-video w-full" />
        <div className="pointer-events-none absolute left-2.5 top-2.5 space-y-0.5 rounded-tile bg-ink/75 px-2.5 py-1.5 font-mono text-[11px] leading-snug text-white backdrop-blur-sm">
          <div>rung {overlay.level}</div>
          <div>{kbps(overlay.bitrate)}</div>
          <div>buffer {overlay.buffer.toFixed(1)} s</div>
          <div>dropped {overlay.dropped}</div>
          <div>stalls {overlay.stalls}</div>
        </div>
      </div>
      {error && (
        <p className="border-t border-pink-200 bg-pink-50 px-4 py-2.5 text-small text-pink-600">
          The player stopped: {error}
        </p>
      )}
      {src && drm?.protected && !drm.configured && (
        <p className="border-t border-violet-200 bg-violet-50 px-4 py-2.5 text-small text-violet-700">
          This channel is protected with {drm.system_label} and no licence URL is configured, so
          the picture will not start. Set it in Settings &rarr; DRM. The analysis runs either
          way: the segments are decrypted on the backend.
        </p>
      )}
      {!src && (
        <p className="px-4 py-4 text-small text-ink-muted">
          Start an analysis to load the stream through the proxy.
        </p>
      )}
    </div>
  )
}
