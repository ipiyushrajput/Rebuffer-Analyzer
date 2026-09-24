/** REST client. Paths are relative so the app works behind nginx and behind the Vite proxy. */

import type { PlaylistSnapshotData, SegmentResultData } from '../ws/messages'

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

/**
 * What the player needs for one channel, worked out by the backend.
 *
 * `license_path` is this application's own relay, never the licence server: the browser has
 * no route to that server and it sends no CORS headers. A clear channel comes back with empty
 * strings and the player is configured exactly as it was before DRM existed.
 */
export interface PlayerDrm {
  protected: boolean
  system: string
  system_label: string
  key_system: string
  license_path: string
  /**
   * 'relay' means `license_path` is this application's own relay endpoint; 'direct' means it
   * is the licence server's own URL and the browser posts to it, which a deployment chooses
   * in Settings -> DRM.
   */
  license_request_path: 'relay' | 'direct'
  configured: boolean
}

export interface RealtimeSession extends JobSummary {
  ws_url: string
  player_url: string
  player_drm: PlayerDrm
  player_metrics_note: string
}

/** The deployment's DRM configuration. Credentials are reported as set or not set, never read back. */
export interface DrmSettings {
  enabled: boolean
  license_url: string
  cpix_content_id: string
  keys_configured: boolean
  playback_configured: boolean
  /** Whether an evidence bundle carries the decrypted media beside the encrypted bytes. */
  decrypt_evidence: boolean
  /** Where the browser sends the licence challenge: through this analyzer, or straight on. */
  license_request_path: 'relay' | 'direct'
  configured: boolean
  endpoint: string
  client_cert_set: boolean
  client_key_set: boolean
  server_cert_set: boolean
}

/** One report file stored against an analysed channel. */
export interface ChannelReport {
  id: number
  format: string
  size_bytes: number
  created_at: string
  url: string
  exists: boolean
}

/** A finished analysis, read back from the database rather than the job manager. */
export interface AnalysedChannel extends JobSummary {
  reports: ChannelReport[]
}

/** One country the channel catalogue serves, and the data set its channels come from. */
export interface CatalogueCountry {
  code: string
  name: string
  group: string
}

/** One channel as the catalogue lists it. `playback_url` already has the routing marker off. */
export interface CatalogueChannel {
  number: string
  service_id: string
  country: string
  name: string
  /** Empty when the catalogue lists the channel with no CNTN_URI. */
  playback_url: string
  /** False when there is no playback URL, so there is nothing to analyse. */
  analysable: boolean
  extra: string[]
}

export interface CataloguePage {
  channels: CatalogueChannel[]
  page: number
  page_size: number
  total: number | null
  total_pages: number | null
  has_next: boolean
  has_previous: boolean
  url: string
  today: string
  country: CatalogueCountry
  environment: string
}

// --- CASCADA: the field rebuffering metric -----------------------------------

/** What the UI may know about the configured CASCADA session. Never the session itself. */
export interface CascadaSessionState {
  configured: boolean
  /** 'environment' for a host-configured session, 'stored' for one pasted in Settings. */
  source: string
  /** The last four characters of the sessionid, enough to tell two sessions apart. */
  masked: string
  last_validated_at: string | null
  valid: boolean | null
  detail: string
}

/** One minute of measurement. `value` is null for a minute CASCADA reported nothing for. */
export interface CascadaPoint {
  at: string
  value: number | null
}

/** Percentages of viewing time, computed from the current week's rows alone. */
export interface CascadaStats {
  average_pct: number | null
  max_pct: number | null
  max_at: string | null
  minutes_above: number
  minutes_counted: number
  minutes_missing: number
  percent_time_above: number | null
  previous_week_average_pct: number | null
  week_over_week_delta_pct: number | null
  threshold_pct: number
  above_threshold: boolean
}

export interface CascadaWindow {
  start: string
  end: string
  minutes: number
  days: number
}

/**
 * Where a rebuffering average came from. Realtime is one value per minute over the rolling
 * window; historical is one value per UTC day over the last seven complete days.
 */
export type DataSource = 'realtime' | 'historical'

/** A run of whole UTC days as the historical source states it. */
export interface CascadaDays {
  first_day: string
  last_day: string
  days: number
  label: string
}

/** One channel as the listing and the country report state it. */
export interface CascadaChannelRow extends CascadaStats {
  service_id: string
  channel_name: string
  country: string
  /** True when CASCADA returned fewer minutes than the window asked for. */
  truncated: boolean
  fetched_at: string
  cached: boolean
  source: DataSource
  /** 'minute' for realtime; 'day' for historical, where `minutes_*` count days. */
  granularity: 'minute' | 'day'
  // Historical only.
  days_with_data?: number
  days_expected?: number
  days_above?: number
  min_days?: number
  insufficient?: boolean
  requested_days?: CascadaDays
  returned_days?: CascadaDays
  provider_name?: string
  cascada_channel_name?: string
}

/** One channel with both series, as the Rebuffering Data modal reads it. */
export interface CascadaChannel extends CascadaChannelRow {
  window: CascadaWindow
  origin: CascadaPoint[]
  comparison: CascadaPoint[]
}

/**
 * One channel's playback errors, as the Error Data modal reads it.
 *
 * `errors` is a count per minute summed over every device, not a percentage, so the fields
 * are named for what they hold. `threshold_per_min` is null until one is set in Settings;
 * with none set no minute is marked and `above_threshold` is always false.
 */
export interface CascadaErrorChannel {
  metric: 'errors'
  /** The unit CASCADA stated for `errors`, which is 'times'. */
  unit: string
  service_id: string
  channel_name: string
  country: string
  average_per_min: number | null
  max_per_min: number | null
  max_at: string | null
  total: number | null
  minutes_counted: number
  minutes_missing: number
  previous_week_average_per_min: number | null
  week_over_week_delta_per_min: number | null
  week_over_week_change_pct: number | null
  threshold_per_min: number | null
  minutes_above: number
  percent_time_above: number | null
  above_threshold: boolean
  truncated: boolean
  fetched_at: string
  cached: boolean
  window: CascadaWindow
  origin: CascadaPoint[]
  comparison: CascadaPoint[]
}

export interface CascadaFailure {
  service_id: string
  channel_name: string
  reason: string
}

/** A channel a historical scan measured with too few days to judge. */
export interface CascadaInsufficient {
  service_id: string
  channel_name: string
  days_with_data: number
  days_expected: number
  average_pct: number | null
}

export interface CascadaScan {
  scan_id: string
  country: string
  source: DataSource
  /** The dates the averages cover, stated next to every average. */
  window_label: string
  insufficient: CascadaInsufficient[]
  insufficient_count: number
  no_provider: { service_id: string; channel_name: string; reason: string }[]
  ambiguous: { service_id: string; channel_name: string; chosen: string; candidates: string[] }[]
  status: string
  done: number
  total: number
  progress: number
  window: CascadaWindow
  threshold_pct: number
  above: CascadaChannelRow[]
  above_count: number
  measured_count: number
  below_count: number
  failures: CascadaFailure[]
  error: string | null
  complete: boolean
  started_at: string
  finished_at: string | null
}

/** What the cached channel → provider map holds. */
export interface CascadaProviders {
  loaded: boolean
  fetched_at?: string
  group?: string
  rows_read?: number
  rows_kept?: number
  channels?: number
  ambiguous_channels?: number
}

export interface CascadaChannelQuery {
  service_id: string
  channel_name: string
  country: string
}

function cascadaChannelQuery(params: CascadaChannelQuery & { refresh?: boolean }): string {
  const query = new URLSearchParams({
    service_id: params.service_id,
    channel_name: params.channel_name,
    country: params.country,
  })
  if (params.refresh) query.set('refresh', 'true')
  return query.toString()
}

export const endpoints = {
  health: () => api.get<Record<string, unknown>>('/health'),

  createRealtime: (body: unknown) => api.post<RealtimeSession>('/realtime/sessions', body),

  drmSettings: () => api.get<{ drm: DrmSettings; endpoint_default: string }>('/drm/settings'),
  saveDrmSettings: (body: unknown) => api.put<{ drm: DrmSettings }>('/drm/settings', body),
  drmProbe: (url: string) =>
    api.get<Record<string, unknown> & { player: PlayerDrm }>(
      `/drm/probe?url=${encodeURIComponent(url)}`,
    ),
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
  /** `stored_error` is set when the jobs from before this process started could not be read. */
  listAging: () =>
    api.get<{ jobs: JobSummary[]; stored_error?: string | null }>('/aging/jobs'),
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

  catalogueCountries: () =>
    api.get<{ countries: CatalogueCountry[]; environments: string[]; page_size: number }>(
      '/catalogue/countries',
    ),
  catalogueChannels: (params: { country: string; env: string; page: number; today?: string }) =>
    api.get<CataloguePage>(
      `/catalogue/channels?country=${encodeURIComponent(params.country)}` +
        `&env=${encodeURIComponent(params.env)}&page=${params.page}` +
        (params.today ? `&today=${encodeURIComponent(params.today)}` : ''),
    ),

  cascadaSession: () => api.get<CascadaSessionState>('/cascada/session'),
  saveCascadaSession: (body: { cookie_header?: string; sessionid?: string; csrftoken?: string }) =>
    api.put<CascadaSessionState>('/cascada/session', body),
  validateCascadaSession: () => api.post<CascadaSessionState>('/cascada/session/validate'),
  forgetCascadaSession: () => api.del<CascadaSessionState>('/cascada/session'),

  cascadaChannel: (params: CascadaChannelQuery & { refresh?: boolean }) =>
    api.get<CascadaChannel>(`/cascada/channel?${cascadaChannelQuery(params)}`),
  cascadaChannelReportUrl: (params: CascadaChannelQuery, fmt: 'csv' | 'xlsx') =>
    api.url(`/cascada/channel/report.${fmt}?${cascadaChannelQuery(params)}`),
  cascadaHistorical: (params: CascadaChannelQuery & { refresh?: boolean }) =>
    api.get<CascadaChannel>(`/cascada/channel/historical?${cascadaChannelQuery(params)}`),
  cascadaHistoricalReportUrl: (params: CascadaChannelQuery, fmt: 'csv' | 'xlsx') =>
    api.url(`/cascada/channel/historical/report.${fmt}?${cascadaChannelQuery(params)}`),
  cascadaProviders: () => api.get<CascadaProviders>('/cascada/providers'),
  refreshCascadaProviders: () => api.post<CascadaProviders>('/cascada/providers/refresh'),
  cascadaErrors: (params: CascadaChannelQuery & { refresh?: boolean }) =>
    api.get<CascadaErrorChannel>(`/cascada/channel/errors?${cascadaChannelQuery(params)}`),
  cascadaErrorsReportUrl: (params: CascadaChannelQuery, fmt: 'csv' | 'xlsx') =>
    api.url(`/cascada/channel/errors/report.${fmt}?${cascadaChannelQuery(params)}`),

  startCascadaScan: (body: { country: string; concurrency?: number; source?: DataSource }) =>
    api.post<CascadaScan>('/cascada/scans', body),
  readCascadaScan: (id: string) => api.get<CascadaScan>(`/cascada/scans/${id}`),
  cancelCascadaScan: (id: string) => api.del<CascadaScan>(`/cascada/scans/${id}`),
  cascadaScanReportUrl: (id: string, fmt: 'csv' | 'xlsx') =>
    api.url(`/cascada/scans/${id}/report.${fmt}`),

  batchSettings: () =>
    api.get<{
      settings: BatchSettings
      defaults: BatchSettings
      effective: Record<string, unknown>
    }>('/batch/settings'),
  saveBatchSettings: (body: BatchSettings) =>
    api.put<{ settings: BatchSettings; defaults: BatchSettings }>('/batch/settings', body),
  batchSchedules: () =>
    api.get<{ schedules: BatchSchedule[]; min_days_between_runs: number }>('/batch/schedules'),
  saveBatchSchedule: (body: Partial<BatchSchedule> & { country: string }) =>
    api.put<BatchSchedule>('/batch/schedules', body),
  deleteBatchSchedule: (country: string) =>
    api.del<{ deleted: boolean }>(`/batch/schedules/${encodeURIComponent(country)}`),

  batchEstimate: (country: string) =>
    api.get<BatchEstimate>(`/batch/estimate?country=${encodeURIComponent(country)}`),
  startBatch: (country: string, source: DataSource) =>
    api.post<Batch>('/batch/batches', { country, source }),
  listBatches: () => api.get<{ batches: Batch[]; count: number }>('/batch/batches'),
  readBatch: (id: string) => api.get<Batch>(`/batch/batches/${id}`),
  cancelBatch: (id: string) => api.del<Batch>(`/batch/batches/${id}`),
  rerunBatch: (id: string) => api.post<Batch>(`/batch/batches/${id}/rerun`),
  batchLog: (id: string) => api.get<{ lines: BatchLogLine[] }>(`/batch/batches/${id}/log`),
  batchLogUrl: (id: string) => api.url(`/batch/batches/${id}/log?download=true`),
  batchReportUrl: (id: string, fmt: 'csv' | 'xlsx') =>
    api.url(`/batch/batches/${id}/report.${fmt}`),
  batchColumns: () =>
    api.get<{ columns: string[]; window_days: number; window_label: string }>('/batch/columns'),

  agingSamples: (id: string, params: { from?: string; to?: string; maxPoints?: number } = {}) => {
    const query = new URLSearchParams()
    if (params.from) query.set('from', params.from)
    if (params.to) query.set('to', params.to)
    if (params.maxPoints) query.set('max_points', String(params.maxPoints))
    const suffix = query.toString()
    return api.get<JobSamples>(`/aging/jobs/${id}/samples${suffix ? `?${suffix}` : ''}`)
  },

  listChannels: () => api.get<{ channels: AnalysedChannel[]; count: number }>('/channels'),
  readChannel: (id: string) =>
    api.get<AnalysedChannel & Record<string, unknown>>(`/channels/${id}`),
  deleteChannel: (id: string) =>
    api.del<{ deleted: boolean; reports_removed: number }>(`/channels/${id}`),

  listReports: (query = '') => api.get<{ reports: Record<string, unknown>[] }>(`/reports${query}`),
  deleteReport: (id: number) => api.del<{ deleted: boolean }>(`/reports/${id}`),

  settings: () => api.get<Record<string, unknown>>('/settings'),
  saveSettings: (body: unknown) => api.put<Record<string, unknown>>('/settings', body),
  rules: () => api.get<RuleCatalogue>('/settings/rules'),
  /** Reassign one rule's severity. `null` restores the one the catalogue declares. */
  setRuleSeverity: (ruleId: string, severity: string | null) =>
    api.put<RuleCatalogue>(`/settings/rules/${encodeURIComponent(ruleId)}`, { severity }),
}

/** One declared check, with what it reports today beside what the catalogue declares. */
export interface CatalogueRule {
  id: string
  layer: string
  /** What the rule reports today: the override when there is one, else the declaration. */
  severity: string
  declared_severity: string
  overridden: boolean
  owner: string
  owner_label: string
  title: string
  root_cause: string
  fix: string
  rebuffer_impact: string
  reference: string
  thresholds: string[]
}

export interface RuleCatalogue {
  count: number
  severities: string[]
  overridden_count: number
  rules: CatalogueRule[]
}

// --- Automated batches ------------------------------------------------------

/** What a batch was configured with. A running batch keeps the values it started with. */
export interface BatchSettings {
  analysis_duration_minutes: number
  analysis_concurrency: number
  max_runtime_minutes: number
  aging_enabled: boolean
  aging_duration_days: number
  aging_max_concurrent: number
  correlation_tolerance_s: number
  spike_min_minutes: number
  retention_per_country: number
  /** Where a scheduled batch reads rebuffering from. A batch started by hand names its own. */
  scheduled_data_source: DataSource
}

export interface BatchItem {
  service_id: string
  channel_name: string
  country: string
  playback_url: string
  average_pct: number | null
  max_pct: number | null
  minutes_above: number
  status: string
  job_id: string | null
  aging_job_id: string | null
  error: string | null
  summary: string | null
  correlation: Record<string, unknown>
}

export interface Batch {
  id: string
  country: string
  /** 'manual' when an operator started it, 'scheduled' when the weekly firing did. */
  kind: string
  status: string
  phase: string
  running: boolean
  channels_listed: number
  channels_scanned: number
  channels_above: number
  channels_analysed: number
  channels_failed: number
  /** The measurement window the averages cover, which the report labels its column from. */
  window_from: string | null
  window_to: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null
  settings: Record<string, unknown>
  /** Which CASCADA source selected this batch's channels. */
  data_source: DataSource
  items?: BatchItem[]
}

export interface BatchSchedule {
  country: string
  enabled: boolean
  /** Monday is 0, matching the backend and `Date.getUTCDay()` shifted. */
  weekday: number
  hour_utc: number
  minute_utc: number
  overrides: Record<string, unknown>
  last_fired_at: string | null
  last_success_at: string | null
  last_reason: string | null
}

export interface BatchEstimate {
  country: string
  channels_listed: number
  analysis_duration_minutes: number
  analysis_concurrency: number
  max_runtime_minutes: number
  channels_within_runtime: number
  worst_case_runtime_minutes: number
  window_days: number
}

export interface BatchLogLine {
  at: string
  level: string
  message: string
}

/** One job's stored samples, already shaped for the Realtime chart components. */
export interface JobSamples {
  job_id: string
  range: {
    from: string | null
    to: string | null
    first_sample: string | null
    last_sample: string | null
  }
  counts: Record<string, number>
  max_points: number
  downsampled: boolean
  snapshots: PlaylistSnapshotData[]
  segments: SegmentResultData[]
  /** Declared BANDWIDTH per rung, in bit/s, or null for a rung that declares none. */
  declared: Record<string, number | null>
  player: Record<string, unknown>[]
  vpb: Record<string, { at: string; level_s: number; state: string }[]>
  /** Set when the run recorded no player samples, which an aging run never does. */
  player_note: string
}
