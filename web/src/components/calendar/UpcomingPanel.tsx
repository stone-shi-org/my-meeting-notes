import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  CalendarClock,
  CalendarPlus,
  Check,
  ExternalLink,
  MapPin,
  Users,
} from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { Badge, Card, Input, Label, Select, Skeleton, Textarea } from '@/components/ui/primitives';
import { ErrorState } from '@/components/ui/states';
import { api } from '@/lib/api';
import {
  eventDate,
  eventTimeLabel,
  groupByDay,
  localDatetimeValue,
} from '@/lib/calendar';
import { cn } from '@/lib/cn';
import type { Meeting, Paginated, Thread, UpcomingEvent, UpcomingList } from '@/types/api';

/** How many days ahead to look. Two weeks is the default the feature is built around. */
const RANGES = [7, 14, 30];

/** Shown collapsed; a fortnight of a busy calendar would bury the thread list. */
const COLLAPSED_DAYS = 3;

interface CreateResult {
  meeting: Meeting;
  thread_id: number;
  speaker_hints: number;
}

/** The event fields the API accepts, without the UI-only `attached` marker. */
function eventPayload(event: UpcomingEvent) {
  const { attached: _attached, ...rest } = event;
  return rest;
}

function CreateFromEventDialog({
  event,
  onClose,
}: {
  event: UpcomingEvent;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();

  const eventTitle = event.summary || 'Untitled event';
  const [title, setTitle] = useState(eventTitle);
  const [when, setWhen] = useState(
    localDatetimeValue(eventDate(event.start) ?? new Date()),
  );
  const [threadId, setThreadId] = useState('');
  const [newThreadTitle, setNewThreadTitle] = useState(eventTitle);
  const [newThreadDescription, setNewThreadDescription] = useState('');
  // Editable, because an attendee list is a guess at who will actually speak.
  const [speakers, setSpeakers] = useState(event.attendees.join(', '));

  const threads = useQuery({
    queryKey: ['threads', 'picker'],
    queryFn: () => api.get<Paginated<Thread>>('/threads', { page_size: 100 }),
  });

  const creatingNewThread = threadId === '';

  const create = useMutation({
    mutationFn: () =>
      api.post<CreateResult>('/calendar/upcoming/meeting', {
        event: eventPayload(event),
        title,
        meeting_at: new Date(when).toISOString(),
        speaker_names: speakers
          .split(',')
          .map((name) => name.trim())
          .filter(Boolean),
        ...(creatingNewThread
          ? {
              new_thread_title: newThreadTitle,
              new_thread_description: newThreadDescription || null,
            }
          : { thread_id: Number(threadId) }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['upcoming'] });
      void queryClient.invalidateQueries({ queryKey: ['threads'] });
    },
  });

  if (create.isSuccess) {
    const { meeting, thread_id, speaker_hints } = create.data;
    return (
      <div className="fixed inset-0 z-50 grid place-items-center bg-overlay p-4 backdrop-blur-sm">
        <Card className="w-full max-w-md p-6">
          <h2 className="font-display text-xl font-semibold">Meeting created</h2>
          <p className="mt-2 text-sm text-fg-muted">
            “{meeting.title}” is ready, with the event attached
            {speaker_hints > 0 &&
              ` and ${speaker_hints} speaker name${speaker_hints === 1 ? '' : 's'} noted`}
            . Upload the recording once you have it.
          </p>
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="ghost" onClick={onClose}>
              Close
            </Button>
            <Button variant="secondary" asChild>
              <Link to={`/threads/${thread_id}`}>Open thread</Link>
            </Button>
            <Button variant="primary" asChild>
              <Link to={`/meetings/new?threadId=${thread_id}`}>Upload recording</Link>
            </Button>
          </div>
        </Card>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center overflow-y-auto bg-overlay p-4 backdrop-blur-sm">
      <Card className="w-full max-w-md p-6">
        <h2 className="font-display text-xl font-semibold">New meeting from event</h2>
        <p className="mt-1 text-sm text-fg-subtle">
          The event stays attached, so its details reach the summary.
        </p>

        <form
          className="mt-5 space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <div>
            <Label htmlFor="ce-title">Title</Label>
            <Input
              id="ce-title"
              className="mt-1.5"
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </div>

          <div>
            <Label htmlFor="ce-when">When</Label>
            <Input
              id="ce-when"
              className="mt-1.5"
              type="datetime-local"
              value={when}
              onChange={(e) => setWhen(e.target.value)}
            />
          </div>

          <div>
            <Label htmlFor="ce-thread">Thread</Label>
            <Select
              id="ce-thread"
              className="mt-1.5"
              value={threadId}
              onChange={(e) => setThreadId(e.target.value)}
            >
              <option value="">＋ Create a new thread</option>
              {threads.data?.items.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.title}
                </option>
              ))}
            </Select>
          </div>

          {creatingNewThread && (
            <div className="space-y-3 rounded-md border border-border bg-surface-2/50 p-3">
              <div>
                <Label htmlFor="ce-nt-title">New thread title</Label>
                <Input
                  id="ce-nt-title"
                  className="mt-1.5"
                  required
                  value={newThreadTitle}
                  onChange={(e) => setNewThreadTitle(e.target.value)}
                />
              </div>
              <div>
                <Label htmlFor="ce-nt-desc">Description</Label>
                <Textarea
                  id="ce-nt-desc"
                  className="mt-1.5"
                  rows={2}
                  value={newThreadDescription}
                  onChange={(e) => setNewThreadDescription(e.target.value)}
                />
              </div>
            </div>
          )}

          <div>
            <Label htmlFor="ce-speakers">Speakers</Label>
            <Input
              id="ce-speakers"
              className="mt-1.5"
              value={speakers}
              onChange={(e) => setSpeakers(e.target.value)}
              placeholder="Alice, Bob, Priya"
            />
            <p className="mt-1 text-xs text-fg-subtle">
              {event.attendees.length > 0
                ? 'From the invitees. Trim it to who actually speaks.'
                : 'Comma separated. Offered as suggestions once we know who spoke most.'}
            </p>
          </div>

          {create.error && (
            <p role="alert" className="text-sm text-danger-ink">
              {(create.error as Error).message}
            </p>
          )}

          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={create.isPending}>
              Create meeting
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}

function EventRow({
  event,
  onCreate,
}: {
  event: UpcomingEvent;
  onCreate: () => void;
}) {
  return (
    <li className="flex items-start gap-3 border-b border-border p-3 last:border-0 hover:bg-surface-2">
      <span className="w-28 shrink-0 pt-0.5 text-xs tabular text-fg-subtle">
        {eventTimeLabel(event)}
      </span>

      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium">
          {event.url ? (
            <a
              href={event.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-primary hover:underline"
            >
              {event.summary || 'Untitled event'}
              <ExternalLink className="size-3 shrink-0" aria-hidden />
            </a>
          ) : (
            event.summary || 'Untitled event'
          )}
        </p>

        <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-fg-subtle">
          {event.calendar_name && <span className="truncate">{event.calendar_name}</span>}
          {event.location && (
            <span className="inline-flex min-w-0 items-center gap-1">
              <MapPin className="size-3 shrink-0 text-fg-faint" aria-hidden />
              <span className="truncate">{event.location}</span>
            </span>
          )}
          {event.attendees.length > 0 && (
            <span className="inline-flex items-center gap-1">
              <Users className="size-3 text-fg-faint" aria-hidden />
              {/* The count is decoration; the names themselves are the content. */}
              <span aria-hidden>{event.attendees.length}</span>
              <span className="sr-only">
                {event.attendees.length} invitees: {event.attendees.join(', ')}
              </span>
            </span>
          )}
        </div>
      </div>

      {event.attached ? (
        <Badge variant="success" className="mt-0.5 shrink-0">
          <Check className="size-3" aria-hidden />
          {event.attached.meeting_id ? (
            <Link
              to={`/meetings/${event.attached.meeting_id}`}
              className="hover:underline"
            >
              Meeting created
            </Link>
          ) : (
            'Attached'
          )}
        </Badge>
      ) : (
        <Button variant="secondary" size="sm" className="mt-0.5 shrink-0" onClick={onCreate}>
          <CalendarPlus />
          Create meeting
        </Button>
      )}
    </li>
  );
}

/**
 * What is coming up, across every connected calendar.
 *
 * The inverse of the match panel: instead of uploading a recording and then
 * hunting for the event it belongs to, start from the event -- the meeting is
 * created with its title, time and invitees already filled in, and the event is
 * attached to it there and then.
 */
export function UpcomingPanel() {
  const [days, setDays] = useState(14);
  const [expanded, setExpanded] = useState(false);
  const [creating, setCreating] = useState<UpcomingEvent | null>(null);

  const query = useQuery({
    queryKey: ['upcoming', days],
    queryFn: () => api.get<UpcomingList>('/calendar/upcoming', { days }),
    // Calendars change under us, and this is the landing page.
    staleTime: 60_000,
  });

  const data = query.data;
  const allDays = groupByDay(data?.events ?? []);
  const visibleDays = expanded ? allDays : allDays.slice(0, COLLAPSED_DAYS);
  const hiddenEvents = allDays
    .slice(COLLAPSED_DAYS)
    .reduce((total, day) => total + day.events.length, 0);

  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => query.refetch()} />;
  }

  if (data && data.connected === 0) {
    return (
      <Card className="flex flex-wrap items-center gap-3 p-4">
        <CalendarClock className="size-5 shrink-0 text-fg-faint" aria-hidden />
        <p className="min-w-0 flex-1 text-sm text-fg-subtle">
          Connect a calendar to see what is coming up and start a meeting from an invite.
        </p>
        <Button variant="secondary" size="sm" asChild>
          <Link to="/settings/integrations">Connect a calendar</Link>
        </Button>
      </Card>
    );
  }

  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-center gap-3 border-b border-border p-4">
        <CalendarClock className="size-5 shrink-0 text-entity-event" aria-hidden />
        <div className="min-w-0 flex-1">
          <h2 className="font-display text-lg font-semibold">Upcoming</h2>
          <p className="text-sm text-fg-subtle">
            Your calendars for the next {days} days. Create a meeting from any of them.
          </p>
        </div>
        <Select
          aria-label="How far ahead to look"
          className="w-auto"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
        >
          {RANGES.map((option) => (
            <option key={option} value={option}>
              Next {option} days
            </option>
          ))}
        </Select>
      </div>

      {/* `> 0`, not a bare length: a falsy 0 renders as a literal "0". */}
      {(data?.error || (data?.source_errors.length ?? 0) > 0) && (
        <div className="flex items-start gap-2 border-b border-border bg-warning-soft/40 p-3">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
          <div className="text-xs text-fg-muted">
            {/* Per-account, because "calendar failed" does not say which one. */}
            {data?.source_errors.map((failure) => (
              <p key={failure.integration_id}>
                {failure.account}: {failure.error}
              </p>
            ))}
          </div>
        </div>
      )}

      {query.isLoading && (
        <div className="space-y-3 p-4">
          <Skeleton className="h-4 w-28" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      )}

      {data && data.events.length === 0 && !query.isLoading && (
        <p className="p-4 text-sm text-fg-subtle">
          Nothing on your calendars in the next {days} days.
        </p>
      )}

      {visibleDays.map((day) => (
        <section key={day.key}>
          <h3
            className={cn(
              'border-b border-border bg-surface-2/50 px-3 py-1.5',
              'text-xs font-semibold uppercase tracking-wide text-fg-subtle',
            )}
          >
            {day.label}
          </h3>
          <ul>
            {day.events.map((event) => (
              <EventRow key={event.uid} event={event} onCreate={() => setCreating(event)} />
            ))}
          </ul>
        </section>
      ))}

      {hiddenEvents > 0 && !expanded && (
        <button
          type="button"
          className="w-full border-t border-border p-2.5 text-sm text-primary hover:bg-surface-2"
          onClick={() => setExpanded(true)}
        >
          Show {hiddenEvents} more event{hiddenEvents === 1 ? '' : 's'}
        </button>
      )}

      {creating && (
        <CreateFromEventDialog event={creating} onClose={() => setCreating(null)} />
      )}
    </Card>
  );
}
