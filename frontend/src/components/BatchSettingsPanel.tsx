/**
 * The batch configuration: what an automated batch does, and when it does it by itself.
 *
 * These are not analysis thresholds, so they are not on the Settings save bar. They have
 * their own endpoints, their own defaults and their own save, for the same reason the CASCADA
 * session does: a schedule is a row that a running loop reads every minute, and a half-edited
 * one would fire.
 *
 * A batch already running is untouched by anything saved here. It froze its settings when it
 * started, and its report states the values it used.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { endpoints, type BatchSchedule, type BatchSettings } from '../api/client'
import { Card, CardHeader, Field, InlineAlert, cx } from '../components/ui'
import { IconCheck, IconTrash } from '../components/ui/icons'
import { localDateTime, utcTitle } from '../lib/format'

/** Monday is 0, matching the backend and the `batch_schedules` row. */
const WEEKDAYS = [
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
  'Sunday',
]

/** Every editable batch setting, with the sentence that says what it governs. */
const FIELDS: {
  key: keyof BatchSettings
  label: string
  hint: string
  min: number
  max: number
}[] = [
  {
    key: 'analysis_duration_minutes',
    label: 'Analysis duration per channel (minutes)',
    hint: 'How long each above-threshold channel is analysed for.',
    min: 1,
    max: 240,
  },
  {
    key: 'analysis_concurrency',
    label: 'Channels analysed in parallel',
    hint: 'Raising this shortens the batch and raises the load on the analyzer host.',
    min: 1,
    max: 16,
  },
  {
    key: 'max_runtime_minutes',
    label: 'Maximum batch runtime (minutes)',
    hint: 'A batch that reaches this stops and files what it measured. The channels it did not reach are named in the report.',
    min: 10,
    max: 1440,
  },
  {
    key: 'aging_duration_days',
    label: 'Aging duration (days)',
    hint: 'How long each aging run started by a scheduled batch keeps measuring.',
    min: 1,
    max: 30,
  },
  {
    key: 'aging_max_concurrent',
    label: 'Aging runs held at once',
    hint: 'The worst channels by average take the slots. The rest are recorded as skipped, with the reason.',
    min: 1,
    max: 50,
  },
  {
    key: 'correlation_tolerance_s',
    label: 'Spike-to-event tolerance (seconds)',
    hint: 'How far apart a rebuffering spike and an aging event may be and still be the same moment.',
    min: 0,
    max: 3600,
  },
  {
    key: 'spike_min_minutes',
    label: 'Minutes to make a spike',
    hint: 'Consecutive minutes above the threshold before a stretch counts as a spike window.',
    min: 1,
    max: 120,
  },
  {
    key: 'retention_per_country',
    label: 'Batches kept per country',
    hint: 'Zero keeps every batch. Any other number deletes the oldest beyond it once a new batch finishes.',
    min: 0,
    max: 500,
  },
]

export function BatchSettingsPanel() {
  const queryClient = useQueryClient()
  const { data } = useQuery({ queryKey: ['batch-settings'], queryFn: endpoints.batchSettings })

  const [draft, setDraft] = useState<BatchSettings | null>(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (data) setDraft(data.settings)
  }, [data])

  const save = useMutation({
    mutationFn: (body: BatchSettings) => endpoints.saveBatchSettings(body),
    onSuccess: () => {
      setError(null)
      setSaved(true)
      window.setTimeout(() => setSaved(false), 2500)
      void queryClient.invalidateQueries({ queryKey: ['batch-settings'] })
    },
    onError: (err: Error) => setError(err.message),
  })

  if (!data || !draft) {
    return (
      <Card>
        <CardHeader
          title="Automated batches"
          subtitle="Reading the stored batch configuration."
        />
      </Card>
    )
  }

  const effective = data.effective as Record<string, number>
  const edited = FIELDS.some((field) => draft[field.key] !== data.settings[field.key])
  const agingEdited = draft.aging_enabled !== data.settings.aging_enabled
  const dirty = edited || agingEdited
  const offDefault = (key: keyof BatchSettings) => draft[key] !== data.defaults[key]

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Automated batches"
          subtitle="What a batch does once it is started, by an operator or by the weekly firing."
          actions={
            dirty ? <span className="chip-violet">unsaved</span> : <span className="chip-clean">Saved</span>
          }
        />

        <div className="px-5 pb-4">
          <InlineAlert tone="info">
            A batch judges a channel on its average over{' '}
            <span className="font-mono">{effective.window_days}</span> days against{' '}
            <span className="font-mono">{effective.threshold_pct}%</span>. Both live with the
            analysis thresholds, on the CASCADA tab, so there is one copy of each.
          </InlineAlert>
        </div>

        <div className="grid gap-4 px-5 pb-5 md:grid-cols-2 xl:grid-cols-3">
          {FIELDS.map((field) => (
            <Field
              key={field.key}
              label={field.label}
              htmlFor={`batch-${field.key}`}
              hint={field.hint}
            >
              <input
                id={`batch-${field.key}`}
                className={cx('input-mono', offDefault(field.key) && 'border-violet-300')}
                type="number"
                min={field.min}
                max={field.max}
                value={Number(draft[field.key])}
                onChange={(e) =>
                  setDraft({ ...draft, [field.key]: Number(e.target.value) })
                }
              />
              <p className="mt-1 font-mono text-[10px] text-ink-faint">
                {field.key}
                {offDefault(field.key) && ` · default ${String(data.defaults[field.key])}`}
              </p>
            </Field>
          ))}

          <Field
            label="Aging after a scheduled batch"
            htmlFor="batch-aging-enabled"
            hint="A manual batch never starts aging. This governs the scheduled firing only."
          >
            <label className="flex items-center gap-2 pt-1 text-small text-ink-soft">
              <input
                id="batch-aging-enabled"
                type="checkbox"
                className="h-4 w-4 accent-brand-600"
                checked={draft.aging_enabled}
                onChange={(e) => setDraft({ ...draft, aging_enabled: e.target.checked })}
              />
              {draft.aging_enabled
                ? 'Above-threshold channels age for the week that follows'
                : 'No aging run is started'}
            </label>
            <p className="mt-1 font-mono text-[10px] text-ink-faint">aging_enabled</p>
          </Field>
        </div>

        <div className="flex flex-wrap items-center gap-3 card-divider px-5 py-3">
          <button
            type="button"
            className="btn-primary"
            onClick={() => save.mutate(draft)}
            disabled={save.isPending || !dirty}
          >
            {save.isPending ? 'Saving' : 'Save batch settings'}
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => setDraft(data.defaults)}
            disabled={save.isPending}
          >
            Restore defaults
          </button>
          {saved && (
            <span className="chip-clean">
              <IconCheck size={12} />
              Saved
            </span>
          )}
          <span className="ml-auto max-w-lg text-micro leading-snug text-ink-muted">
            A batch that is already running keeps the settings it started with, and its report
            states them.
          </span>
        </div>

        {error && (
          <div className="px-5 pb-4">
            <InlineAlert tone="error">{error}</InlineAlert>
          </div>
        )}
      </Card>

      <ScheduleTable />
    </div>
  )
}

/** The weekly firings: one row per country, each one a `batch_schedules` row. */
function ScheduleTable() {
  const queryClient = useQueryClient()
  const { data } = useQuery({ queryKey: ['batch-schedules'], queryFn: endpoints.batchSchedules })
  const { data: catalogue } = useQuery({
    queryKey: ['catalogue-countries'],
    queryFn: endpoints.catalogueCountries,
  })

  const [adding, setAdding] = useState('')
  const [error, setError] = useState<string | null>(null)

  const invalidate = () => {
    setError(null)
    void queryClient.invalidateQueries({ queryKey: ['batch-schedules'] })
  }

  const save = useMutation({
    mutationFn: (body: Partial<BatchSchedule> & { country: string }) =>
      endpoints.saveBatchSchedule(body),
    onSuccess: invalidate,
    onError: (err: Error) => setError(err.message),
  })
  const remove = useMutation({
    mutationFn: (country: string) => endpoints.deleteBatchSchedule(country),
    onSuccess: invalidate,
    onError: (err: Error) => setError(err.message),
  })

  const schedules = data?.schedules ?? []
  const gap = data?.min_days_between_runs ?? 7
  const countries = catalogue?.countries ?? []
  const configured = new Set(schedules.map((row) => row.country))
  const available = countries.filter((country) => !configured.has(country.code))

  const patch = (row: BatchSchedule, change: Partial<BatchSchedule>) =>
    save.mutate({
      country: row.country,
      enabled: row.enabled,
      weekday: row.weekday,
      hour_utc: row.hour_utc,
      minute_utc: row.minute_utc,
      overrides: row.overrides,
      ...change,
    })

  return (
    <Card>
      <CardHeader
        title="Weekly firing"
        subtitle={`A country fires on its weekday and time, and is skipped with a recorded reason when a batch for it is still running or fewer than ${gap} days have passed since its last success.`}
        actions={
          available.length > 0 && (
            <div className="flex items-center gap-2">
              <select
                className="input max-w-[220px]"
                aria-label="Country to schedule"
                value={adding}
                onChange={(e) => setAdding(e.target.value)}
              >
                <option value="">Add a country…</option>
                {available.map((country) => (
                  <option key={country.code} value={country.code}>
                    {country.name} ({country.code})
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn"
                disabled={!adding || save.isPending}
                onClick={() => {
                  save.mutate({
                    country: adding,
                    enabled: true,
                    weekday: 0,
                    hour_utc: 2,
                    minute_utc: 0,
                  })
                  setAdding('')
                }}
              >
                Add
              </button>
            </div>
          )
        }
      />

      {error && (
        <div className="px-5 pb-3">
          <InlineAlert tone="error">{error}</InlineAlert>
        </div>
      )}

      {schedules.length === 0 ? (
        <p className="px-5 pb-5 text-small text-ink-muted">
          No country fires by itself. Add one above, or start a batch by hand on the Automated
          Batch tab.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="table">
            <thead>
              <tr>
                <th>Country</th>
                <th>Enabled</th>
                <th>Weekday</th>
                <th>Time (UTC)</th>
                <th>Last fired</th>
                <th>Last success</th>
                <th>Last reason</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {schedules.map((row) => (
                <tr key={row.country}>
                  <td className="font-mono text-micro text-ink">{row.country}</td>
                  <td>
                    <label className="flex items-center gap-2 text-micro text-ink-soft">
                      <input
                        type="checkbox"
                        className="h-4 w-4 accent-brand-600"
                        checked={row.enabled}
                        aria-label={`Weekly firing for ${row.country}`}
                        onChange={(e) => patch(row, { enabled: e.target.checked })}
                      />
                      {row.enabled ? 'On' : 'Off'}
                    </label>
                  </td>
                  <td>
                    <select
                      className="input max-w-[150px]"
                      aria-label={`Weekday for ${row.country}`}
                      value={row.weekday}
                      onChange={(e) => patch(row, { weekday: Number(e.target.value) })}
                    >
                      {WEEKDAYS.map((day, index) => (
                        <option key={day} value={index}>
                          {day}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <div className="flex items-center gap-1">
                      <input
                        className="input-mono w-[68px]"
                        type="number"
                        min={0}
                        max={23}
                        aria-label={`Hour for ${row.country}`}
                        value={row.hour_utc}
                        onChange={(e) => patch(row, { hour_utc: Number(e.target.value) })}
                      />
                      <span className="text-ink-faint">:</span>
                      <input
                        className="input-mono w-[68px]"
                        type="number"
                        min={0}
                        max={59}
                        aria-label={`Minute for ${row.country}`}
                        value={row.minute_utc}
                        onChange={(e) => patch(row, { minute_utc: Number(e.target.value) })}
                      />
                    </div>
                  </td>
                  <td
                    className="font-mono text-micro text-ink-soft"
                    title={utcTitle(row.last_fired_at)}
                  >
                    {row.last_fired_at ? localDateTime(row.last_fired_at) : '—'}
                  </td>
                  <td
                    className="font-mono text-micro text-ink-soft"
                    title={utcTitle(row.last_success_at)}
                  >
                    {row.last_success_at ? localDateTime(row.last_success_at) : '—'}
                  </td>
                  <td className="max-w-[320px] text-micro text-ink-muted">
                    {row.last_reason ?? '—'}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="btn-quiet text-pink-600 hover:bg-pink-50"
                      aria-label={`Remove the schedule for ${row.country}`}
                      disabled={remove.isPending}
                      onClick={() => remove.mutate(row.country)}
                    >
                      <IconTrash size={14} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}
