import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, CalendarDays, Link2, Mail, Search } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Badge, Card, Meter, Spinner } from '@/components/ui/primitives';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import { ApiError, type CalendarEvent, type Email, type MatchRun, type Meeting } from '@/types/api';

function bucket(score: number | null): { label: string; variant: 'success' | 'warning' | 'neutral' } {
  if (score === null) return { label: 'Unranked', variant: 'neutral' };
  if (score >= 0.7) return { label: 'Strong', variant: 'success' };
  if (score >= 0.4) return { label: 'Possible', variant: 'warning' };
  return { label: 'Weak', variant: 'neutral' };
}

function CandidateRow({
  checked,
  onToggle,
  title,
  meta,
  snippet,
  score,
  reason,
  radio,
}: {
  checked: boolean;
  onToggle: () => void;
  title: string;
  meta: string;
  snippet?: string | null;
  score: number | null;
  reason: string | null;
  radio?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const { label, variant } = bucket(score);

  return (
    <li className="border-b border-border last:border-0">
      <label className="flex cursor-pointer items-start gap-3 p-3 hover:bg-surface-2">
        <input
          type={radio ? 'radio' : 'checkbox'}
          checked={checked}
          onChange={onToggle}
          className="mt-1 size-4 shrink-0 rounded border-border-strong"
        />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">{title}</p>
          <p className="mt-0.5 truncate text-xs text-fg-subtle">{meta}</p>
          {snippet && <p className="mt-1 line-clamp-1 text-xs text-fg-faint">{snippet}</p>}

          <div className="mt-2 flex items-center gap-2">
            <Meter value={score ?? 0} label={`Relevance ${label}`} className="max-w-[100px]" />
            {/* Text bucket alongside the bar: score colour alone is not enough. */}
            <Badge variant={variant} size="sm">
              {label}
              {score !== null && ` ${score.toFixed(2)}`}
            </Badge>
          </div>

          {reason && (
            <div className="mt-1.5">
              <p className={cn('text-xs text-fg-muted', !expanded && 'line-clamp-2')}>{reason}</p>
              {reason.length > 90 && (
                <button
                  type="button"
                  className="mt-0.5 text-xs text-primary hover:underline"
                  onClick={(e) => {
                    e.preventDefault();
                    setExpanded((v) => !v);
                  }}
                >
                  {expanded ? 'less' : 'why?'}
                </button>
              )}
            </div>
          )}
        </div>
      </label>
    </li>
  );
}

/**
 * Search calendar and email for items belonging to this meeting.
 *
 * Mounted on the job page as the productive-wait slot: diarization takes
 * minutes, so the user reviews matches during exactly the window they would
 * otherwise sit idle.
 */
export function McpMatchPanel({
  meeting,
  onAttached,
}: {
  meeting: Meeting;
  onAttached?: () => void;
}) {
  const queryClient = useQueryClient();
  const [jobId, setJobId] = useState<string | null>(null);
  const [pickedEvent, setPickedEvent] = useState<string | null>(null);
  const [pickedEmails, setPickedEmails] = useState<Set<string>>(new Set());
  const [seeded, setSeeded] = useState(false);

  const latest = useQuery({
    queryKey: ['match', meeting.id],
    queryFn: () => api.get<MatchRun>(`/meetings/${meeting.id}/match/latest`),
    retry: (_, error) => !(error instanceof ApiError && error.status === 404),
    refetchInterval: jobId ? 2000 : false,
  });

  const start = useMutation({
    mutationFn: () => api.post<{ job_id: string }>(`/meetings/${meeting.id}/match`, {}),
    onSuccess: (data) => {
      setJobId(data.job_id);
      setSeeded(false);
    },
  });

  // Stop polling once a fresh run has landed.
  useEffect(() => {
    if (jobId && latest.data && latest.data.created_at) {
      const age = Date.now() - new Date(latest.data.created_at).getTime();
      if (age < 60_000) setJobId(null);
    }
  }, [jobId, latest.data]);

  // Pre-tick what the ranker is confident about: un-ticking is faster than
  // ticking, and it is usually right.
  useEffect(() => {
    if (seeded || !latest.data) return;
    const event = latest.data.events.find((e) => e.suggested);
    setPickedEvent(event?.uid ?? null);
    setPickedEmails(
      new Set(latest.data.emails.filter((m) => m.suggested).map((m) => m.message_id)),
    );
    setSeeded(true);
  }, [latest.data, seeded]);

  const confirm = useMutation({
    mutationFn: () =>
      api.post<{ meeting: Meeting; attached_events: number; attached_emails: number }>(
        `/meetings/${meeting.id}/match/confirm`,
        {
          event_uids: pickedEvent ? [pickedEvent] : [],
          email_message_ids: [...pickedEmails],
          append_event_title_to_meeting_title: true,
        },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['meeting', String(meeting.id)] });
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', String(meeting.thread_id)] });
      void queryClient.invalidateQueries({ queryKey: ['match', meeting.id] });
      onAttached?.();
    },
  });

  const run = latest.data;
  const selectedCount = (pickedEvent ? 1 : 0) + pickedEmails.size;

  const previewTitle = useMemo(() => {
    if (!pickedEvent || !run) return null;
    const event = run.events.find((e) => e.uid === pickedEvent);
    if (!event?.summary) return null;
    const suffix = ` — ${event.summary.trim()}`;
    return meeting.title.endsWith(suffix) ? null : meeting.title + suffix;
  }, [pickedEvent, run, meeting.title]);

  const running = start.isPending || !!jobId;

  return (
    <Card className="overflow-hidden">
      <div className="flex items-center gap-3 border-b border-border p-4">
        <Link2 className="size-5 shrink-0 text-primary" aria-hidden />
        <div className="min-w-0 flex-1">
          <h3 className="font-display text-lg font-semibold">Related items</h3>
          <p className="text-sm text-fg-subtle">
            Search your calendar and email for things that belong to this meeting.
          </p>
        </div>
        <Button variant="secondary" onClick={() => start.mutate()} loading={running}>
          <Search />
          {run ? 'Search again' : 'Find matches'}
        </Button>
      </div>

      {running && (
        <div className="flex items-center gap-2 p-4 text-sm text-fg-subtle">
          <Spinner /> Searching calendar and email, then ranking what comes back…
        </div>
      )}

      {start.error && (
        <p className="p-4 text-sm text-danger-ink">{(start.error as Error).message}</p>
      )}

      {!run && !running && !start.error && (
        <p className="p-4 text-sm text-fg-subtle">
          Nothing searched yet.
        </p>
      )}

      {run && !running && (
        <>
          {(run.calendar_error || run.email_error || run.error) && (
            <div className="flex items-start gap-2 border-b border-border bg-warning-soft/40 p-3">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
              <div className="text-xs text-fg-muted">
                {run.calendar_error && <p>Calendar: {run.calendar_error}</p>}
                {run.email_error && <p>Email: {run.email_error}</p>}
                {run.error && <p>Ranking unavailable — results are shown unranked.</p>}
              </div>
            </div>
          )}

          <div className="grid gap-0 lg:grid-cols-2">
            <section className="border-b border-border lg:border-b-0 lg:border-r">
              <h4 className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs font-semibold uppercase tracking-wide text-fg-subtle">
                <CalendarDays className="size-3.5 text-entity-event" aria-hidden />
                Calendar ({run.events.length})
              </h4>
              {run.events.length === 0 ? (
                <p className="p-3 text-sm text-fg-faint">No events in the window.</p>
              ) : (
                <ul className="max-h-96 overflow-y-auto">
                  {run.events.map((event: CalendarEvent) => (
                    <CandidateRow
                      key={event.uid}
                      radio
                      checked={pickedEvent === event.uid}
                      onToggle={() =>
                        setPickedEvent((prev) => (prev === event.uid ? null : event.uid))
                      }
                      title={event.summary || 'Untitled event'}
                      meta={[
                        (event.start ?? event.start_at ?? '').slice(0, 16).replace('T', ' '),
                        event.calendar_name,
                      ]
                        .filter(Boolean)
                        .join(' · ')}
                      score={event.relevance_score}
                      reason={event.relevance_reason}
                    />
                  ))}
                </ul>
              )}
            </section>

            <section>
              <h4 className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs font-semibold uppercase tracking-wide text-fg-subtle">
                <Mail className="size-3.5 text-entity-email" aria-hidden />
                Email ({run.emails.length})
              </h4>
              {run.emails.length === 0 ? (
                <p className="p-3 text-sm text-fg-faint">No emails in the window.</p>
              ) : (
                <ul className="max-h-96 overflow-y-auto">
                  {run.emails.map((email: Email) => (
                    <CandidateRow
                      key={email.message_id}
                      checked={pickedEmails.has(email.message_id)}
                      onToggle={() =>
                        setPickedEmails((prev) => {
                          const next = new Set(prev);
                          if (next.has(email.message_id)) next.delete(email.message_id);
                          else next.add(email.message_id);
                          return next;
                        })
                      }
                      title={email.subject || '(no subject)'}
                      meta={[email.sender, (email.date ?? '').slice(0, 16).replace('T', ' ')]
                        .filter(Boolean)
                        .join(' · ')}
                      snippet={email.snippet}
                      score={email.relevance_score}
                      reason={email.relevance_reason}
                    />
                  ))}
                </ul>
              )}
            </section>
          </div>

          <div className="flex flex-wrap items-center gap-3 border-t border-border bg-surface-2/50 p-3">
            <div className="min-w-0 flex-1 text-sm">
              <span className="font-medium tabular">{selectedCount}</span> selected
              {previewTitle && (
                <p className="mt-0.5 truncate text-xs text-fg-subtle">
                  Title will become “{previewTitle}”
                </p>
              )}
            </div>
            <Button
              variant="primary"
              disabled={selectedCount === 0}
              loading={confirm.isPending}
              onClick={() => confirm.mutate()}
            >
              Attach {selectedCount > 0 ? selectedCount : ''}
            </Button>
          </div>

          {confirm.isSuccess && (
            <p className="border-t border-border bg-success-soft/40 p-3 text-sm text-success-ink">
              Attached {confirm.data.attached_events} event
              {confirm.data.attached_events === 1 ? '' : 's'} and{' '}
              {confirm.data.attached_emails} email
              {confirm.data.attached_emails === 1 ? '' : 's'} to the thread.
            </p>
          )}
        </>
      )}
    </Card>
  );
}
