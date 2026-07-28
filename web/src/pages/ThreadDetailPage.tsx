import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ArrowLeft,
  CalendarDays,
  CheckSquare,
  Clock,
  ExternalLink,
  Mail,
  MapPin,
  Mic,
  Plus,
  Trash2,
  X,
} from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { Badge, Card, Skeleton } from '@/components/ui/primitives';
import { EmptyState, ErrorState } from '@/components/ui/states';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import { emailLink } from '@/lib/links';
import { fmtClock } from '@/lib/time';
import type {
  CalendarEvent,
  Email,
  Meeting,
  Thread,
  TimelineItem,
} from '@/types/api';

type Filter = 'meeting' | 'event' | 'email';

const KIND_META: Record<Filter, { label: string; icon: typeof Mic; accent: string }> = {
  meeting: { label: 'Meetings', icon: Mic, accent: 'text-entity-meeting' },
  event: { label: 'Events', icon: CalendarDays, accent: 'text-entity-event' },
  email: { label: 'Emails', icon: Mail, accent: 'text-entity-email' },
};

function dayKey(iso: string | null): string {
  if (!iso) return 'Undated';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return 'Undated';
  const today = new Date();
  const yesterday = new Date(Date.now() - 86_400_000);
  if (date.toDateString() === today.toDateString()) return 'Today';
  if (date.toDateString() === yesterday.toDateString()) return 'Yesterday';
  return date.toLocaleDateString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: date.getFullYear() === today.getFullYear() ? undefined : 'numeric',
  });
}

function timeOf(iso: string | null): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

function MeetingTimelineCard({ meeting }: { meeting: Meeting }) {
  return (
    <Card interactive className="p-4">
      <Link to={`/meetings/${meeting.id}`} className="block">
        <div className="flex items-start justify-between gap-3">
          <h4 className="font-medium leading-snug">{meeting.title}</h4>
          <div className="flex shrink-0 items-center gap-2">
            {meeting.status === 'processing' && (
              <Badge variant="info" dot>
                Processing
              </Badge>
            )}
            {meeting.status === 'failed' && <Badge variant="danger">Failed</Badge>}
            {meeting.audio_duration_sec != null && (
              <span className="font-mono text-xs text-fg-subtle">
                {fmtClock(meeting.audio_duration_sec)}
              </span>
            )}
          </div>
        </div>

        {meeting.summary_tldr && (
          <p className="mt-1.5 line-clamp-2 text-sm text-fg-muted">{meeting.summary_tldr}</p>
        )}

        <div className="mt-3 flex items-center gap-3 text-xs text-fg-subtle">
          {meeting.speaker_count > 0 && <span>{meeting.speaker_count} speakers</span>}
          {meeting.open_action_items > 0 && (
            <span className="inline-flex items-center gap-1">
              <CheckSquare className="size-3.5" aria-hidden />
              {meeting.open_action_items} open
            </span>
          )}
          <span className="ml-auto text-primary">Open transcript →</span>
        </div>
      </Link>
    </Card>
  );
}

/** Detach an attached item from its thread.
 *
 * Only the copy on the thread goes; nothing is touched in the actual calendar or
 * mailbox, which is why this needs no confirmation dialog -- re-running the match
 * offers it straight back. */
function useDetach(threadId: string, kind: 'emails' | 'calendar-events') {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.del(`/threads/${threadId}/${kind}/${id}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['thread', threadId] });
    },
  });
}

function DetachButton({
  onClick,
  pending,
  label,
}: {
  onClick: () => void;
  pending: boolean;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={pending}
      aria-label={label}
      title={label}
      // Revealed on hover, but always reachable by keyboard -- hover-only
      // controls are invisible to anyone tabbing through.
      className={cn(
        'shrink-0 rounded p-1 text-fg-faint transition-opacity',
        'opacity-0 group-hover:opacity-100 focus-visible:opacity-100',
        'hover:text-danger-ink disabled:opacity-50',
      )}
    >
      <X className="size-3.5" aria-hidden />
    </button>
  );
}

function EventTimelineCard({ event, threadId }: { event: CalendarEvent; threadId: string }) {
  const detach = useDetach(threadId, 'calendar-events');
  return (
    <div className="group rounded-md border-l-2 border-entity-event bg-surface-2/50 py-2 pl-3 pr-3">
      <div className="flex items-start gap-2">
        <p className="min-w-0 flex-1 text-sm font-medium">
          {event.url ? (
            <a
              href={event.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-primary hover:underline"
            >
              {event.summary || 'Untitled event'}
              <ExternalLink className="size-3" aria-hidden />
            </a>
          ) : (
            event.summary || 'Untitled event'
          )}
        </p>
        {event.id !== undefined && (
          <DetachButton
            onClick={() => detach.mutate(event.id as number)}
            pending={detach.isPending}
            label="Remove this event from the thread"
          />
        )}
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-fg-subtle">
        {event.calendar_name && <span>{event.calendar_name}</span>}
        {event.location && (
          <span className="inline-flex items-center gap-1">
            <MapPin className="size-3" aria-hidden />
            {event.location}
          </span>
        )}
        {event.relevance_reason && (
          <span className="italic text-fg-faint">{event.relevance_reason}</span>
        )}
      </div>
    </div>
  );
}

function EmailTimelineCard({ email, threadId }: { email: Email; threadId: string }) {
  const href = emailLink(email);
  const detach = useDetach(threadId, 'emails');
  return (
    <div className="group py-1.5 pl-3">
      <div className="flex items-start gap-2">
        <p className="min-w-0 flex-1 text-sm">
          {href ? (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-primary hover:underline"
            >
              {email.subject || '(no subject)'}
              <ExternalLink className="size-3" aria-hidden />
            </a>
          ) : (
            email.subject || '(no subject)'
          )}
        </p>
        {typeof email.id === 'number' && (
          <DetachButton
            onClick={() => detach.mutate(email.id as number)}
            pending={detach.isPending}
            label="Remove this email from the thread"
          />
        )}
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-3 text-xs text-fg-subtle">
        <span className="truncate">{email.sender}</span>
        {email.tag && <Badge variant="outline" size="sm">{email.tag}</Badge>}
      </div>
      {email.snippet && (
        <p className="mt-1 line-clamp-1 text-xs text-fg-faint">{email.snippet}</p>
      )}
    </div>
  );
}

export function ThreadDetailPage() {
  const { threadId } = useParams<{ threadId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [filters, setFilters] = useState<Set<Filter>>(
    () => new Set(['meeting', 'event', 'email'] as Filter[]),
  );

  const thread = useQuery({
    queryKey: ['thread', threadId],
    queryFn: () => api.get<Thread>(`/threads/${threadId}`),
    enabled: !!threadId,
  });

  const timeline = useQuery({
    queryKey: ['thread-timeline', threadId],
    queryFn: () => api.get<TimelineItem[]>(`/threads/${threadId}/timeline`),
    enabled: !!threadId,
  });

  const remove = useMutation({
    mutationFn: () => api.del(`/threads/${threadId}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['threads'] });
      navigate('/');
    },
  });

  const grouped = useMemo(() => {
    const items = (timeline.data ?? []).filter((i) => filters.has(i.kind as Filter));
    const groups: { day: string; items: TimelineItem[] }[] = [];
    for (const item of items) {
      const day = dayKey(item.at);
      const last = groups[groups.length - 1];
      if (last && last.day === day) last.items.push(item);
      else groups.push({ day, items: [item] });
    }
    return groups;
  }, [timeline.data, filters]);

  function toggle(kind: Filter) {
    setFilters((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  }

  if (thread.isError) return <ErrorState error={thread.error} />;

  return (
    <div className="space-y-6">
      <Link
        to="/"
        className="inline-flex items-center gap-1 text-sm text-fg-subtle hover:text-fg"
      >
        <ArrowLeft className="size-4" aria-hidden />
        Threads
      </Link>

      {thread.isLoading ? (
        <Skeleton className="h-16 w-full max-w-xl" />
      ) : (
        thread.data && (
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <h1 className="font-display text-2xl font-semibold">{thread.data.title}</h1>
              {thread.data.description && (
                <p className="mt-1 max-w-2xl text-sm text-fg-muted">
                  {thread.data.description}
                </p>
              )}
              <p className="mt-2 flex flex-wrap items-center gap-x-3 text-sm text-fg-subtle">
                <span>{thread.data.meeting_count} meetings</span>
                <span>·</span>
                <span>{thread.data.event_count} events</span>
                <span>·</span>
                <span>{thread.data.email_count} emails</span>
              </p>
            </div>

            <div className="flex gap-2">
              <Button variant="primary" asChild>
                <Link to={`/meetings/new?threadId=${thread.data.id}`}>
                  <Plus />
                  Add meeting
                </Link>
              </Button>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Delete thread"
                onClick={() => {
                  if (
                    window.confirm(
                      `Delete "${thread.data!.title}" and all ${thread.data!.meeting_count} of its meetings? This also removes the audio from disk.`,
                    )
                  ) {
                    remove.mutate();
                  }
                }}
              >
                <Trash2 />
              </Button>
            </div>
          </div>
        )
      )}

      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filter timeline">
        {(Object.keys(KIND_META) as Filter[]).map((kind) => {
          const { label, icon: Icon, accent } = KIND_META[kind];
          const on = filters.has(kind);
          return (
            <button
              key={kind}
              aria-pressed={on}
              onClick={() => toggle(kind)}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-sm transition-colors duration-fast',
                on
                  ? 'border-border-strong bg-surface text-fg'
                  : 'border-border bg-transparent text-fg-faint',
              )}
            >
              <Icon className={cn('size-3.5', on ? accent : 'text-fg-faint')} aria-hidden />
              {label}
            </button>
          );
        })}
      </div>

      {timeline.isError && <ErrorState error={timeline.error} onRetry={() => timeline.refetch()} />}

      {timeline.isLoading && (
        <div className="space-y-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-20 w-full" />
          ))}
        </div>
      )}

      {timeline.data && grouped.length === 0 && (
        <Card>
          <EmptyState
            icon={Clock}
            title="Nothing here yet"
            description="Upload a recording, or search your calendar and email for related items."
            action={
              <Button variant="primary" asChild>
                <Link to={`/meetings/new?threadId=${threadId}`}>
                  <Plus />
                  Add a meeting
                </Link>
              </Button>
            }
          />
        </Card>
      )}

      {grouped.length > 0 && (
        <ol aria-label="Thread timeline" className="relative">
          {grouped.map((group) => (
            <li key={group.day} className="relative">
              <h3 className="sticky top-16 z-10 -mx-1 bg-bg/90 py-2 pl-1 text-xs font-semibold uppercase tracking-wide text-fg-subtle backdrop-blur">
                {group.day}
              </h3>

              <ol className="relative ml-[15px] border-l border-border pl-6">
                {group.items.map((item) => {
                  const { icon: Icon, accent } = KIND_META[item.kind as Filter];
                  return (
                    <li
                      key={`${item.kind}-${item.id}`}
                      className="relative py-2"
                      aria-label={`${KIND_META[item.kind as Filter].label.slice(0, -1)} · ${
                        (item.payload as Meeting).title ??
                        (item.payload as CalendarEvent).summary ??
                        (item.payload as Email).subject ??
                        ''
                      }`}
                    >
                      <span
                        className="absolute -left-[31px] top-3 grid size-[30px] place-items-center rounded-full border border-border bg-surface"
                        aria-hidden
                      >
                        <Icon className={cn('size-3.5', accent)} />
                      </span>

                      <div className="flex items-baseline gap-2">
                        <time
                          dateTime={item.at ?? undefined}
                          className="w-12 shrink-0 font-mono text-xs text-fg-faint"
                        >
                          {timeOf(item.at)}
                        </time>
                        <div className="min-w-0 flex-1">
                          {item.kind === 'meeting' && (
                            <MeetingTimelineCard meeting={item.payload as Meeting} />
                          )}
                          {item.kind === 'event' && (
                            <EventTimelineCard
                              event={item.payload as CalendarEvent}
                              threadId={threadId!}
                            />
                          )}
                          {item.kind === 'email' && (
                            <EmailTimelineCard email={item.payload as Email} threadId={threadId!} />
                          )}
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ol>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
