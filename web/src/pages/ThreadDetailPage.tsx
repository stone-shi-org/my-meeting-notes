import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Archive,
  ArchiveRestore,
  ArrowLeft,
  CalendarDays,
  CheckCheck,
  CheckSquare,
  Clock,
  ExternalLink,
  Mail,
  MapPin,
  Mic,
  NotebookPen,
  Pencil,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { DeleteMeetingButton } from '@/components/meetings/DeleteMeetingButton';
import { NoteCard, NoteComposer } from '@/components/notes/NoteCard';
import { MoveToThread } from '@/components/thread/MoveToThread';
import { ThreadChatPanel } from '@/components/thread/ThreadChatPanel';
import { Button } from '@/components/ui/Button';
import { Badge, Card, Input, Skeleton } from '@/components/ui/primitives';
import { EmptyState, ErrorState } from '@/components/ui/states';
import type { NoteScope } from '@/hooks/useNotes';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import { emailLink } from '@/lib/links';
import { fmtClock, fmtRelative } from '@/lib/time';
import type {
  CalendarEvent,
  Email,
  FollowUpResult,
  Meeting,
  NextStepResult,
  Note,
  Thread,
  TimelineItem,
} from '@/types/api';

type Filter = 'meeting' | 'event' | 'email' | 'note';

const KIND_META: Record<Filter, { label: string; icon: typeof Mic; accent: string }> = {
  meeting: { label: 'Meetings', icon: Mic, accent: 'text-entity-meeting' },
  event: { label: 'Events', icon: CalendarDays, accent: 'text-entity-event' },
  email: { label: 'Emails', icon: Mail, accent: 'text-entity-email' },
  note: { label: 'Notes', icon: NotebookPen, accent: 'text-entity-note' },
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

function MeetingTimelineCard({ meeting, threadId }: { meeting: Meeting; threadId: string }) {
  const queryClient = useQueryClient();
  // Created from a calendar event, or by hand, and still waiting for its audio.
  const awaitingAudio = !meeting.has_audio && meeting.status !== 'processing';

  // Moving a meeting cascades to its attached emails, events and notes on the
  // backend, so the invalidation here is the same shape as any other move --
  // both threads' timelines and counts, plus the list for the home screen.
  const move = useMutation({
    mutationFn: (targetThreadId: number) =>
      api.post(`/meetings/${meeting.id}/move`, { target_thread_id: targetThreadId }),
    onSuccess: (_data, targetThreadId) => {
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['thread', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', String(targetThreadId)] });
      void queryClient.invalidateQueries({ queryKey: ['thread', String(targetThreadId)] });
      void queryClient.invalidateQueries({ queryKey: ['threads'] });
    },
  });

  return (
    <Card interactive className="group relative p-4">
      {/* Stretched link: one anchor covering the card, so the delete button can
          be a sibling rather than a button nested inside an anchor. The content
          ignores pointer events so clicks land on the link underneath. */}
      <Link
        to={`/meetings/${meeting.id}`}
        className="absolute inset-0 rounded-lg"
        aria-label={`Open ${meeting.title}`}
      />
      <div className="pointer-events-none relative">
        <div className="flex items-start justify-between gap-3">
          <h4 className="font-medium leading-snug">{meeting.title}</h4>
          <div className="pointer-events-auto flex shrink-0 items-center gap-2">
            {meeting.status === 'processing' && (
              <Badge variant="info" dot>
                Processing
              </Badge>
            )}
            {meeting.status === 'failed' && <Badge variant="danger">Failed</Badge>}
            {awaitingAudio && <Badge variant="neutral">No recording</Badge>}
            {meeting.audio_duration_sec != null && (
              <span className="font-mono text-xs text-fg-subtle">
                {fmtClock(meeting.audio_duration_sec)}
              </span>
            )}
            <MoveToThread
              currentThreadId={threadId}
              pending={move.isPending}
              label="Move this meeting to another thread"
              className="opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
              onMove={(targetThreadId) => move.mutate(targetThreadId)}
            />
            <DeleteMeetingButton
              meeting={meeting}
              variant="icon"
              onDeleted={() => {
                void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
                void queryClient.invalidateQueries({ queryKey: ['thread', threadId] });
                void queryClient.invalidateQueries({ queryKey: ['threads'] });
              }}
            />
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
          {move.error && (
            <span className="pointer-events-auto text-danger-ink">
              {(move.error as Error).message}
            </span>
          )}
          {/* The recording is what this meeting is missing, so that is what the
              card offers -- not "open transcript", which leads to a page whose
              only content is the same invitation. */}
          <span className="ml-auto text-primary">
            {awaitingAudio ? 'Add a recording →' : 'Open transcript →'}
          </span>
        </div>
      </div>
    </Card>
  );
}

type Kind = 'emails' | 'calendar-events';

/** Detach an attached item from its thread.
 *
 * Only the copy on the thread goes; nothing is touched in the actual calendar or
 * mailbox, which is why this needs no confirmation dialog -- re-running the match
 * offers it straight back. */
function useDetach(threadId: string, kind: Kind) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.del(`/threads/${threadId}/${kind}/${id}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['thread', threadId] });
    },
  });
}

/** Move an attached item onto another thread.
 *
 * Invalidates both ends: the source loses the item from its timeline and
 * counts, the destination gains it -- and `threads` too, since both cards'
 * counts on the home screen are affected. */
function useMoveItem(threadId: string, kind: Kind) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, targetThreadId }: { id: number; targetThreadId: number }) =>
      api.post(`/threads/${threadId}/${kind}/${id}/move`, { target_thread_id: targetThreadId }),
    onSuccess: (_data, { targetThreadId }) => {
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['thread', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', String(targetThreadId)] });
      void queryClient.invalidateQueries({ queryKey: ['thread', String(targetThreadId)] });
      void queryClient.invalidateQueries({ queryKey: ['threads'] });
    },
  });
}

/** Clear the "arrived while you were away" mark on one item.
 *
 * Fired on the link the user actually clicks, which is the moment the mark stops
 * being true. Harmless to repeat: the backend only stamps a row that is still
 * unread, so a second click is a 200 with nothing changed. */
function useMarkRead(threadId: string, kind: Kind) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.post(`/threads/${threadId}/${kind}/${id}/read`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['thread', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['threads'] });
    },
  });
}

/** The unread mark on one row. Paired with bold text and a "New" label, never
 *  the only thing saying so. */
function UnreadDot() {
  return (
    <span className="mt-1.5 size-2 shrink-0 rounded-full glow-dot" aria-hidden />
  );
}

function MarkReadButton({ onClick, pending }: { onClick: () => void; pending: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={pending}
      className="shrink-0 rounded px-1.5 py-0.5 text-2xs font-medium text-info-ink hover:bg-info-soft disabled:opacity-50"
    >
      Mark read
    </button>
  );
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
  const move = useMoveItem(threadId, 'calendar-events');
  const markRead = useMarkRead(threadId, 'calendar-events');
  const unread = !!event.unread && event.id !== undefined;
  const read = () => {
    if (unread) markRead.mutate(event.id as number);
  };

  return (
    <div
      className={cn(
        'group rounded-md border-l-2 bg-surface-2/50 py-2 pl-3 pr-3',
        unread ? 'border-info bg-info-soft/30' : 'border-entity-event',
      )}
    >
      <div className="flex items-start gap-2">
        {unread && <UnreadDot />}
        <p className={cn('min-w-0 flex-1 text-sm', unread ? 'font-semibold' : 'font-medium')}>
          {event.url ? (
            <a
              href={event.url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={read}
              className="inline-flex items-center gap-1 text-primary hover:underline"
            >
              {event.summary || 'Untitled event'}
              <ExternalLink className="size-3" aria-hidden />
            </a>
          ) : (
            event.summary || 'Untitled event'
          )}
        </p>
        {unread && <MarkReadButton onClick={read} pending={markRead.isPending} />}
        {event.id !== undefined && (
          <>
            <MoveToThread
              currentThreadId={threadId}
              pending={move.isPending}
              label="Move this event to another thread"
              onMove={(targetThreadId) => move.mutate({ id: event.id as number, targetThreadId })}
            />
            <DetachButton
              onClick={() => detach.mutate(event.id as number)}
              pending={detach.isPending}
              label="Remove this event from the thread"
            />
          </>
        )}
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-fg-subtle">
        {unread && <span className="font-medium text-info-ink">New · added for you</span>}
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
        {move.error && (
          <span className="text-danger-ink">{(move.error as Error).message}</span>
        )}
      </div>
    </div>
  );
}

function EmailTimelineCard({ email, threadId }: { email: Email; threadId: string }) {
  const href = emailLink(email);
  const detach = useDetach(threadId, 'emails');
  const move = useMoveItem(threadId, 'emails');
  const markRead = useMarkRead(threadId, 'emails');
  const unread = !!email.unread && typeof email.id === 'number';
  const read = () => {
    if (unread) markRead.mutate(email.id as number);
  };

  return (
    <div
      className={cn(
        'group py-1.5 pl-3',
        unread && 'rounded-md border-l-2 border-info bg-info-soft/30 pr-3',
      )}
    >
      <div className="flex items-start gap-2">
        {unread && <UnreadDot />}
        <p className={cn('min-w-0 flex-1 text-sm', unread && 'font-semibold')}>
          {href ? (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              onClick={read}
              className="inline-flex items-center gap-1 text-primary hover:underline"
            >
              {email.subject || '(no subject)'}
              <ExternalLink className="size-3" aria-hidden />
            </a>
          ) : (
            email.subject || '(no subject)'
          )}
        </p>
        {unread && <MarkReadButton onClick={read} pending={markRead.isPending} />}
        {typeof email.id === 'number' && (
          <>
            <MoveToThread
              currentThreadId={threadId}
              pending={move.isPending}
              label="Move this email to another thread"
              onMove={(targetThreadId) => move.mutate({ id: email.id as number, targetThreadId })}
            />
            <DetachButton
              onClick={() => detach.mutate(email.id as number)}
              pending={detach.isPending}
              label="Remove this email from the thread"
            />
          </>
        )}
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-3 text-xs text-fg-subtle">
        {unread && <span className="font-medium text-info-ink">New · added for you</span>}
        <span className="truncate">{email.sender}</span>
        {email.tag && <Badge variant="outline" size="sm">{email.tag}</Badge>}
        {move.error && (
          <span className="text-danger-ink">{(move.error as Error).message}</span>
        )}
      </div>
      {email.snippet && (
        <p
          className={cn(
            'mt-1 line-clamp-1 text-xs',
            // Unread rows read as a headline, so the preview steps up from the
            // decorative faint token to one that is legal for text.
            unread ? 'text-fg-muted' : 'text-fg-faint',
          )}
        >
          {email.snippet}
        </p>
      )}
    </div>
  );
}

/**
 * Click-to-edit thread title, same interaction as a thread group's rename
 * (ThreadGroups.tsx): a pencil button swaps the heading for an input, Enter or
 * blur commits, Escape reverts. An empty or unchanged draft is a no-op rather
 * than a write -- there is nothing useful to save.
 */
export function ThreadTitle({ thread }: { thread: Thread }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(thread.title);

  const rename = useMutation({
    mutationFn: (title: string) => api.patch<Thread>(`/threads/${thread.id}`, { title }),
    onSuccess: () => {
      setEditing(false);
      void queryClient.invalidateQueries({ queryKey: ['thread', String(thread.id)] });
      void queryClient.invalidateQueries({ queryKey: ['threads'] });
    },
  });

  function commit() {
    const title = draft.trim();
    if (!title || title === thread.title) {
      setEditing(false);
      setDraft(thread.title);
      return;
    }
    rename.mutate(title);
  }

  if (editing) {
    return (
      <div>
        <Input
          className="font-display text-2xl font-semibold"
          autoFocus
          maxLength={300}
          value={draft}
          aria-label="Thread title"
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.currentTarget.blur();
            } else if (e.key === 'Escape') {
              setDraft(thread.title);
              setEditing(false);
            }
          }}
        />
        {rename.error && (
          <p role="alert" className="mt-1 text-sm text-danger-ink">
            {(rename.error as Error).message}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="group flex items-center gap-1.5">
      <h1 className="font-display text-2xl font-semibold">{thread.title}</h1>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label="Rename thread"
        className="opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
        onClick={() => {
          setDraft(thread.title);
          setEditing(true);
        }}
      >
        <Pencil className="size-3.5" />
      </Button>
    </div>
  );
}

export function ThreadDetailPage() {
  const { threadId } = useParams<{ threadId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [filters, setFilters] = useState<Set<Filter>>(
    () => new Set(['meeting', 'event', 'email', 'note'] as Filter[]),
  );
  const [composing, setComposing] = useState(false);
  const noteScope: NoteScope = { kind: 'thread', threadId: threadId! };
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

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['thread', threadId] });
    void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
    void queryClient.invalidateQueries({ queryKey: ['threads'] });
  };

  /**
   * Put a finished thread away, or take it back out.
   *
   * Archiving is not deleting and not hiding-with-consequences: everything
   * stays, and the thread is still reachable through the Threads list's
   * "Archived only" filter. What it does change is that the periodic sweep
   * stops watching it (`due_threads` filters on `archived = 0`), so a project
   * nobody works on any more stops spending LLM tokens and provider quota
   * every half hour.
   */
  const setArchived = useMutation({
    mutationFn: (archived: boolean) => api.patch(`/threads/${threadId}`, { archived }),
    onSuccess: refresh,
  });

  const markAllRead = useMutation({
    mutationFn: () => api.post(`/threads/${threadId}/read`),
    onSuccess: refresh,
  });

  // The same sweep the scheduler runs, on demand. Same confidence threshold:
  // asking for it sooner is not asking for it to attach more freely.
  const checkNow = useMutation({
    mutationFn: () => api.post<FollowUpResult>(`/threads/${threadId}/follow-ups`),
    onSuccess: refresh,
  });

  // The cached "next step" suggestion. Patched into the thread query directly
  // rather than refetching it, since a refetch would also re-derive
  // next_step_stale from a fingerprint that (by definition) hasn't moved.
  const nextStep = useMutation({
    mutationFn: () => api.post<NextStepResult>(`/threads/${threadId}/next-step`),
    onSuccess: (data) => {
      queryClient.setQueryData<Thread | undefined>(['thread', threadId], (prev) =>
        prev
          ? {
              ...prev,
              next_step: data.next_step ?? prev.next_step,
              next_step_generated_at: data.next_step_generated_at ?? prev.next_step_generated_at,
              next_step_stale: data.next_step_stale,
            }
          : prev,
      );
    },
  });

  // Auto-generate once per thread visit when a meeting/email/event added since
  // the last suggestion has made it stale (or nothing has been generated yet).
  // Keyed on threadId so a failed attempt doesn't retry on every re-render --
  // the refresh button in the box covers that.
  const autoRefreshedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!threadId || !thread.data) return;
    if (!thread.data.next_step_stale) return;
    if (autoRefreshedFor.current === threadId) return;
    autoRefreshedFor.current = threadId;
    nextStep.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, thread.data?.next_step_stale]);

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
              <div className="flex flex-wrap items-center gap-2">
                <ThreadTitle thread={thread.data} />
                {thread.data.archived && <Badge variant="neutral">Archived</Badge>}
              </div>
              {thread.data.archived && (
                <p className="mt-1 text-sm text-fg-subtle">
                  Everything on it is kept. It is out of the Threads list, and follow-ups are
                  no longer checked for automatically.
                </p>
              )}
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
                <span>·</span>
                <span>{thread.data.note_count} notes</span>
                {thread.data.auto_match_at && (
                  <>
                    <span>·</span>
                    <span title={thread.data.auto_match_error ?? undefined}>
                      Checked for follow-ups{' '}
                      <time dateTime={thread.data.auto_match_at}>
                        {fmtRelative(thread.data.auto_match_at)}
                      </time>
                    </span>
                  </>
                )}
              </p>
            </div>

            <div className="flex gap-2">
              <Button
                variant="secondary"
                loading={checkNow.isPending}
                onClick={() => checkNow.mutate()}
                title="Search your calendar and email for anything new on this thread"
              >
                <RefreshCw />
                Check now
              </Button>
              <Button variant="primary" asChild>
                <Link to={`/meetings/new?threadId=${thread.data.id}`}>
                  <Plus />
                  Add meeting
                </Link>
              </Button>
              <Button
                variant="ghost"
                loading={setArchived.isPending}
                onClick={() => setArchived.mutate(!thread.data!.archived)}
                title={
                  thread.data.archived
                    ? 'Put it back on the Threads list and resume automatic follow-up checks'
                    : 'Keep everything, but take it off the Threads list and stop checking it for follow-ups'
                }
              >
                {thread.data.archived ? <ArchiveRestore /> : <Archive />}
                {thread.data.archived ? 'Unarchive' : 'Archive'}
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

      {thread.data && (
        <div className="flex flex-wrap items-start gap-3 rounded-lg border border-border-strong bg-surface-2/60 p-3">
          <Sparkles className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden />
          <div className="min-w-0 flex-1">
            <p className="text-xs font-semibold uppercase tracking-wide text-fg-subtle">
              Next step
            </p>
            {thread.data.next_step ? (
              <p className="mt-1 text-sm text-fg">{thread.data.next_step}</p>
            ) : nextStep.isPending ? (
              <p className="mt-1 text-sm text-fg-subtle">Figuring out what&apos;s next…</p>
            ) : (
              <p className="mt-1 text-sm text-fg-subtle">Not generated yet.</p>
            )}
            <p className="mt-1 text-xs text-fg-subtle">
              {thread.data.next_step_stale && thread.data.next_step && !nextStep.isPending && (
                <span>Outdated — new activity since this was generated. </span>
              )}
              {thread.data.next_step_generated_at && (
                <>
                  Generated{' '}
                  <time dateTime={thread.data.next_step_generated_at}>
                    {fmtRelative(thread.data.next_step_generated_at)}
                  </time>
                </>
              )}
            </p>
            {nextStep.error && (
              <p className="mt-1 text-xs text-danger-ink">{(nextStep.error as Error).message}</p>
            )}
          </div>
          <Button
            size="sm"
            variant="ghost"
            loading={nextStep.isPending}
            onClick={() => nextStep.mutate()}
            title="Regenerate the next-step suggestion"
          >
            <RefreshCw />
            Refresh
          </Button>
        </div>
      )}

      {thread.data && thread.data.unread_count > 0 && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-info/40 bg-info-soft/40 p-3">
          <span className="size-2 shrink-0 rounded-full glow-dot" aria-hidden />
          <p className="min-w-0 flex-1 text-sm text-fg">
            <span className="font-semibold">
              {thread.data.unread_count} new item{thread.data.unread_count === 1 ? '' : 's'}
            </span>{' '}
            {thread.data.unread_count === 1 ? 'was' : 'were'} added to this thread for you.
            Opening one clears its mark.
          </p>
          <Button
            size="sm"
            variant="ghost"
            loading={markAllRead.isPending}
            onClick={() => markAllRead.mutate()}
          >
            <CheckCheck />
            Mark all read
          </Button>
        </div>
      )}

      {checkNow.isSuccess && (
        <p className="text-sm text-fg-subtle" role="status">
          {checkNow.data.skipped === 'no_integrations'
            ? 'No calendar or inbox is connected to your account yet.'
            : checkNow.data.error
              ? `Checked, but ranking was unavailable — nothing was attached (${checkNow.data.error}).`
              : checkNow.data.attached_events + checkNow.data.attached_emails > 0
                ? `Attached ${checkNow.data.attached_events} event(s) and ${checkNow.data.attached_emails} email(s).`
                : `Nothing new confident enough to attach (${checkNow.data.candidates} candidate(s) looked at).`}
        </p>
      )}

      {checkNow.error && (
        <p className="text-sm text-danger-ink">{(checkNow.error as Error).message}</p>
      )}

      {setArchived.error && (
        <p className="text-sm text-danger-ink">{(setArchived.error as Error).message}</p>
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

        {/* Sits with the chips rather than in the header actions: it writes
            straight into the timeline directly below it. */}
        <Button
          size="sm"
          variant="ghost"
          className="ml-auto"
          onClick={() => {
            setComposing(true);
            // Writing a note with the Notes chip off would file it somewhere
            // the page is not showing.
            setFilters((prev) => new Set(prev).add('note'));
          }}
        >
          <NotebookPen />
          New note
        </Button>
      </div>

      {composing && threadId && (
        <NoteComposer scope={noteScope} onDone={() => setComposing(false)} />
      )}

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
                            <MeetingTimelineCard
                              meeting={item.payload as Meeting}
                              threadId={threadId!}
                            />
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
                          {item.kind === 'note' && (
                            <NoteCard note={item.payload as Note} scope={noteScope} />
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

      {threadId && <ThreadChatPanel threadId={threadId} />}
    </div>
  );
}
