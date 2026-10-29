/**
 * What the engineer reading a finding says about it.
 *
 * A verdict — confirmed or false positive — is summed per rule into its precision in the rule
 * catalogue, so the rules that raise noise are named by the people who read them. An
 * acknowledgement accepts a channel's known behaviour: the rule still fires and is still shown,
 * marked, and is no longer alerted on or escalated.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { endpoints } from '../api/client'
import type { FindingData } from '../ws/messages'
import { Card, CardHeader, InlineAlert, SegmentedControl, cx } from './ui'

export function ReviewPanel({ jobId, finding }: { jobId: string; finding: FindingData }) {
  const queryClient = useQueryClient()
  const [note, setNote] = useState('')
  const [ackNote, setAckNote] = useState('')
  const [scope, setScope] = useState<'rendition' | 'every'>('rendition')
  const [expires, setExpires] = useState<number | null>(30)

  const feedback = useQuery({
    queryKey: ['feedback', jobId],
    queryFn: () => endpoints.jobFeedback(jobId),
  })
  const acks = useQuery({
    queryKey: ['acknowledged', jobId],
    queryFn: () => endpoints.jobAcknowledged(jobId),
  })

  const mine = feedback.data?.feedback.find(
    (item) =>
      item.rule_id === finding.rule_id &&
      item.stream_layer === finding.stream_layer &&
      (item.variant ?? null) === (finding.variant ?? null),
  )
  const covering = acks.data?.acknowledgements.filter(
    (item) => item.rule_id === finding.rule_id && (item.variant === null || item.variant === finding.variant),
  )

  const verdict = useMutation({
    mutationFn: (value: 'confirmed' | 'false_positive') =>
      endpoints.giveFeedback(jobId, {
        rule_id: finding.rule_id,
        stream_layer: finding.stream_layer,
        variant: finding.variant,
        verdict: value,
        note: note.trim() || undefined,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['feedback', jobId] })
      void queryClient.invalidateQueries({ queryKey: ['rule-precision'] })
    },
  })
  const acknowledge = useMutation({
    mutationFn: () =>
      endpoints.acknowledge({
        job_id: jobId,
        rule_id: finding.rule_id,
        variant: scope === 'rendition' ? finding.variant : null,
        note: ackNote.trim(),
        expires_days: expires,
      }),
    onSuccess: () => {
      setAckNote('')
      void queryClient.invalidateQueries({ queryKey: ['acknowledged', jobId] })
    },
  })
  const remove = useMutation({
    mutationFn: (id: number) => endpoints.unacknowledge(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['acknowledged', jobId] }),
  })

  return (
    <Card>
      <CardHeader title="Review" />
      <div className="space-y-4 px-5 pb-5">
        <div className="space-y-2">
          <p className="label">Is this finding real?</p>
          <div className="flex flex-wrap gap-1.5">
            <button
              type="button"
              className={cx('btn-ghost btn-sm', mine?.verdict === 'confirmed' && 'border-clean-500 text-clean-600')}
              aria-pressed={mine?.verdict === 'confirmed'}
              disabled={verdict.isPending}
              onClick={() => verdict.mutate('confirmed')}
            >
              Confirmed
            </button>
            <button
              type="button"
              className={cx('btn-ghost btn-sm', mine?.verdict === 'false_positive' && 'border-pink-500 text-pink-600')}
              aria-pressed={mine?.verdict === 'false_positive'}
              disabled={verdict.isPending}
              onClick={() => verdict.mutate('false_positive')}
            >
              False positive
            </button>
          </div>
          <input
            className="input text-small"
            placeholder="Note (optional)"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            maxLength={2000}
          />
          {mine && (
            <p className="text-micro text-ink-muted">
              Marked {mine.verdict === 'confirmed' ? 'confirmed' : 'a false positive'}
              {mine.note ? ` — ${mine.note}` : ''}
            </p>
          )}
          {verdict.error && <InlineAlert tone="error">{String(verdict.error)}</InlineAlert>}
        </div>

        <div className="space-y-2 border-t border-surface-line pt-4">
          <p className="label">Known behaviour of this channel?</p>
          {covering && covering.length > 0 ? (
            covering.map((item) => (
              <div key={item.id} className="flex items-start justify-between gap-2">
                <p className="text-small text-ink-soft">
                  <span className="chip-violet">Acknowledged</span>{' '}
                  {item.variant ? `on ${item.variant}` : 'on every rendition'} — {item.note}
                  {item.expires_at && (
                    <span className="text-micro text-ink-muted"> (until {item.expires_at.slice(0, 10)})</span>
                  )}
                </p>
                <button
                  type="button"
                  className="btn-ghost btn-sm"
                  onClick={() => remove.mutate(item.id)}
                  disabled={remove.isPending}
                >
                  Remove
                </button>
              </div>
            ))
          ) : (
            <>
              <SegmentedControl
                options={[
                  { value: 'rendition', label: finding.variant ? `This rendition` : 'This finding' },
                  { value: 'every', label: 'Every rendition' },
                ]}
                value={scope}
                onChange={(next) => setScope(next as 'rendition' | 'every')}
                ariaLabel="Acknowledgement scope"
              />
              <input
                className="input text-small"
                placeholder="Why it is accepted (required)"
                value={ackNote}
                onChange={(e) => setAckNote(e.target.value)}
                maxLength={2000}
              />
              <div className="flex flex-wrap items-center gap-2">
                <select
                  className="input w-auto py-1 text-small"
                  value={expires ?? ''}
                  onChange={(e) => setExpires(e.target.value ? Number(e.target.value) : null)}
                  aria-label="Expires"
                >
                  <option value="7">for 7 days</option>
                  <option value="30">for 30 days</option>
                  <option value="90">for 90 days</option>
                  <option value="">until removed</option>
                </select>
                <button
                  type="button"
                  className="btn-ghost btn-sm"
                  disabled={ackNote.trim().length < 3 || acknowledge.isPending}
                  onClick={() => acknowledge.mutate()}
                >
                  Acknowledge
                </button>
              </div>
              <p className="text-micro text-ink-muted">
                Still measured and shown; no longer alerted on or escalated.
              </p>
            </>
          )}
          {acknowledge.error && <InlineAlert tone="error">{String(acknowledge.error)}</InlineAlert>}
        </div>
      </div>
    </Card>
  )
}
