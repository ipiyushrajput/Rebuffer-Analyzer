/**
 * Alerts: where a run posts the first occurrence of each issue worth waking someone for.
 *
 * The webhook URL carries its own secret token, so — like the DRM credentials — it is held on
 * the analyzer and reported here as set or not set, never shown back.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { CardHeader, Field, InlineAlert, cx } from './ui'

interface AlertSettingsView {
  enabled: boolean
  webhook_set: boolean
  webhook_host: string
  min_severity: 'CRITICAL' | 'ERROR' | 'WARN'
  job_types: string[]
  public_origin: string
}

const JOB_TYPES = [
  { id: 'aging', label: 'Aging' },
  { id: 'realtime', label: 'Realtime' },
  { id: 'bulk', label: 'Bulk' },
  { id: 'batch', label: 'Automated batch' },
]

export function AlertsPanel() {
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: ['alert-settings'],
    queryFn: () => api.get<AlertSettingsView>('/settings/alerts'),
  })
  const [draft, setDraft] = useState<AlertSettingsView | null>(null)
  const [webhook, setWebhook] = useState('')
  useEffect(() => {
    if (data && !draft) setDraft(data)
  }, [data, draft])

  const save = useMutation({
    mutationFn: (clear: boolean) =>
      api.put<AlertSettingsView>('/settings/alerts', {
        enabled: draft?.enabled ?? false,
        webhook_url: clear ? '' : webhook.trim() || null,
        min_severity: draft?.min_severity ?? 'CRITICAL',
        job_types: draft?.job_types ?? ['aging'],
        public_origin: draft?.public_origin ?? '',
      }),
    onSuccess: (next) => {
      setWebhook('')
      setDraft(next)
      queryClient.setQueryData(['alert-settings'], next)
    },
  })
  const test = useMutation({
    mutationFn: () => api.post<{ delivered: boolean; problem: string }>('/settings/alerts/test'),
  })

  if (!draft) return null
  const toggleType = (id: string) =>
    setDraft({
      ...draft,
      job_types: draft.job_types.includes(id)
        ? draft.job_types.filter((item) => item !== id)
        : [...draft.job_types, id],
    })

  return (
    <div className="space-y-4">
      <CardHeader
        title="Alerts"
        actions={
          <span className={draft.enabled && draft.webhook_set ? 'chip-clean' : 'chip-neutral'}>
            {draft.enabled && draft.webhook_set ? 'On' : 'Off'}
          </span>
        }
      />
      <div className="grid gap-4 px-5 md:grid-cols-2">
        <Field label="Webhook URL (Teams, Slack or any JSON receiver)" htmlFor="alert-webhook">
          <input
            id="alert-webhook"
            className="input-mono"
            placeholder={draft.webhook_set ? `set — ${draft.webhook_host} — type to replace` : 'https://…'}
            value={webhook}
            onChange={(e) => setWebhook(e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </Field>
        <Field label="Alert on" htmlFor="alert-severity">
          <select
            id="alert-severity"
            className="input"
            value={draft.min_severity}
            onChange={(e) => setDraft({ ...draft, min_severity: e.target.value as AlertSettingsView['min_severity'] })}
          >
            <option value="CRITICAL">CRITICAL only</option>
            <option value="ERROR">ERROR and above</option>
            <option value="WARN">WARN and above</option>
          </select>
        </Field>
        <Field label="Link back to this deployment (optional)" htmlFor="alert-origin">
          <input
            id="alert-origin"
            className="input-mono"
            placeholder="http://107.109.131.68:8080"
            value={draft.public_origin}
            onChange={(e) => setDraft({ ...draft, public_origin: e.target.value })}
            spellCheck={false}
          />
        </Field>
        <fieldset>
          <legend className="field-label">Runs that alert</legend>
          <div className="flex flex-wrap gap-1.5">
            {JOB_TYPES.map((type) => (
              <button
                key={type.id}
                type="button"
                aria-pressed={draft.job_types.includes(type.id)}
                onClick={() => toggleType(type.id)}
                className={cx(
                  'rounded-tile border px-3 py-1.5 text-small font-semibold',
                  draft.job_types.includes(type.id)
                    ? 'border-brand-600 bg-brand-50 text-brand-600'
                    : 'border-surface-line bg-white text-ink-muted',
                )}
              >
                {type.label}
              </button>
            ))}
          </div>
        </fieldset>
      </div>
      <div className="flex flex-wrap items-center gap-2 px-5 pb-5">
        <label className="flex items-center gap-2 text-small text-ink-soft">
          <input
            type="checkbox"
            className="h-4 w-4 accent-brand-600"
            checked={draft.enabled}
            onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}
          />
          Send alerts
        </label>
        <span className="flex-1" />
        {draft.webhook_set && (
          <button type="button" className="btn-ghost btn-sm" onClick={() => save.mutate(true)}>
            Remove webhook
          </button>
        )}
        <button
          type="button"
          className="btn-ghost btn-sm"
          disabled={!draft.webhook_set || test.isPending}
          onClick={() => test.mutate()}
        >
          Send a test
        </button>
        <button type="button" className="btn-primary btn-sm" disabled={save.isPending} onClick={() => save.mutate(false)}>
          Save
        </button>
      </div>
      <div className="px-5 pb-5">
        {save.error && <InlineAlert tone="error">{String(save.error)}</InlineAlert>}
        {test.data && (
          <InlineAlert tone={test.data.delivered ? 'info' : 'error'}>
            {test.data.delivered ? 'The test message was delivered.' : `Not delivered: ${test.data.problem}`}
          </InlineAlert>
        )}
        <p className="text-micro text-ink-muted">
          One message per issue per run; acknowledged behaviours are never sent.
        </p>
      </div>
    </div>
  )
}
