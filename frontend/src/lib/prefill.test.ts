/**
 * Sending a channel from All channels into a job form.
 *
 * The contract the tab depends on: the channel fills in, everything else stays the
 * operator's, and the same channel can be sent twice.
 */

import { describe, expect, it } from 'vitest'
import { emptyForm } from '../components/UrlForm'
import { applyPrefill, channelLabel, type ChannelPrefill } from './prefill'

const CHANNEL: ChannelPrefill = {
  playback_url: 'https://cdn/x.m3u8?ads.service_id=US300068X7',
  channel_name: 'Samsung Television Network',
  service_id: 'US300068X7',
  country: 'US',
  token: 1,
}

describe('channelLabel', () => {
  it('carries the service id and country, which the job form has no field for', () => {
    expect(channelLabel(CHANNEL)).toBe('Samsung Television Network · US300068X7 · US')
  })

  it('leaves out a field the catalogue did not fill', () => {
    expect(channelLabel({ ...CHANNEL, service_id: '', country: '' })).toBe(
      'Samsung Television Network',
    )
  })
})

describe('applyPrefill', () => {
  it('fills the channel and leaves every other choice alone', () => {
    const chosen = {
      ...emptyForm(true),
      origin_url: 'https://origin/x.m3u8',
      options: { ...emptyForm(true).options, ua_profile: 'tizen7', check_sets: ['subtitles'] },
    }
    const filled = applyPrefill(chosen, CHANNEL)

    expect(filled.playback_url).toBe(CHANNEL.playback_url)
    expect(filled.channel_name).toBe('Samsung Television Network · US300068X7 · US')
    // The operator's own selections survive the prefill.
    expect(filled.origin_url).toBe('https://origin/x.m3u8')
    expect(filled.options.ua_profile).toBe('tizen7')
    expect(filled.options.check_sets).toEqual(['subtitles'])
  })

  it('never mutates the form it was handed', () => {
    const before = emptyForm(false)
    applyPrefill(before, CHANNEL)
    expect(before.playback_url).toBe('')
  })
})
