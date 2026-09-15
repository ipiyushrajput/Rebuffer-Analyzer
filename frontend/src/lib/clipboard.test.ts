/**
 * The copy buttons on the deployment host.
 *
 * The analyzer is served over plain HTTP on an IP address, which is not a secure context, so
 * `navigator.clipboard` is absent there and the async path cannot run at all. These tests
 * drive both routes.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyText } from './clipboard'

function withoutClipboardApi(): void {
  Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
}

afterEach(() => {
  vi.restoreAllMocks()
  Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
})

describe('copyText', () => {
  it('falls back to the selection path when the clipboard API is absent', async () => {
    withoutClipboardApi()
    const copied: string[] = []
    const exec = vi.fn(() => {
      copied.push((document.querySelector('textarea[readonly]') as HTMLTextAreaElement).value)
      return true
    })
    Object.defineProperty(document, 'execCommand', { value: exec, configurable: true })

    expect(await copyText('MED-021 evidence')).toBe(true)
    expect(copied).toEqual(['MED-021 evidence'])
    // The scratch textarea is removed whatever the outcome.
    expect(document.querySelector('textarea[readonly]')).toBeNull()
  })

  it('falls back when the clipboard API is present and rejects', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: () => Promise.reject(new Error('denied')) },
      configurable: true,
    })
    Object.defineProperty(document, 'execCommand', { value: () => true, configurable: true })
    expect(await copyText('escalation block')).toBe(true)
  })

  it('reports failure rather than throwing when neither route works', async () => {
    withoutClipboardApi()
    Object.defineProperty(document, 'execCommand', {
      value: () => {
        throw new Error('not supported')
      },
      configurable: true,
    })
    expect(await copyText('anything')).toBe(false)
    expect(document.querySelector('textarea[readonly]')).toBeNull()
  })

  it('uses the clipboard API when it is available', async () => {
    const writeText = vi.fn(() => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    expect(await copyText('https://cdn/x.m3u8')).toBe(true)
    expect(writeText).toHaveBeenCalledWith('https://cdn/x.m3u8')
  })
})
