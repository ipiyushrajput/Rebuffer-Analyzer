/**
 * The realtime socket.
 *
 * Events flow in from the analysis engine; player telemetry flows back out on the same
 * socket, so a stall the player reports is correlated against the backend events measured at
 * the same instant.
 */

import { useCallback, useEffect, useRef } from 'react'
import { wsUrl } from '../api/client'
import { useSessionStore } from '../store/session'
import type { ClientMessage, PlayerEventOut, ServerMessage } from './messages'

const RECONNECT_BASE_MS = 1000
const RECONNECT_MAX_MS = 15000
const PING_INTERVAL_MS = 20000

export function useSessionSocket(sessionId: string | null) {
  const socketRef = useRef<WebSocket | null>(null)
  const attemptRef = useRef(0)
  const closedRef = useRef(false)
  const ingest = useSessionStore((s) => s.ingest)
  const setConnected = useSessionStore((s) => s.setConnected)

  const send = useCallback((message: ClientMessage) => {
    const socket = socketRef.current
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(message))
    }
  }, [])

  const sendPlayerEvent = useCallback(
    (event: PlayerEventOut) => {
      send({ type: 'player_event', data: { ts: new Date().toISOString(), ...event } })
    },
    [send],
  )

  useEffect(() => {
    if (!sessionId) return undefined
    closedRef.current = false
    let pingTimer: number | undefined
    let reconnectTimer: number | undefined

    const connect = () => {
      if (closedRef.current) return
      const socket = new WebSocket(wsUrl(`/ws/realtime/${sessionId}`))
      socketRef.current = socket

      socket.onopen = () => {
        attemptRef.current = 0
        setConnected(true)
        pingTimer = window.setInterval(() => send({ type: 'ping', data: {} }), PING_INTERVAL_MS)
      }

      socket.onmessage = (raw) => {
        try {
          ingest(JSON.parse(raw.data as string) as ServerMessage)
        } catch {
          /* a frame that does not parse is dropped; the next one still renders */
        }
      }

      socket.onclose = () => {
        setConnected(false)
        window.clearInterval(pingTimer)
        if (closedRef.current) return
        attemptRef.current += 1
        const delay = Math.min(RECONNECT_BASE_MS * 2 ** (attemptRef.current - 1), RECONNECT_MAX_MS)
        reconnectTimer = window.setTimeout(connect, delay)
      }

      socket.onerror = () => socket.close()
    }

    connect()

    return () => {
      closedRef.current = true
      window.clearInterval(pingTimer)
      window.clearTimeout(reconnectTimer)
      socketRef.current?.close()
      socketRef.current = null
      setConnected(false)
    }
  }, [sessionId, ingest, setConnected, send])

  return { sendPlayerEvent }
}
