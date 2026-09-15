/**
 * Carrying a channel from All channels into Realtime or Aging.
 *
 * The app has no router: tabs are mounted once and hidden with CSS, so a running session
 * keeps its player and its samples. A channel therefore travels as state rather than as a
 * route — the sending tab raises `token`, the receiving tab seeds its form from it once, and
 * every field stays editable. Nothing starts on its own.
 */

import { useEffect } from 'react'
import { emptyForm, type UrlFormValue } from '../components/UrlForm'

export interface ChannelPrefill {
  playback_url: string
  channel_name: string
  service_id: string
  country: string
  /** Raised on every send, so choosing the same channel twice seeds the form again. */
  token: number
}

/**
 * The name the analysis is filed under.
 *
 * The job form carries one name, so the service id and country ride with it: an operator
 * looking at Analysed channels three days later needs to know which channel in which
 * country a run came from, and the name is the only field that reaches the report.
 */
export function channelLabel(prefill: ChannelPrefill): string {
  const parts = [prefill.channel_name.trim(), prefill.service_id.trim(), prefill.country.trim()]
  return parts.filter(Boolean).join(' · ')
}

export function applyPrefill(form: UrlFormValue, prefill: ChannelPrefill): UrlFormValue {
  // The comparison URLs and job options are the operator's to set; only the channel is filled.
  return { ...form, playback_url: prefill.playback_url, channel_name: channelLabel(prefill) }
}

/**
 * Seed a job form when a channel arrives, keeping whatever the operator has already chosen.
 *
 * `recordEvidence` matches the tab's own empty form, so a prefill that lands while a
 * previous run is on screen still starts from that tab's defaults.
 */
export function usePrefill(
  prefill: ChannelPrefill | null,
  setForm: (value: UrlFormValue) => void,
  recordEvidence: boolean,
): void {
  useEffect(() => {
    if (!prefill) return
    setForm(applyPrefill(emptyForm(recordEvidence), prefill))
    // Only the token advances a send; the object identity changes on every render.
  }, [prefill?.token]) // eslint-disable-line react-hooks/exhaustive-deps
}
