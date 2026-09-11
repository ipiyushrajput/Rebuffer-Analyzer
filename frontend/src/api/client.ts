/** REST client. Paths are relative so the app works behind nginx and behind the Vite proxy. */

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(message)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...init?.headers,
    },
  })
  if (!response.ok) {
    let detail: unknown = await response.text()
    try {
      detail = JSON.parse(detail as string)
    } catch {
      /* the body is not JSON; the text is the detail */
    }
    const message =
      typeof detail === 'object' && detail !== null && 'detail' in detail
        ? String((detail as { detail: unknown }).detail)
        : `Request to ${path} returned HTTP ${response.status}`
    throw new ApiError(message, response.status, detail)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  upload: <T>(path: string, form: FormData) =>
    request<T>(path, { method: 'POST', body: form }),
  url: (path: string) => `${API_BASE}${path}`,
}

export function wsUrl(path: string): string {
  const configured = import.meta.env.VITE_WS_BASE
  if (configured) return `${configured}${path.replace(/^\/ws/, '')}`
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}${path}`
}

// --- typed endpoints --------------------------------------------------------

export interface JobSummary {
  id: string
  type: string
  status: string
  channel_name: string
  urls: Record<string, string | null>
  options: Record<string, unknown>
  progress: number
  elapsed_s: number
  remaining_s: number
  created_at: string
  started_at: string | null
  ends_at: string | null
  finished_at: string | null
  counts: Record<string, number>
  verdict: Record<string, unknown> | null
  error: string | null
  parent_job_id: string | null
}

export interface RealtimeSession extends JobSummary {
  ws_url: string
  player_url: string
  player_metrics_note: string
}

export const endpoints = {
  health: () => api.get<Record<string, unknown>>('/health'),

  createRealtime: (body: unknown) => api.post<RealtimeSession>('/realtime/sessions', body),
  stopRealtime: (id: string) => api.del<JobSummary>(`/realtime/sessions/${id}`),
  realtimeResult: (id: string) => api.get<Record<string, unknown>>(`/realtime/sessions/${id}/result`),
  realtimeManifests: (id: string) =>
    api.get<{ manifests: { layer: string; variant: string; url: string; raw: string; at: string }[] }>(
      `/realtime/sessions/${id}/manifests`,
    ),
  realtimeSnapshot: (id: string, variant: string, at?: string) =>
    api.get<{
      variant: string
      at: string
      url: string
      raw: string
      diff: string[]
      available: string[]
      summary: Record<string, unknown>
    }>(`/realtime/sessions/${id}/snapshots?variant=${encodeURIComponent(variant)}${at ? `&at=${encodeURIComponent(at)}` : ''}`),

  createAging: (body: unknown) => api.post<JobSummary>('/aging/jobs', body),
  listAging: () => api.get<{ jobs: JobSummary[] }>('/aging/jobs'),
  readAging: (id: string) => api.get<JobSummary>(`/aging/jobs/${id}`),
  cancelAging: (id: string) => api.del<JobSummary>(`/aging/jobs/${id}`),

  createBulk: (form: FormData) => api.upload<JobSummary>('/bulk/jobs', form),
  readBulk: (id: string) =>
    api.get<JobSummary & { items: Record<string, unknown>[] }>(`/bulk/jobs/${id}`),
  listBulk: () => api.get<{ jobs: JobSummary[] }>('/bulk/jobs'),
  cancelBulk: (id: string) => api.del<JobSummary>(`/bulk/jobs/${id}`),
  validateBulk: (form: FormData) =>
    api.upload<{ rows: Record<string, unknown>[]; errors: Record<string, unknown>[] }>(
      '/bulk/validate',
      form,
    ),

  listReports: (query = '') => api.get<{ reports: Record<string, unknown>[] }>(`/reports${query}`),
  deleteReport: (id: number) => api.del<{ deleted: boolean }>(`/reports/${id}`),

  settings: () => api.get<Record<string, unknown>>('/settings'),
  saveSettings: (body: unknown) => api.put<Record<string, unknown>>('/settings', body),
  rules: () => api.get<{ count: number; rules: Record<string, unknown>[] }>('/settings/rules'),
}
