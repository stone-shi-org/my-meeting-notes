/**
 * Authoring the Development provider's fake inbox and calendar.
 *
 * Only reachable when the server has `MMN_DEV_PROVIDER_ENABLED` set — the tab
 * that renders this is hidden otherwise, and every route behind it 404s anyway.
 *
 * The date control is the part that matters. An item pinned to a calendar date
 * falls out of the 60/60 match window within a couple of months and quietly
 * stops testing anything, so the default is an offset — from now, or from a
 * meeting's start.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CalendarDays, Mail, Plus, Sparkles, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Badge, Card, Input, Label, Select, Textarea } from '@/components/ui/primitives';
import { ErrorState } from '@/components/ui/states';
import { api, notifyUnauthorized } from '@/lib/api';
import { parseSseFrames } from '@/lib/chatStream';
import { cn } from '@/lib/cn';
import {
  ApiError,
  type ApiErrorBody,
  type DevDateMode,
  type DevDraft,
  type DevEmail,
  type DevEvent,
  type Integration,
  type Meeting,
  type Paginated,
  type Thread,
} from '@/types/api';

const DEV_PROVIDER = 'dev';

/** Offsets worth one click. Anything else is typed. */
const OFFSET_PRESETS = [
  { label: '2 days before', minutes: -2880 },
  { label: '1 day before', minutes: -1440 },
  { label: '1 hour after', minutes: 60 },
  { label: '1 day after', minutes: 1440 },
  { label: '2 days after', minutes: 2880 },
  { label: '1 week after', minutes: 10080 },
];

type Kind = 'emails' | 'events';

interface DateFields {
  date_mode: DevDateMode;
  at: string | null;
  offset_minutes: number;
  anchor_meeting_id: number | null;
}

const NEW_DATE: DateFields = {
  date_mode: 'relative',
  at: null,
  offset_minutes: -1440,
  anchor_meeting_id: null,
};

/* -------------------------------------------------------------------------- */
/* The date control                                                           */
/* -------------------------------------------------------------------------- */

function DateFieldset({
  value,
  meetings,
  onChange,
}: {
  value: DateFields;
  meetings: Meeting[];
  onChange: (next: DateFields) => void;
}) {
  return (
    <div className="grid gap-2 sm:grid-cols-[10rem_minmax(0,1fr)]">
      <div>
        <Label htmlFor="date-mode">When</Label>
        <Select
          id="date-mode"
          className="mt-1"
          value={value.date_mode}
          onChange={(e) => {
            const date_mode = e.target.value as DevDateMode;
            onChange({
              ...value,
              date_mode,
              // Dropping the anchor when leaving 'anchored' keeps the payload
              // honest: the server rejects an anchored item without a meeting.
              anchor_meeting_id: date_mode === 'anchored' ? value.anchor_meeting_id : null,
            });
          }}
        >
          <option value="relative">Relative to now</option>
          <option value="anchored">After a meeting</option>
          <option value="absolute">On a date</option>
        </Select>
      </div>

      <div>
        {value.date_mode === 'absolute' ? (
          <>
            <Label htmlFor="date-at">Date and time</Label>
            <Input
              id="date-at"
              className="mt-1"
              type="datetime-local"
              value={(value.at ?? '').slice(0, 16)}
              onChange={(e) =>
                onChange({ ...value, at: e.target.value ? `${e.target.value}:00Z` : null })
              }
            />
            <p className="mt-1 text-xs text-fg-subtle">
              Pinned. It will fall out of the match window in a couple of months — use one of
              the other two for a fixture you want to keep.
            </p>
          </>
        ) : (
          <>
            <Label htmlFor="date-offset">
              {value.date_mode === 'anchored' ? 'Offset from the meeting' : 'Offset from now'}
            </Label>
            <div className="mt-1 flex flex-wrap gap-2">
              <Input
                id="date-offset"
                className="w-28"
                type="number"
                aria-label="Offset in minutes"
                value={value.offset_minutes}
                onChange={(e) => onChange({ ...value, offset_minutes: Number(e.target.value) })}
              />
              <Select
                aria-label="Offset preset"
                className="w-auto"
                value=""
                onChange={(e) =>
                  onChange({ ...value, offset_minutes: Number(e.target.value) })
                }
              >
                <option value="">minutes — or pick…</option>
                {OFFSET_PRESETS.map((p) => (
                  <option key={p.minutes} value={p.minutes}>
                    {p.label}
                  </option>
                ))}
              </Select>
            </div>

            {value.date_mode === 'anchored' && (
              <Select
                aria-label="Anchor meeting"
                className="mt-2"
                value={value.anchor_meeting_id ?? ''}
                onChange={(e) =>
                  onChange({
                    ...value,
                    anchor_meeting_id: e.target.value ? Number(e.target.value) : null,
                  })
                }
              >
                <option value="">Pick a meeting…</option>
                {meetings.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.title}
                  </option>
                ))}
              </Select>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Add forms                                                                  */
/* -------------------------------------------------------------------------- */

function AddItemForm({
  kind,
  integrationId,
  meetings,
  onDone,
}: {
  kind: Kind;
  integrationId: number;
  meetings: Meeting[];
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [title, setTitle] = useState('');
  const [sender, setSender] = useState('');
  const [body, setBody] = useState('');
  const [attendees, setAttendees] = useState('');
  const [date, setDate] = useState<DateFields>(NEW_DATE);
  const [relevant, setRelevant] = useState(true);
  const [rfc2822, setRfc2822] = useState(false);
  const [allDay, setAllDay] = useState(false);
  const [repeat, setRepeat] = useState(1);

  const create = useMutation({
    mutationFn: () =>
      api.post(`/dev/integrations/${integrationId}/${kind}`, {
        ...date,
        expected_relevant: relevant,
        ...(kind === 'emails'
          ? { subject: title, sender: sender || null, snippet: body || null, rfc2822_date: rfc2822 }
          : {
              summary: title,
              description: body || null,
              attendees: attendees
                .split(',')
                .map((a) => a.trim())
                .filter(Boolean),
              all_day: allDay,
              repeat_weekly: repeat,
            }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['dev-items', integrationId, kind] });
      onDone();
    },
  });

  return (
    <form
      className="space-y-3 rounded border border-border-strong bg-surface-2/40 p-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (title.trim()) create.mutate();
      }}
    >
      <div>
        <Label htmlFor="item-title">{kind === 'emails' ? 'Subject' : 'Title'}</Label>
        <Input
          id="item-title"
          className="mt-1"
          autoFocus
          required
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder={
            kind === 'emails' ? 'Re: Oracle cutover — rollback window' : 'Atlas cutover rehearsal'
          }
        />
      </div>

      {kind === 'emails' ? (
        <div>
          <Label htmlFor="item-sender">From</Label>
          <Input
            id="item-sender"
            className="mt-1"
            value={sender}
            onChange={(e) => setSender(e.target.value)}
            placeholder="Jane Doe &lt;jane.doe@example.com&gt;"
          />
        </div>
      ) : (
        <div>
          <Label htmlFor="item-attendees">Attendees</Label>
          <Input
            id="item-attendees"
            className="mt-1"
            value={attendees}
            onChange={(e) => setAttendees(e.target.value)}
            placeholder="Jane Doe, Bob Smith — organizer first"
          />
        </div>
      )}

      <div>
        <Label htmlFor="item-body">{kind === 'emails' ? 'Snippet' : 'Description'}</Label>
        <Textarea
          id="item-body"
          className="mt-1"
          rows={2}
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
      </div>

      <DateFieldset value={date} meetings={meetings} onChange={setDate} />

      <div className="flex flex-wrap items-center gap-4 text-sm text-fg-muted">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            className="size-4 rounded border-border-strong"
            checked={relevant}
            onChange={(e) => setRelevant(e.target.checked)}
          />
          Should be matched
        </label>

        {kind === 'emails' ? (
          <label className="flex items-center gap-2" title="Gmail's format. Exercises timestamp normalisation.">
            <input
              type="checkbox"
              className="size-4 rounded border-border-strong"
              checked={rfc2822}
              onChange={(e) => setRfc2822(e.target.checked)}
            />
            RFC 2822 date
          </label>
        ) : (
          <>
            <label className="flex items-center gap-2" title="A bare date with no time.">
              <input
                type="checkbox"
                className="size-4 rounded border-border-strong"
                checked={allDay}
                onChange={(e) => setAllDay(e.target.checked)}
              />
              All day
            </label>
            <label className="flex items-center gap-2">
              Repeat weekly
              <Input
                type="number"
                className="h-8 w-16"
                aria-label="Weekly repeats"
                min={1}
                max={52}
                value={repeat}
                onChange={(e) => setRepeat(Number(e.target.value))}
              />
            </label>
          </>
        )}
      </div>

      {create.error && (
        <p role="alert" className="text-sm text-danger-ink">
          {(create.error as Error).message}
        </p>
      )}

      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" loading={create.isPending}>
          Add
        </Button>
      </div>
    </form>
  );
}

/* -------------------------------------------------------------------------- */
/* Item lists                                                                 */
/* -------------------------------------------------------------------------- */

function describeWhen(item: DevEmail | DevEvent, meetings: Meeting[]): string {
  if (item.date_mode === 'absolute') return item.at ?? 'no date';

  const minutes = item.offset_minutes ?? 0;
  const magnitude =
    Math.abs(minutes) >= 1440
      ? `${(Math.abs(minutes) / 1440).toFixed(Math.abs(minutes) % 1440 ? 1 : 0)}d`
      : `${Math.abs(minutes)}m`;
  const direction = minutes < 0 ? 'before' : 'after';

  if (item.date_mode === 'anchored') {
    const meeting = meetings.find((m) => m.id === item.anchor_meeting_id);
    return `${magnitude} ${direction} ${meeting?.title ?? 'a deleted meeting'}`;
  }
  return `${magnitude} ${direction} now`;
}

function ItemRow({
  kind,
  item,
  meetings,
  integrationId,
}: {
  kind: Kind;
  item: DevEmail | DevEvent;
  meetings: Meeting[];
  integrationId: number;
}) {
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => api.del(`/dev/${kind === 'emails' ? 'emails' : 'events'}/${item.id}`),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ['dev-items', integrationId, kind] }),
  });

  const title = 'subject' in item ? item.subject : item.summary;
  const secondary = 'sender' in item ? item.sender : (item.attendees ?? []).join(', ');

  return (
    <li className="flex items-start gap-3 border-t border-border py-2.5 first:border-t-0">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium">{title}</p>
        <p className="truncate text-xs text-fg-subtle">
          {secondary && <span className="mr-2">{secondary}</span>}
          <span>{describeWhen(item, meetings)}</span>
          {'repeat_weekly' in item && item.repeat_weekly > 1 && (
            <span className="ml-2">×{item.repeat_weekly} weekly</span>
          )}
          {'all_day' in item && item.all_day && <span className="ml-2">all day</span>}
          {'rfc2822_date' in item && item.rfc2822_date && (
            <span className="ml-2">RFC 2822</span>
          )}
        </p>
      </div>

      {/* The answer key, not a switch: nothing behaves differently either way. */}
      <Badge variant={item.expected_relevant ? 'success' : 'neutral'}>
        {item.expected_relevant ? 'should match' : 'decoy'}
      </Badge>

      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={`Delete ${title}`}
        loading={remove.isPending}
        onClick={() => remove.mutate()}
      >
        <Trash2 className="size-3.5" />
      </Button>
    </li>
  );
}

function ItemList({
  kind,
  integrationId,
  meetings,
}: {
  kind: Kind;
  integrationId: number;
  meetings: Meeting[];
}) {
  const [adding, setAdding] = useState(false);
  const query = useQuery({
    queryKey: ['dev-items', integrationId, kind],
    queryFn: () =>
      api.get<(DevEmail | DevEvent)[]>(`/dev/integrations/${integrationId}/${kind}`),
  });

  const items = query.data ?? [];

  return (
    <Card className="p-5">
      <div className="flex items-center justify-between gap-3">
        <h3 className="flex items-center gap-2 font-display text-base font-semibold">
          {kind === 'emails' ? (
            <Mail className="size-4 text-fg-faint" aria-hidden />
          ) : (
            <CalendarDays className="size-4 text-fg-faint" aria-hidden />
          )}
          {kind === 'emails' ? 'Emails' : 'Calendar events'}
          <span className="tabular text-xs font-normal text-fg-subtle">{items.length}</span>
        </h3>
        {!adding && (
          <Button size="sm" variant="secondary" onClick={() => setAdding(true)}>
            <Plus />
            Add
          </Button>
        )}
      </div>

      {query.isError && <ErrorState className="mt-3" error={query.error} />}

      {adding && (
        <div className="mt-4">
          <AddItemForm
            kind={kind}
            integrationId={integrationId}
            meetings={meetings}
            onDone={() => setAdding(false)}
          />
        </div>
      )}

      {items.length === 0 && !adding ? (
        <p className="mt-3 text-sm text-fg-subtle">
          Nothing here yet. Add a few — including some that should <em>not</em> match, which are
          the ones that tell you whether the matcher is being careful.
        </p>
      ) : (
        <ul className="mt-3">
          {items.map((item) => (
            <ItemRow
              key={item.id}
              kind={kind}
              item={item}
              meetings={meetings}
              integrationId={integrationId}
            />
          ))}
        </ul>
      )}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Generation                                                                 */
/* -------------------------------------------------------------------------- */

interface GenerateResult {
  drafts: DevDraft[];
  model: string;
}

/**
 * POSTs to `/generate` and reads the SSE reply (`progress`/`done`/`error`)
 * instead of one blocking JSON response. A batch draft is a single LLM call
 * that can run long enough for the browser or a proxy in front of it to give
 * up on a connection sitting silent the whole time -- the same reasoning as
 * `lib/chatStream.ts`'s `streamChat`, reusing its frame parser since the wire
 * format is the same `event:`/`data:` shape. `onProgress` only reports a
 * running character count: the reply is one JSON object, not prose, so there
 * is nothing to render token by token -- the count exists purely so the
 * button doesn't look frozen while the model is still working.
 */
async function streamGenerate(
  integrationId: number,
  body: { thread_id: number; count: number; additional_prompt?: string },
  onProgress: (chars: number) => void,
): Promise<GenerateResult> {
  const response = await fetch(`/api/dev/integrations/${integrationId}/generate`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    let errBody: ApiErrorBody | undefined;
    let message = response.statusText;
    let code = 'http_error';
    try {
      errBody = (await response.json()) as ApiErrorBody;
      message = errBody?.error?.message ?? message;
      code = errBody?.error?.code ?? code;
    } catch {
      /* non-JSON error body */
    }
    if (response.status === 401) notifyUnauthorized();
    throw new ApiError(response.status, code, message, errBody);
  }
  if (!response.body) throw new Error('Empty response body');

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) throw new Error('Generation stream ended without a result');

    buffer += decoder.decode(value, { stream: true });
    const { frames, rest } = parseSseFrames(buffer);
    buffer = rest;

    for (const frame of frames) {
      const data = JSON.parse(frame.data);
      if (frame.event === 'progress') onProgress(data.chars);
      else if (frame.event === 'done') return data as GenerateResult;
      else if (frame.event === 'error') throw new ApiError(502, data.code, data.message, { error: data });
    }
  }
}

function GeneratePanel({
  integrationId,
  threads,
}: {
  integrationId: number;
  threads: Thread[];
}) {
  const queryClient = useQueryClient();
  const [threadId, setThreadId] = useState<number | ''>('');
  const [count, setCount] = useState(8);
  const [additionalPrompt, setAdditionalPrompt] = useState('');
  const [drafts, setDrafts] = useState<DevDraft[] | null>(null);
  const [accepting, setAccepting] = useState(false);
  const [progress, setProgress] = useState(0);

  const generate = useMutation({
    mutationFn: () => {
      setProgress(0);
      return streamGenerate(
        integrationId,
        {
          thread_id: threadId as number,
          count,
          additional_prompt: additionalPrompt.trim() || undefined,
        },
        setProgress,
      );
    },
    onSuccess: (result) => setDrafts(result.drafts),
  });

  /** Accepting writes through the ordinary create route — one write path, so a
   * batch of nonsense costs a click rather than a cleanup. */
  async function acceptAll(chosen: DevDraft[]) {
    setAccepting(true);
    try {
      for (const draft of chosen) {
        const { kind, note: _note, ...fields } = draft;
        await api.post(`/dev/integrations/${integrationId}/${kind}`, fields);
      }
      await queryClient.invalidateQueries({ queryKey: ['dev-items', integrationId] });
      setDrafts(null);
    } finally {
      setAccepting(false);
    }
  }

  return (
    <Card className="p-5">
      <h3 className="flex items-center gap-2 font-display text-base font-semibold">
        <Sparkles className="size-4 text-fg-faint" aria-hidden />
        Generate from a thread
      </h3>
      <p className="mt-1 text-sm text-fg-subtle">
        Drafts a mix of real follow-ups, near-misses and noise around a thread. Nothing is saved
        until you accept it.
      </p>

      <div className="mt-4 space-y-3">
        <div className="flex flex-wrap items-end gap-2">
          <div className="min-w-[14rem] flex-1">
            <Label htmlFor="gen-thread">Thread</Label>
            <Select
              id="gen-thread"
              className="mt-1"
              value={threadId}
              onChange={(e) => setThreadId(e.target.value ? Number(e.target.value) : '')}
            >
              <option value="">Pick a thread…</option>
              {threads.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.title}
                </option>
              ))}
            </Select>
          </div>
          <div>
            <Label htmlFor="gen-count">How many</Label>
            <Input
              id="gen-count"
              className="mt-1 w-20"
              type="number"
              min={1}
              max={20}
              value={count}
              onChange={(e) => setCount(Number(e.target.value))}
            />
          </div>
          <Button
            variant="secondary"
            disabled={!threadId}
            loading={generate.isPending}
            onClick={() => generate.mutate()}
          >
            Generate
          </Button>
        </div>

        <div>
          <Label htmlFor="gen-additional-prompt">Additional prompt (optional)</Label>
          <Textarea
            id="gen-additional-prompt"
            className="mt-1 min-h-16 resize-none"
            placeholder="e.g. Include an email about budget approval or vendor quotes..."
            value={additionalPrompt}
            onChange={(e) => setAdditionalPrompt(e.target.value)}
          />
        </div>
      </div>

      {generate.isPending && progress > 0 && (
        <p className="mt-3 text-sm text-fg-subtle">Generating… {progress} characters so far</p>
      )}

      {generate.error && (
        <p role="alert" className="mt-3 text-sm text-danger-ink">
          {(generate.error as Error).message}
        </p>
      )}

      {drafts && drafts.length === 0 && (
        <p className="mt-3 text-sm text-fg-subtle">
          The model returned nothing usable. Try again, or add items by hand.
        </p>
      )}

      {drafts && drafts.length > 0 && (
        <DraftReview
          drafts={drafts}
          busy={accepting}
          onCancel={() => setDrafts(null)}
          onAccept={acceptAll}
        />
      )}
    </Card>
  );
}

function DraftReview({
  drafts,
  busy,
  onAccept,
  onCancel,
}: {
  drafts: DevDraft[];
  busy: boolean;
  onAccept: (chosen: DevDraft[]) => void;
  onCancel: () => void;
}) {
  const [keep, setKeep] = useState<boolean[]>(() => drafts.map(() => true));
  const chosen = drafts.filter((_, i) => keep[i]);

  return (
    <div className="mt-4 rounded border border-border-strong">
      <ul>
        {drafts.map((draft, i) => (
          <li
            key={i}
            className={cn(
              'flex items-start gap-3 border-t border-border p-3 first:border-t-0',
              !keep[i] && 'opacity-50',
            )}
          >
            <input
              type="checkbox"
              className="mt-1 size-4 rounded border-border-strong"
              aria-label={`Keep ${draft.subject ?? draft.summary}`}
              checked={keep[i]}
              onChange={(e) =>
                setKeep((prev) => prev.map((v, j) => (j === i ? e.target.checked : v)))
              }
            />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{draft.subject ?? draft.summary}</p>
              <p className="truncate text-xs text-fg-subtle">
                {draft.kind === 'emails' ? draft.sender : (draft.attendees ?? []).join(', ')}
                {draft.note && <span className="ml-2 italic">{draft.note}</span>}
              </p>
            </div>
            <Badge variant={draft.expected_relevant ? 'success' : 'neutral'}>
              {draft.expected_relevant ? 'should match' : 'decoy'}
            </Badge>
          </li>
        ))}
      </ul>

      <div className="flex justify-end gap-2 border-t border-border p-3">
        <Button variant="ghost" onClick={onCancel}>
          Discard all
        </Button>
        <Button
          variant="primary"
          loading={busy}
          disabled={chosen.length === 0}
          onClick={() => onAccept(chosen)}
        >
          Add {chosen.length} item{chosen.length === 1 ? '' : 's'}
        </Button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* The tab                                                                    */
/* -------------------------------------------------------------------------- */

export function DevDataPanel() {
  const [selected, setSelected] = useState<number | null>(null);

  const integrations = useQuery({
    queryKey: ['integrations'],
    queryFn: () => api.get<Integration[]>('/integrations'),
  });
  const threads = useQuery({
    queryKey: ['threads', { all: true }],
    queryFn: () => api.get<Paginated<Thread>>('/threads', { page_size: 100 }),
  });

  const accounts = (integrations.data ?? []).filter((i) => i.provider === DEV_PROVIDER);
  const integrationId = selected ?? accounts[0]?.id ?? null;

  // Every meeting the user can anchor to, flattened out of the thread list.
  const meetings = useQuery({
    queryKey: ['dev-anchor-meetings'],
    queryFn: () => api.get<Paginated<Meeting>>('/meetings', { page_size: 100 }),
  });

  if (integrations.isError) return <ErrorState error={integrations.error} />;

  return (
    <div className="space-y-4">
      <Card className="p-5">
        <h2 className="font-display text-lg font-semibold">Development data</h2>
        <p className="mt-1 text-sm text-fg-subtle">
          A calendar and inbox you write yourself, so matching and the follow-up sweep can be
          exercised without connecting a real account. Everything here goes through the same
          pipeline real providers do.
        </p>

        {accounts.length === 0 ? (
          <p className="mt-4 rounded border border-dashed border-border p-4 text-sm text-fg-subtle">
            No Development account yet. Add one under{' '}
            <strong>Settings → Integrations → Development (fake data)</strong>, then come back.
          </p>
        ) : accounts.length > 1 ? (
          <div className="mt-4 max-w-xs">
            <Label htmlFor="dev-account">Account</Label>
            <Select
              id="dev-account"
              className="mt-1"
              value={integrationId ?? ''}
              onChange={(e) => setSelected(Number(e.target.value))}
            >
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.account_label || a.account_key}
                </option>
              ))}
            </Select>
          </div>
        ) : null}
      </Card>

      {integrationId !== null && (
        <>
          <GeneratePanel integrationId={integrationId} threads={threads.data?.items ?? []} />
          <ItemList
            kind="emails"
            integrationId={integrationId}
            meetings={meetings.data?.items ?? []}
          />
          <ItemList
            kind="events"
            integrationId={integrationId}
            meetings={meetings.data?.items ?? []}
          />
        </>
      )}
    </div>
  );
}
