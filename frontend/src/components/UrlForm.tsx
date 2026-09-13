/**
 * The URL and job-options form, shared by the Realtime and Aging tabs.
 *
 * The playback URL is required; origin, CDN and SSAI are optional and, when present, pin a
 * defect to the first layer it appears on. Everything beyond the URL is folded away, so the
 * common path is one field and one button.
 */

import { useState } from 'react'
import { CHECK_SETS, UA_PROFILES, VPB_MODES } from '../lib/constants'
import { Field, cx } from './ui'

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

function Toggle({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean
  onChange: (value: boolean) => void
  label: string
  disabled?: boolean
}) {
  return (
    <label
      className={cx(
        'flex cursor-pointer items-start gap-2.5 rounded-tile border px-3 py-2 text-small transition-colors duration-150',
        checked
          ? 'border-brand-200 bg-brand-50 text-brand-600'
          : 'border-surface-line bg-white text-ink-soft hover:border-surface-lineStrong',
        disabled && 'cursor-not-allowed opacity-50',
      )}
    >
      <input
        type="checkbox"
        className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-brand-600"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        disabled={disabled}
      />
      <span className="leading-snug">{label}</span>
    </label>
  )
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
      <div className="grid gap-3 lg:grid-cols-[1fr_260px]">
        <Field label="Playback URL" htmlFor="playback-url">
          <input
            id="playback-url"
            className="input-mono"
            placeholder="https://cdn.example/live/channel/master.m3u8?hdnts=…"
            value={value.playback_url}
            onChange={(e) => set({ playback_url: e.target.value })}
            disabled={disabled}
            spellCheck={false}
          />
        </Field>
        <Field label="Channel name" htmlFor="channel-name">
          <input
            id="channel-name"
            className="input"
            placeholder="Samsung TV Plus — Channel 1"
            value={value.channel_name}
            onChange={(e) => set({ channel_name: e.target.value })}
            disabled={disabled}
          />
        </Field>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className="btn-quiet" onClick={() => setExpanded((v) => !v)}>
          {expanded ? 'Hide' : 'Add'} origin, CDN and SSAI URLs
        </button>
        <button type="button" className="btn-quiet" onClick={() => setShowOptions((v) => !v)}>
          {showOptions ? 'Hide' : 'Show'} job options
        </button>
        {value.options.check_sets.length > 0 && (
          <span className="chip-blue">{value.options.check_sets.length} extra check sets</span>
        )}
        {Object.keys(value.options.clear_keys).length > 0 && (
          <span className="chip-violet">
            {Object.keys(value.options.clear_keys).length} key pairs held
          </span>
        )}
      </div>

      {expanded && (
        <div className="grid gap-3 rounded-tile border border-surface-line bg-surface-raised p-4 lg:grid-cols-3">
          {(
            [
              ['origin_url', 'Origin URL'],
              ['cdn_url', 'CDN URL'],
              ['ssai_url', 'SSAI (MediaTailor) URL'],
            ] as const
          ).map(([field, label]) => (
            <Field key={field} label={label} htmlFor={field}>
              <input
                id={field}
                className="input-mono"
                value={value[field]}
                onChange={(e) => set({ [field]: e.target.value } as Partial<UrlFormValue>)}
                disabled={disabled}
                spellCheck={false}
              />
            </Field>
          ))}
          <p className="text-micro leading-snug text-ink-muted lg:col-span-3">
            With these URLs the same checks run on each layer and every defect is pinned to the
            first layer it appears on. Without them each defect is attributed from its own layer
            and the response headers that prove it.
          </p>
        </div>
      )}

      {showOptions && (
        <div className="space-y-4 rounded-tile border border-surface-line bg-surface-raised p-4">
          <div className="grid gap-3 lg:grid-cols-3">
            <Field label="User-Agent profile" htmlFor="ua-profile">
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
            </Field>
            <Field label="Virtual buffer sensitivity" htmlFor="vpb-mode">
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
            </Field>
            <div className="flex items-end">
              <Toggle
                checked={value.options.record_evidence}
                onChange={(record_evidence) => setOptions({ record_evidence })}
                disabled={disabled}
                label="Record evidence around each incident"
              />
            </div>
          </div>

          <fieldset>
            <legend className="field-label">Check sets — the baseline always runs</legend>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
              {CHECK_SETS.map((set_) => (
                <Toggle
                  key={set_.id}
                  checked={value.options.check_sets.includes(set_.id)}
                  onChange={() => toggleCheckSet(set_.id)}
                  disabled={disabled}
                  label={set_.label}
                />
              ))}
            </div>
          </fieldset>

          <fieldset>
            <legend className="field-label">
              Clear keys for encrypted channels — held in memory, never stored or printed
            </legend>
            <div className="flex flex-wrap items-center gap-2">
              <input
                className="input-mono max-w-[220px]"
                placeholder="KID"
                value={keyPair.kid}
                onChange={(e) => setKeyPair({ ...keyPair, kid: e.target.value })}
                disabled={disabled}
              />
              <input
                className="input-mono max-w-[220px]"
                placeholder="KEY"
                type="password"
                value={keyPair.key}
                onChange={(e) => setKeyPair({ ...keyPair, key: e.target.value })}
                disabled={disabled}
              />
              <button
                type="button"
                className="btn-ghost"
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
            </div>
          </fieldset>
        </div>
      )}
    </div>
  )
}
