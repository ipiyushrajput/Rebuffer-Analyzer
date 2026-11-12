import { afterEach, describe, expect, it, vi } from 'vitest'

// The client reads the base path once, at import, as the built bundle does.
async function clientUnder(base: string) {
  vi.resetModules()
  vi.stubEnv('BASE_URL', base)
  vi.stubEnv('VITE_API_BASE', '')
  vi.stubEnv('VITE_WS_BASE', '')
  return import('./client')
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('paths under the base the app is served from', () => {
  it('re-roots the backend paths the page is handed on /streamlens', async () => {
    const { api, publicPath } = await clientUnder('/streamlens/')
    expect(api.url('/health')).toBe('/streamlens/api/health')
    expect(publicPath('/api/proxy?u=https%3A%2F%2Fcdn%2Fm.m3u8&session=s1')).toBe(
      '/streamlens/api/proxy?u=https%3A%2F%2Fcdn%2Fm.m3u8&session=s1',
    )
    expect(publicPath('/api/drm/license')).toBe('/streamlens/api/drm/license')
  })

  it('leaves an absolute licence URL, the direct mode, as it is', async () => {
    const { publicPath } = await clientUnder('/streamlens/')
    expect(publicPath('https://licence.example/widevine')).toBe('https://licence.example/widevine')
    expect(publicPath('')).toBe('')
  })

  it('opens WebSockets below the base, on the page origin', async () => {
    const { wsUrl } = await clientUnder('/streamlens/')
    expect(wsUrl('/ws/jobs/abc')).toBe(`ws://${window.location.host}/streamlens/ws/jobs/abc`)
  })

  it('keeps the root layout when the app is served at /', async () => {
    const { api, publicPath, wsUrl } = await clientUnder('/')
    expect(api.url('/health')).toBe('/api/health')
    expect(publicPath('/api/proxy?u=x')).toBe('/api/proxy?u=x')
    expect(wsUrl('/ws/realtime/r1')).toBe(`ws://${window.location.host}/ws/realtime/r1`)
  })
})
