/**
 * The CASCADA session panel.
 *
 * CASCADA sits behind the corporate identity provider and that provider requires MFA, so the
 * analyzer cannot sign itself in. It carries an operator's session instead, pasted here once.
 *
 * The browser cannot hand its own CASCADA session over: those cookies belong to
 * cascada.samsungcloud.tv, so this page never receives them and cannot read them, and Django
 * marks `sessionid` HttpOnly. Copying the Cookie header out of devtools is the one route, and
 * this panel takes the whole header so it is one copy and one paste.
 *
 * What is typed here goes to the backend and is never sent back: the panel shows the last four
 * characters, when the session was last proven to work, and what happened the last time it was
 * used.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, endpoints, type CascadaSessionState } from '../api/client'
import { Card, CardHeader, Field, InlineAlert } from './ui'
import { IconCheck } from './ui/icons'

function message(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : String(error)
}

function stateTone(state: CascadaSessionState): 'clean' | 'warn' | 'error' {
  if (!state.configured) return 'error'
  return state.valid === false ? 'warn' : 'clean'
}

function stateLabel(state: CascadaSessionState): string {
  if (!state.configured) return 'No session'
  if (state.valid === false) return 'Session rejected'
  if (state.valid === true) return 'Session valid'
  return 'Not yet validated'
}

export function CascadaSession() {
  const queryClient = useQueryClient()
  const [pasted, setPasted] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const session = useQuery({ queryKey: ['cascada-session'], queryFn: endpoints.cascadaSession })

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['cascada-session'] })
  }

  const save = useMutation({
    mutationFn: () => endpoints.saveCascadaSession({ cookie_header: pasted }),
    onSuccess: () => {
      setError(null)
      setSaved(true)
      setPasted('')
      window.setTimeout(() => setSaved(false), 2500)
      refresh()
    },
    onError: (err: Error) => setError(message(err)),
  })

  const validate = useMutation({
    mutationFn: endpoints.validateCascadaSession,
    onSuccess: () => {
      setError(null)
      refresh()
    },
    onError: (err: Error) => setError(message(err)),
  })

  const forget = useMutation({
    mutationFn: endpoints.forgetCascadaSession,
    onSuccess: () => {
      setError(null)
      refresh()
    },
    onError: (err: Error) => setError(message(err)),
  })

  const state = session.data
  const fromHost = state?.source === 'environment'

  return (
    <Card>
      <CardHeader
        title="CASCADA session"
        subtitle="The field rebuffering metric is read with an operator's CASCADA session. It is held on the analyzer and never sent back to this page."
        actions={
          state && (
            <span
              className={
                stateTone(state) === 'clean'
                  ? 'chip-clean'
                  : stateTone(state) === 'warn'
                    ? 'chip-violet'
                    : 'chip-pink'
              }
            >
              {stateLabel(state)}
            </span>
          )
        }
      />

      <div className="space-y-4 px-5 pb-5">
        {state && (
          <dl className="grid gap-3 sm:grid-cols-3">
            <div>
              <dt className="field-label">Source</dt>
              <dd className="font-mono text-small text-ink-soft">
                {fromHost ? "The analyzer host's environment" : 'Pasted in this panel'}
              </dd>
            </div>
            <div>
              <dt className="field-label">Session</dt>
              <dd className="font-mono text-small text-ink-soft">
                {state.masked || 'none configured'}
              </dd>
            </div>
            <div>
              <dt className="field-label">Last checked</dt>
              <dd className="font-mono text-small text-ink-soft">
                {state.last_validated_at ?? 'never'}
              </dd>
            </div>
          </dl>
        )}

        {state?.detail && (
          <InlineAlert tone={state.valid === false ? 'warn' : 'info'}>{state.detail}</InlineAlert>
        )}

        {fromHost ? (
          <InlineAlert tone="info">
            This host carries its own CASCADA session in its environment, so nothing is pasted
            here. Clear CASCADA_SESSIONID in backend/.env to paste one instead.
          </InlineAlert>
        ) : (
          <>
            <Field
              label="Paste a CASCADA session"
              htmlFor="cascada-cookie"
              hint="In a signed-in CASCADA tab, open devtools, pick any request to cascada.samsungcloud.tv, and copy its Cookie request header. Paste the whole line — only sessionid, csrftoken and messages are kept."
            >
              <textarea
                id="cascada-cookie"
                className="input-mono min-h-24"
                placeholder="Cookie: sessionid=…; csrftoken=…"
                value={pasted}
                onChange={(e) => setPasted(e.target.value)}
                spellCheck={false}
                autoComplete="off"
              />
            </Field>

            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="btn-primary"
                disabled={!pasted.trim() || save.isPending}
                onClick={() => save.mutate()}
              >
                {save.isPending ? 'Checking against CASCADA' : 'Save and validate'}
              </button>
              <button
                type="button"
                className="btn-ghost btn-sm"
                disabled={!state?.configured || validate.isPending}
                onClick={() => validate.mutate()}
              >
                {validate.isPending ? 'Checking' : 'Validate now'}
              </button>
              <button
                type="button"
                className="btn-ghost btn-sm"
                disabled={!state?.configured || forget.isPending}
                title="Remove the stored session from the analyzer"
                onClick={() => forget.mutate()}
              >
                {forget.isPending ? 'Removing' : 'Forget session'}
              </button>
              {saved && (
                <span className="inline-flex items-center gap-1 text-small font-semibold text-clean-600">
                  <IconCheck size={14} />
                  Stored
                </span>
              )}
            </div>
          </>
        )}

        {error && <InlineAlert tone="error">{error}</InlineAlert>}
        {session.isError && <InlineAlert tone="error">{message(session.error)}</InlineAlert>}
      </div>
    </Card>
  )
}

/** The banner every CASCADA surface shows when the session is the thing that failed. */
export function CascadaSessionBanner({ detail }: { detail: string }) {
  return (
    <InlineAlert tone="error">
      <span className="font-semibold">CASCADA session expired.</span> {detail} Open Settings →
      CASCADA and paste a fresh session, then run this again.
    </InlineAlert>
  )
}
