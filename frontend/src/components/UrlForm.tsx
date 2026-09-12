/**
 * The URL and job-options form, shared by the Realtime and Aging tabs.
 *
 * The playback URL is required; origin, CDN and SSAI are optional and, when present, pin a
 * defect to the first layer it appears on.
 */

import { useState } from 'react'
import { CHECK_SETS, UA_PROFILES, VPB_MODES } from '../lib/constants'

export interface JobOptions {
  ua_profile: string
  check_sets: string[]
  renditions: string[] | null
  record_evidence: boolean
  clear_keys: Record<string, string>
  vpb_mode: string | null
}

export interface UrlFormValue {
  playback_url: string
  origin_url: string
  cdn_url: string
  ssai_url: string
  channel_name: string
  options: JobOptions
}

export const emptyForm = (recordEvidence = false): UrlFormValue => ({
  playback_url: '',
  origin_url: '',
  cdn_url: '',
  ssai_url: '',
  channel_name: '',
  options: {
    ua_profile: 'tizen5',
    check_sets: [],
    renditions: null,
    record_evidence: recordEvidence,
    clear_keys: {},
    vpb_mode: null,
  },
})

/** Strip empty comparison URLs and normalise the payload the API expects. */
export function toPayload(value: UrlFormValue): Record<string, unknown> {
  return {
    playback_url: value.playback_url.trim(),
    origin_url: value.origin_url.trim() || null,
    cdn_url: value.cdn_url.trim() || null,
    ssai_url: value.ssai_url.trim() || null,
    channel_name: value.channel_name.trim() || null,
    options: {
      ua_profile: value.options.ua_profile,
      check_sets: value.options.check_sets,
      renditions: value.options.renditions,
      record_evidence: value.options.record_evidence,
      clear_keys: value.options.clear_keys,
      vpb_mode: value.options.vpb_mode,
    },
  }
}

interface Props {
  value: UrlFormValue
  onChange: (value: UrlFormValue) => void
  disabled?: boolean
}

export function UrlForm({ value, onChange, disabled }: Props) {
  const [expanded, setExpanded] = useState(false)
  const [showOptions, setShowOptions] = useState(false)
  const [keyPair, setKeyPair] = useState({ kid: '', key: '' })

  const set = (patch: Partial<UrlFormValue>) => onChange({ ...value, ...patch })
  const setOptions = (patch: Partial<JobOptions>) =>
    onChange({ ...value, options: { ...value.options, ...patch } })

  const toggleCheckSet = (id: string) => {
    const next = value.options.check_sets.includes(id)
      ? value.options.check_sets.filter((item) => item !== id)
      : [...value.options.check_sets, id]
    setOptions({ check_sets: next })
  }

  return (
    <div className="space-y-3">
      <div className="grid gap-3 lg:grid-cols-[1fr_240px]">
        <div>
          <label className="label" htmlFor="playback-url">
            Playback URL
          </label>
          <input
            id="playback-url"
            className="input mono"
            placeholder="https://cdn.example/live/channel/master.m3u8?hdnts=…"
            value={value.playback_url}
            onChange={(e) => set({ playback_url: e.target.value })}
            disabled={disabled}
            spellCheck={false}
          />
        </div>
        <div>
          <label className="label" htmlFor="channel-name">
            Channel name
          </label>
          <input
            id="channel-name"
            className="input"
            placeholder="Samsung TV Plus — Channel 1"
            value={value.channel_name}
            onChange={(e) => set({ channel_name: e.target.value })}
            disabled={disabled}
          />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <button
          type="button"
          className="text-xs font-semibold text-brand-700 hover:underline"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? 'Hide' : 'Add'} origin, CDN and SSAI URLs for layer attribution
        </button>
        <button
          type="button"
          className="text-xs font-semibold text-brand-700 hover:underline"
          onClick={() => setShowOptions((v) => !v)}
        >
          {showOptions ? 'Hide' : 'Show'} job options
        </button>
      </div>

      {expanded && (
        <div className="grid gap-3 rounded-md border border-[var(--rba-line)] bg-slate-50 p-3 lg:grid-cols-3">
          {(
            [
              ['origin_url', 'Origin URL'],
              ['cdn_url', 'CDN URL'],
              ['ssai_url', 'SSAI (MediaTailor) URL'],
            ] as const
          ).map(([field, label]) => (
            <div key={field}>
              <label className="label" htmlFor={field}>
                {label}
              </label>
              <input
                id={field}
                className="input mono"
                value={value[field]}
                onChange={(e) => set({ [field]: e.target.value } as Partial<UrlFormValue>)}
                disabled={disabled}
                spellCheck={false}
              />
            </div>
          ))}
          <p className="text-xs text-[var(--rba-muted)] lg:col-span-3">
            With these URLs the same checks run on each layer and every defect is pinned to the
            first layer it appears on. Without them each defect is attributed from its own layer
            and the response headers that prove it.
          </p>
        </div>
      )}

      {showOptions && (
        <div className="space-y-3 rounded-md border border-[var(--rba-line)] bg-slate-50 p-3">
          <div className="grid gap-3 lg:grid-cols-3">
            <div>
              <label className="label" htmlFor="ua-profile">
                User-Agent profile
              </label>
              <select
                id="ua-profile"
                className="input"
                value={value.options.ua_profile}
                onChange={(e) => setOptions({ ua_profile: e.target.value })}
                disabled={disabled}
              >
                {UA_PROFILES.map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.label}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="label" htmlFor="vpb-mode">
                Virtual buffer sensitivity
              </label>
              <select
                id="vpb-mode"
                className="input"
                value={value.options.vpb_mode ?? ''}
                onChange={(e) => setOptions({ vpb_mode: e.target.value || null })}
                disabled={disabled}
              >
                <option value="">Use the configured default</option>
                {VPB_MODES.map((mode) => (
                  <option key={mode.id} value={mode.id}>
                    {mode.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex items-end">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={value.options.record_evidence}
                  onChange={(e) => setOptions({ record_evidence: e.target.checked })}
                  disabled={disabled}
                />
                Record evidence (segments and playlist snapshots around each incident)
              </label>
            </div>
          </div>

          <fieldset>
            <legend className="label">Check sets — baseline always runs</legend>
            <div className="flex flex-wrap gap-3">
              {CHECK_SETS.map((set_) => (
                <label key={set_.id} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={value.options.check_sets.includes(set_.id)}
                    onChange={() => toggleCheckSet(set_.id)}
                    disabled={disabled}
                  />
                  {set_.label}
                </label>
              ))}
            </div>
          </fieldset>

          <fieldset>
            <legend className="label">
              Clear keys for encrypted channels — held in memory, never stored or printed
            </legend>
            <div className="flex flex-wrap items-end gap-2">
              <input
                className="input mono max-w-[220px]"
                placeholder="KID"
                value={keyPair.kid}
                onChange={(e) => setKeyPair({ ...keyPair, kid: e.target.value })}
                disabled={disabled}
              />
              <input
                className="input mono max-w-[220px]"
                placeholder="KEY"
                type="password"
                value={keyPair.key}
                onChange={(e) => setKeyPair({ ...keyPair, key: e.target.value })}
                disabled={disabled}
              />
              <button
                type="button"
                className="btn-secondary"
                disabled={disabled || !keyPair.kid || !keyPair.key}
                onClick={() => {
                  setOptions({
                    clear_keys: { ...value.options.clear_keys, [keyPair.kid]: keyPair.key },
                  })
                  setKeyPair({ kid: '', key: '' })
                }}
              >
                Add key
              </button>
              {Object.keys(value.options.clear_keys).length > 0 && (
                <span className="chip border-slate-200 bg-white text-[var(--rba-muted)]">
                  {Object.keys(value.options.clear_keys).length} key pair(s) held
                </span>
              )}
            </div>
          </fieldset>
        </div>
      )}
    </div>
  )
}
