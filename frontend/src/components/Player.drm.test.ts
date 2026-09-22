/**
 * What the preview tells an operator when a protected channel will not play.
 *
 * `keySystemNoAccess` is the message the deployment actually produces, and on its own it
 * names nothing anybody can act on. Seeing it at all proves detection and wiring worked:
 * hls.js's EMEController returns early everywhere when `emeEnabled` is false, so the error is
 * unreachable unless the backend detected Widevine and the page passed `drmSystems`. What
 * failed is the page's origin — Encrypted Media Extensions exist only on a secure context —
 * and nothing about the stream, the key server or the licence server is involved.
 */

import { describe, expect, it } from 'vitest'
import { explainDrm } from './Player'

describe('explainDrm', () => {
  it('names the origin and the three ways round it for a refused key system', () => {
    const text = explainDrm('keySystemNoAccess', 'Widevine')

    expect(text).toContain('Widevine')
    expect(text).toContain('secure context')
    expect(text).toContain('http://localhost')
    expect(text).toMatch(/HTTPS/)
  })

  it('says the analysis is unaffected, because it is', () => {
    // The backend decrypts the segments itself; a preview that will not start costs the
    // picture and no measurement.
    expect(explainDrm('keySystemNoAccess', 'Widevine')).toContain('decrypted on the backend')
  })

  it('points a licence failure at the licence URL rather than at the origin', () => {
    const text = explainDrm('keyLoadError', 'Widevine')

    expect(text).toContain('Settings')
    expect(text).not.toContain('secure context')
  })

  it('has nothing to add to an error that is not about DRM', () => {
    expect(explainDrm('fragLoadError', 'Widevine')).toBe('')
    expect(explainDrm('manifestLoadError', 'Widevine')).toBe('')
  })

  it('never invents a hedge', () => {
    const hedges = [/\bmay be\b/i, /\bmight\b/i, /\bpossibly\b/i, /\blikely\b/i, /\bappears to\b/i]
    const messages = [
      'keySystemNoAccess',
      'keySystemNoKeys',
      'keySystemNoSession',
      'keySystemNoInitData',
      'keySystemLicenseRequestFailed',
      'keyLoadError',
      'keyLoadTimeOut',
      'fragDecryptError',
    ].map((detail) => explainDrm(detail, 'Widevine'))

    for (const message of messages) {
      expect(message).not.toBe('')
      for (const hedge of hedges) expect(message).not.toMatch(hedge)
    }
  })
})
