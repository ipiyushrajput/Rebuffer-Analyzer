/**
 * Settings tab: thresholds, User-Agent profile, concurrency limits and retention.
 *
 * A change applies to jobs started afterwards. Running jobs keep the thresholds they started
 * with, so a report always states the values its findings were measured against.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { endpoints } from '../api/client'
import { UA_PROFILES, VPB_MODES } from '../lib/constants'

interface SettingsPayload {
  thresholds: Record<string, number | string>
  defaults: Record<string, number | string>
  preferences: Record<string, number | boolean | string>
  ua_profiles: string[]
}

const GROUPS: { title: string; note: string; keys: string[] }[] = [
  {
    title: 'Rebuffering',
    note: 'The ratio that flags a channel, and how the virtual player models a Tizen device.',
    keys: [
      'rebuffer_ratio_threshold',
      'vpb_mode',
      'vpb_startup_buffer_td_multiple',
      'vpb_rebuffer_resume_td_multiple',
      'vpb_max_buffer_s',
      'vpb_outage_threshold_s',
    ],
  },
  {
    title: 'Playlists and sequence numbers',
    note: 'Freshness, window length, and how far renditions may drift apart.',
    keys: [
      'stale_playlist_factor',
      'cross_variant_msn_error_spread',
      'min_live_window_multiple',
      'segment_extinf_max_ratio_to_td',
      'segment_extinf_absolute_max_s',
      'playlist_cache_max_age_factor',
      'master_repoll_interval_s',
    ],
  },
  {
    title: 'Delivery',
    note: 'How slowly a segment may arrive before the buffer is at risk.',
    keys: [
      'download_ratio_warn',
      'download_ratio_error',
      'ttfb_budget_ms',
      'request_timeout_s',
      'bandwidth_overshoot_tolerance',
    ],
  },
  {
    title: 'Segments and bitstreams',
    note: 'Container, timing and audio/video tolerances.',
    keys: [
      'tiny_segment_bytes',
      'extinf_vs_actual_tolerance_s',
      'pts_gap_tolerance_ms',
      'av_skew_normal_ms',
      'av_skew_error_ms',
      'av_pts_delta_critical_ms',
    ],
  },
  {
    title: 'Ladder',
    note: 'The shape a ladder must keep for ABR to recover on a constrained connection.',
    keys: ['lowest_rung_max_kbps', 'max_adjacent_rung_ratio'],
  },
  {
    title: 'Incidents and sampling',
    note: 'Hysteresis on both edges of an incident, and how deeply each rung is sampled.',
    keys: [
      'incident_open_s',
      'incident_clear_s',
      'nth_segment_sampling_other_rungs',
      'ladder_sweep_interval_s',
      'evidence_window_s',
      'segment_retry_attempts',
      'segment_retry_backoff_s',
    ],
  },
]

export function SettingsTab() {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({ queryKey: ['settings'], queryFn: endpoints.settings })
  const settings = data as SettingsPayload | undefined

  const [thresholds, setThresholds] = useState<Record<string, number | string>>({})
  const [preferences, setPreferences] = useState<Record<string, number | boolean | string>>({})
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (settings) {
      setThresholds(settings.thresholds)
      setPreferences(settings.preferences)
    }
  }, [settings])

  const save = useMutation({
    mutationFn: () => endpoints.saveSettings({ thresholds, preferences }),
    onSuccess: () => {
      setError(null)
      setSaved(true)
      window.setTimeout(() => setSaved(false), 2500)
      void queryClient.invalidateQueries({ queryKey: ['settings'] })
    },
    onError: (err: Error) => setError(err.message),
  })

  if (isLoading || !settings) {
    return <p className="card px-4 py-6 text-sm text-[var(--rba-muted)]">Loading settings.</p>
  }

  const changed = (key: string) =>
    String(thresholds[key]) !== String(settings.defaults[key])

  return (
    <div className="space-y-4">
      <section className="card px-4 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            className="btn-primary"
            onClick={() => save.mutate()}
            disabled={save.isPending}
          >
            Save settings
          </button>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => setThresholds(settings.defaults)}
          >
            Restore defaults
          </button>
          {saved && <span className="chip border-pass bg-green-50 text-pass">Saved</span>}
          <span className="ml-auto text-xs text-[var(--rba-muted)]">
            A change applies to jobs started afterwards. A running job keeps the thresholds it
            started with, and every report states the values its findings were measured against.
          </span>
        </div>
        {error && (
          <p className="mt-3 rounded border border-critical bg-red-50 px-3 py-2 text-sm text-critical">
            {error}
          </p>
        )}
      </section>

      <section className="card">
        <div className="card-header">
          <h2 className="card-title">Fetch profile and limits</h2>
        </div>
        <div className="grid gap-3 px-4 py-3 md:grid-cols-4">
          <div>
            <label className="label" htmlFor="pref-ua">
              Default User-Agent profile
            </label>
            <select
              id="pref-ua"
              className="input"
              value={String(preferences.ua_profile ?? 'tizen5')}
              onChange={(e) => setPreferences({ ...preferences, ua_profile: e.target.value })}
            >
              {UA_PROFILES.filter((p) => settings.ua_profiles.includes(p.id)).map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="pref-jobs">
              Maximum concurrent jobs
            </label>
            <input
              id="pref-jobs"
              className="input"
              type="number"
              min={1}
              max={200}
              value={Number(preferences.max_concurrent_jobs ?? 20)}
              onChange={(e) =>
                setPreferences({ ...preferences, max_concurrent_jobs: Number(e.target.value) })
              }
            />
          </div>
          <div>
            <label className="label" htmlFor="pref-bulk">
              Default bulk concurrency
            </label>
            <input
              id="pref-bulk"
              className="input"
              type="number"
              min={1}
              max={50}
              value={Number(preferences.bulk_default_concurrency ?? 5)}
              onChange={(e) =>
                setPreferences({ ...preferences, bulk_default_concurrency: Number(e.target.value) })
              }
            />
          </div>
          <div>
            <label className="label" htmlFor="pref-retention">
              Sample retention (days)
            </label>
            <input
              id="pref-retention"
              className="input"
              type="number"
              min={1}
              max={365}
              value={Number(preferences.sample_retention_days ?? 30)}
              onChange={(e) =>
                setPreferences({ ...preferences, sample_retention_days: Number(e.target.value) })
              }
            />
            <p className="mt-1 text-xs text-[var(--rba-muted)]">
              Raw samples older than this are purged. Findings, incidents and reports are kept.
            </p>
          </div>
        </div>
      </section>

      {GROUPS.map((group) => (
        <section key={group.title} className="card">
          <div className="card-header">
            <div>
              <h2 className="card-title">{group.title}</h2>
              <p className="text-xs text-[var(--rba-muted)]">{group.note}</p>
            </div>
          </div>
          <div className="grid gap-3 px-4 py-3 md:grid-cols-3 lg:grid-cols-4">
            {group.keys
              .filter((key) => key in thresholds)
              .map((key) => (
                <div key={key}>
                  <label className="label" htmlFor={`threshold-${key}`}>
                    {key}
                    {changed(key) && (
                      <span className="ml-1 font-normal normal-case text-warn">
                        (default {String(settings.defaults[key])})
                      </span>
                    )}
                  </label>
                  {key === 'vpb_mode' ? (
                    <select
                      id={`threshold-${key}`}
                      className="input"
                      value={String(thresholds[key])}
                      onChange={(e) => setThresholds({ ...thresholds, [key]: e.target.value })}
                    >
                      {VPB_MODES.map((mode) => (
                        <option key={mode.id} value={mode.id}>
                          {mode.label}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      id={`threshold-${key}`}
                      className="input mono"
                      type="number"
                      step="any"
                      value={Number(thresholds[key])}
                      onChange={(e) =>
                        setThresholds({ ...thresholds, [key]: Number(e.target.value) })
                      }
                    />
                  )}
                </div>
              ))}
          </div>
        </section>
      ))}

      <RuleCatalogue />
    </div>
  )
}

function RuleCatalogue() {
  const { data } = useQuery({ queryKey: ['rules'], queryFn: endpoints.rules })
  const [filter, setFilter] = useState('')
  const rules = (data?.rules ?? []) as {
    id: string
    severity: string
    owner_label: string
    title: string
    root_cause: string
    fix: string
    layer: string
    thresholds: string[]
  }[]

  const visible = rules.filter((rule) =>
    filter
      ? `${rule.id} ${rule.title} ${rule.layer} ${rule.owner_label}`
          .toLowerCase()
          .includes(filter.toLowerCase())
      : true,
  )

  return (
    <section className="card">
      <div className="card-header">
        <h2 className="card-title">Rule catalogue — {data?.count ?? 0} rules</h2>
        <input
          className="input max-w-[240px]"
          placeholder="Filter by id, title, layer or owner"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>
      <div className="max-h-[520px] overflow-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="table-head">
              <th className="px-3 py-2">ID</th>
              <th className="px-3 py-2">Severity</th>
              <th className="px-3 py-2">Owner</th>
              <th className="px-3 py-2">Title, root cause and fix</th>
              <th className="px-3 py-2">Thresholds</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--rba-line)]">
            {visible.map((rule) => (
              <tr key={rule.id}>
                <td className="px-3 py-2 font-mono text-xs">{rule.id}</td>
                <td className="px-3 py-2 text-xs">{rule.severity}</td>
                <td className="px-3 py-2 text-xs">{rule.owner_label}</td>
                <td className="px-3 py-2">
                  <strong>{rule.title}</strong>
                  <div className="text-xs text-[var(--rba-muted)]">{rule.root_cause}</div>
                  <div className="text-xs text-[var(--rba-muted)]">Fix: {rule.fix}</div>
                </td>
                <td className="px-3 py-2 font-mono text-xs">
                  {rule.thresholds.join(', ') || '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
