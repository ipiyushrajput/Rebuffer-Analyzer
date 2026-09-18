/**
 * Settings tab: thresholds, User-Agent profile, concurrency limits and retention.
 *
 * A change applies to jobs started afterwards. Running jobs keep the thresholds they started
 * with, so a report always states the values its findings were measured against. The save bar
 * stays on screen and counts the edits, so nothing is changed without being committed.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { endpoints } from '../api/client'
import { BatchSettingsPanel } from '../components/BatchSettingsPanel'
import { CascadaSession } from '../components/CascadaSession'
import { PageBody, PageHeader } from '../components/layout/PageHeader'
import {
  Card,
  CardHeader,
  EmptyState,
  Field,
  InlineAlert,
  SeverityChip,
  Tabs,
  cx,
} from '../components/ui'
import { IconCheck, IconSearch } from '../components/ui/icons'
import { UA_PROFILES, VPB_MODES, type Severity } from '../lib/constants'

interface SettingsPayload {
  thresholds: Record<string, number | string | boolean>
  defaults: Record<string, number | string | boolean>
  preferences: Record<string, number | boolean | string>
  ua_profiles: string[]
}

type GroupId =
  | 'profile'
  | 'rebuffering'
  | 'playlists'
  | 'delivery'
  | 'segments'
  | 'ladder'
  | 'incidents'
  | 'cascada'
  | 'batch'
  | 'rules'

const GROUPS: { id: GroupId; title: string; note: string; keys: string[] }[] = [
  {
    id: 'rebuffering',
    title: 'Rebuffering',
    note: 'The ratio that flags a channel, and how the virtual player models a Tizen device.',
    keys: [
      'rebuffer_ratio_threshold',
      'vpb_mode',
      'vpb_apply_byte_caps',
      'vpb_fhd_total_mb',
      'vpb_fhd_total_s',
      'vpb_uhd_total_mb',
      'vpb_uhd_total_s',
      'vpb_uhd_min_height',
      'vpb_startup_fraction',
      'vpb_resume_fraction',
      'vpb_low_watermark_fraction',
      'vpb_outage_threshold_s',
    ],
  },
  {
    id: 'playlists',
    title: 'Playlists and sequence numbers',
    note: 'Freshness, window length, and how far renditions may drift apart.',
    keys: [
      'stale_playlist_factor',
      'cross_variant_msn_error_spread',
      'cross_variant_dsn_tolerance',
      'min_live_window_multiple',
      'segment_extinf_max_ratio_to_td',
      'segment_extinf_absolute_max_s',
      'playlist_cache_max_age_factor',
      'master_repoll_interval_s',
    ],
  },
  {
    id: 'delivery',
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
    id: 'segments',
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
    id: 'ladder',
    title: 'Ladder',
    note: 'The shape a ladder must keep for ABR to recover on a constrained connection.',
    keys: ['lowest_rung_max_kbps', 'max_adjacent_rung_ratio', 'bandwidth_variation_tolerance'],
  },
  {
    id: 'incidents',
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
  {
    id: 'cascada',
    title: 'CASCADA',
    note: 'The field rebuffering metric: what counts as a rebuffering channel, the window measured, and how hard a country scan pushes.',
    keys: [
      'cascada_rebuffering_threshold_pct',
      'cascada_window_days',
      'cascada_scan_concurrency',
      'cascada_cache_ttl_minutes',
    ],
  },
]

const TABS: { id: GroupId; label: string }[] = [
  { id: 'profile', label: 'Fetch profile' },
  ...GROUPS.map((group) => ({ id: group.id, label: group.title })),
  { id: 'batch', label: 'Automated batches' },
  { id: 'rules', label: 'Rule catalogue' },
]

/** Threshold keys are the machine identifier; the sentence above them is the human one. */
function humanise(key: string): string {
  const text = key.replace(/_/g, ' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

export function SettingsTab() {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({ queryKey: ['settings'], queryFn: endpoints.settings })
  const settings = data as SettingsPayload | undefined

  const [tab, setTab] = useState<GroupId>('profile')
  const [thresholds, setThresholds] = useState<Record<string, number | string | boolean>>({})
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

  const edited = useMemo(() => {
    if (!settings) return []
    return Object.keys(thresholds).filter(
      (key) => String(thresholds[key]) !== String(settings.thresholds[key]),
    )
  }, [thresholds, settings])

  if (isLoading || !settings) {
    return (
      <>
        <PageHeader title="Settings" />
        <PageBody>
          <Card>
            <EmptyState
              title="Reading the stored configuration"
              detail="Thresholds are held in the database so every deployment reports the same numbers."
            />
          </Card>
        </PageBody>
      </>
    )
  }

  const offDefault = (key: string) => String(thresholds[key]) !== String(settings.defaults[key])
  const group = GROUPS.find((item) => item.id === tab)

  return (
    <div className="flex min-h-screen flex-col">
      <PageHeader
        title="Settings"
        subtitle="A change applies to jobs started afterwards; a running job keeps the thresholds it started with."
        status={
          edited.length > 0 ? (
            <span className="chip-violet">{edited.length} unsaved</span>
          ) : (
            <span className="chip-clean">Saved</span>
          )
        }
      />

      <PageBody className="flex-1">
        <Card>
          <div className="px-2 pt-1">
            <Tabs tabs={TABS} value={tab} onChange={setTab} />
          </div>

          {/* --- fetch profile ------------------------------------------------ */}
          <div className={tab === 'profile' ? '' : 'panel-hidden'}>
            <CardHeader
              title="Fetch profile and limits"
              subtitle="How the analyzer identifies itself, how much it runs at once, and how long raw samples are kept."
            />
            <div className="grid gap-4 px-5 pb-5 md:grid-cols-2 xl:grid-cols-4">
              <Field label="Default User-Agent profile" htmlFor="pref-ua">
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
              </Field>
              <Field label="Maximum concurrent jobs" htmlFor="pref-jobs">
                <input
                  id="pref-jobs"
                  className="input-mono"
                  type="number"
                  min={1}
                  max={200}
                  value={Number(preferences.max_concurrent_jobs ?? 20)}
                  onChange={(e) =>
                    setPreferences({ ...preferences, max_concurrent_jobs: Number(e.target.value) })
                  }
                />
              </Field>
              <Field label="Default bulk concurrency" htmlFor="pref-bulk">
                <input
                  id="pref-bulk"
                  className="input-mono"
                  type="number"
                  min={1}
                  max={50}
                  value={Number(preferences.bulk_default_concurrency ?? 5)}
                  onChange={(e) =>
                    setPreferences({
                      ...preferences,
                      bulk_default_concurrency: Number(e.target.value),
                    })
                  }
                />
              </Field>
              <Field
                label="Sample retention (days)"
                htmlFor="pref-retention"
                hint="Raw samples older than this are purged. Findings, incidents and reports are kept."
              >
                <input
                  id="pref-retention"
                  className="input-mono"
                  type="number"
                  min={1}
                  max={365}
                  value={Number(preferences.sample_retention_days ?? 30)}
                  onChange={(e) =>
                    setPreferences({ ...preferences, sample_retention_days: Number(e.target.value) })
                  }
                />
              </Field>
            </div>
          </div>

          {/* --- threshold groups --------------------------------------------- */}
          {group && (
            <div>
              <CardHeader title={group.title} subtitle={group.note} />
              <div className="grid gap-4 px-5 pb-5 md:grid-cols-2 xl:grid-cols-3">
                {group.keys
                  .filter((key) => key in thresholds)
                  .map((key) => (
                    <div key={key}>
                      <label className="field-label flex flex-wrap items-baseline gap-2" htmlFor={`threshold-${key}`}>
                        <span>{humanise(key)}</span>
                        {offDefault(key) && (
                          <span className="font-mono text-[10px] normal-case text-violet-500">
                            default {String(settings.defaults[key])}
                          </span>
                        )}
                      </label>
                      {typeof thresholds[key] === 'boolean' ? (
                        /* A threshold that is on or off, not a quantity. */
                        <label className="flex items-center gap-2 pt-1 text-small text-ink-soft">
                          <input
                            id={`threshold-${key}`}
                            type="checkbox"
                            className="h-4 w-4 accent-brand-600"
                            checked={Boolean(thresholds[key])}
                            onChange={(e) =>
                              setThresholds({ ...thresholds, [key]: e.target.checked })
                            }
                          />
                          {thresholds[key] ? 'Applied' : 'Not applied'}
                        </label>
                      ) : key === 'vpb_mode' ? (
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
                          className={cx('input-mono', offDefault(key) && 'border-violet-300')}
                          type="number"
                          step="any"
                          value={Number(thresholds[key])}
                          onChange={(e) =>
                            setThresholds({ ...thresholds, [key]: Number(e.target.value) })
                          }
                        />
                      )}
                      <p className="mt-1 font-mono text-[10px] text-ink-faint">{key}</p>
                    </div>
                  ))}
              </div>
            </div>
          )}

          {/* --- automated batches ---------------------------------------------
              Batch settings and the weekly firing save through their own endpoints, so the
              panel carries its own save rather than the bar below. */}
          <div className={tab === 'batch' ? 'p-4 pt-2' : 'panel-hidden'}>
            <BatchSettingsPanel />
          </div>

          {/* --- rule catalogue ------------------------------------------------ */}
          <div className={tab === 'rules' ? '' : 'panel-hidden'}>
            <RuleCatalogue />
          </div>
        </Card>

        {/* The session is not a threshold: it is stored and validated on its own, outside
            the save bar, because pasting one has to take effect immediately. */}
        {tab === 'cascada' && <CascadaSession />}

        {error && <InlineAlert tone="error">{error}</InlineAlert>}
      </PageBody>

      {/* --- the save bar ----------------------------------------------------
          Hidden on the batch tab: that panel saves through its own endpoints and carries its
          own button, and a second Save on the same screen saves something else. */}
      <div
        className={cx(
          'sticky bottom-0 z-20 border-t border-rail-border bg-rail px-6 py-3',
          tab === 'batch' && 'hidden',
        )}
      >
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            className="btn-primary"
            onClick={() => save.mutate()}
            disabled={save.isPending || edited.length === 0}
          >
            {save.isPending
              ? 'Saving'
              : edited.length === 0
                ? 'Save changes'
                : `Save ${edited.length} change${edited.length === 1 ? '' : 's'}`}
          </button>
          <button
            type="button"
            className="btn border-rail-border bg-rail-hover text-rail-text hover:bg-rail-active"
            onClick={() => setThresholds(settings.defaults)}
          >
            Restore defaults
          </button>
          {saved && (
            <span className="chip-clean">
              <IconCheck size={12} />
              Saved
            </span>
          )}
          <span className="ml-auto max-w-xl text-micro leading-snug text-rail-muted">
            Every report states the threshold values its findings were measured against.
          </span>
        </div>
      </div>
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
    <div>
      <CardHeader
        title="Rule catalogue"
        subtitle={`${data?.count ?? 0} rules, each with the threshold it reads and the party that owns the fix.`}
        actions={
          <label className="relative">
            <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-faint">
              <IconSearch size={14} />
            </span>
            <input
              className="input max-w-[260px] pl-8"
              placeholder="Filter by id, title, layer or owner"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </label>
        }
      />
      {visible.length === 0 ? (
        <EmptyState
          title="No rule matches this filter"
          detail="Clear the filter to see the whole catalogue."
        />
      ) : (
        <div className="max-h-[560px] overflow-auto">
          <table className="table table-hover">
            <thead className="sticky top-0">
              <tr>
                <th>ID</th>
                <th>Severity</th>
                <th>Owner</th>
                <th>Title, root cause and fix</th>
                <th>Thresholds</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((rule) => (
                <tr key={rule.id}>
                  <td className="font-mono text-micro text-ink">{rule.id}</td>
                  <td>
                    <SeverityChip severity={rule.severity as Severity} />
                  </td>
                  <td className="text-micro text-ink-soft">{rule.owner_label}</td>
                  <td className="max-w-[560px]">
                    <p className="text-body font-semibold text-ink">{rule.title}</p>
                    <p className="mt-0.5 text-micro text-ink-muted">{rule.root_cause}</p>
                    <p className="text-micro text-ink-muted">Fix: {rule.fix}</p>
                  </td>
                  <td className="font-mono text-micro text-ink-soft">
                    {rule.thresholds.join(', ') || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
