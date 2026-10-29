import { describe, expect, it } from 'vitest'
import { curlCommand, requestUrlOf, shellQuote } from './curl'

describe('copy as cURL', () => {
  it('quotes what the shell would split or expand', () => {
    expect(shellQuote('https://cdn.example/a.ts')).toBe('https://cdn.example/a.ts')
    expect(shellQuote('https://cdn.example/a.ts?x=1&y=2')).toBe(`'https://cdn.example/a.ts?x=1&y=2'`)
    expect(shellQuote("it's")).toBe(`'it'"'"'s'`)
  })

  it('sends the run’s User-Agent and does not follow redirects', () => {
    const command = curlCommand('https://cdn.example/x.m3u8?t=a&b=c', 'Mozilla/5.0 (SMART-TV; Tizen 9.0)')
    expect(command).toBe(
      `curl -sS -k -D - -o /dev/null -A 'Mozilla/5.0 (SMART-TV; Tizen 9.0)' 'https://cdn.example/x.m3u8?t=a&b=c'`,
    )
    expect(command).not.toContain(' -L ')
  })

  it('finds the request an evidence sample names', () => {
    expect(requestUrlOf({ uri: 'https://cdn.example/seg.ts', status: 404 })).toBe('https://cdn.example/seg.ts')
    expect(requestUrlOf({ uri: 'seg.ts' })).toBeNull()
    expect(requestUrlOf({ msn: 4 })).toBeNull()
  })
})
