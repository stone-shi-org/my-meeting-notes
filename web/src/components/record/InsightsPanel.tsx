import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  CheckSquare,
  ChevronDown,
  Eraser,
  Loader2,
  MessageCircleQuestion,
  NotebookPen,
  Plus,
  Radar,
  X,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Badge, Card, Input, Select, Spinner } from '@/components/ui/primitives';
import type { Caption } from '@/hooks/useLiveCaption';
import {
  DEFAULT_INSIGHTS_INTERVAL_SEC,
  useInsights,
  type InsightActionItem,
  type InsightQuestion,
  type InsightTopic,
} from '@/hooks/useInsights';
import { useNotes } from '@/hooks/useNotes';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import type { InsightType, Paginated, SettingEntry, Thread } from '@/types/api';

// Only the most recent entries are worth surfacing live -- a long recording
// would otherwise grow each section unbounded. The full list still round-
// trips as previous_topics/previous_questions/previous_action_items (see
// useInsights), so capping the *display* here never affects what the model
// carries forward, or what "Add to notes" saves (that reads the full lists,
// not this capped view).
const MAX_VISIBLE = 5;

/** How long the "Saved to ..." confirmation stays up -- same duration
 * MessageBubble's chat "Add to note" flash uses. */
const FLASH_MS = 4000;

/**
 * One topic or question card, foldable -- same rotate-chevron disclosure
 * ThreadGroups.tsx uses for its section headers, inlined here rather than
 * pulled into a shared primitive (there isn't one anywhere in this codebase;
 * see JobDock.tsx for the other hand-rolled instance).
 *
 * `collapsed` is the *effective* state the parent computed (a manual
 * override if the user has touched this card, else "collapse everything
 * but the newest") -- this component only reports toggle clicks upward, it
 * does not own the state itself.
 */
function FoldCard({
  collapsed,
  onToggle,
  accent,
  summary,
  children,
}: {
  collapsed: boolean;
  onToggle: () => void;
  accent?: boolean;
  summary: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <li
      className={cn(
        'rounded-md border p-3',
        accent ? 'border-primary/40 bg-primary-soft/30' : 'border-border bg-surface-2/50',
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!collapsed}
        className="flex w-full items-start justify-between gap-2 text-left"
      >
        {summary}
        <ChevronDown
          className={cn(
            'mt-0.5 size-3.5 shrink-0 text-fg-faint transition-transform duration-fast',
            collapsed && '-rotate-90',
          )}
          aria-hidden
        />
      </button>
      {!collapsed && <div className="mt-1.5">{children}</div>}
    </li>
  );
}

/**
 * Heading + fixed-height scrollable list -- the same overflow-y-auto recipe
 * LiveTranscriptPanel.tsx and TranscriptPage.tsx use for their own panels,
 * sized down for a sub-section rather than the whole card. `count` is the
 * full (un-capped) list length, shown next to the heading so "showing the
 * last 5 of 12" is visible even though only 5 render.
 */
function InsightSection({
  title,
  count,
  emptyText,
  children,
}: {
  title: string;
  count: number;
  emptyText: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <h3 className="flex items-baseline gap-1.5 text-xs font-semibold uppercase tracking-wide text-fg-subtle">
        {title}
        {count > 0 && <span className="font-normal text-fg-faint">({count})</span>}
      </h3>
      {count === 0 ? (
        <p className="mt-1.5 text-sm text-fg-faint">{emptyText}</p>
      ) : (
        <ul className="mt-1.5 max-h-56 space-y-2 overflow-y-auto pr-1">{children}</ul>
      )}
    </div>
  );
}

/** Markdown body for "Add to notes" -- reads the full (un-capped) lists, not
 * the last-5 slice the panel renders, so saving a note never loses anything
 * just because the display trimmed it. */
function insightsToMarkdown(
  topics: InsightTopic[],
  questions: InsightQuestion[],
  actionItems: InsightActionItem[],
): string {
  const sections: string[] = [];

  if (topics.length > 0) {
    sections.push(
      ['## Topics', ...topics.map((t) => `- **${t.title}** — ${t.summary}`)].join('\n'),
    );
  }

  if (questions.length > 0) {
    sections.push(
      [
        '## Questions',
        ...questions.map((q) => {
          const lines = [`- **${q.question}**`];
          if (q.ai_answer_points.length > 0) {
            lines.push(`  - Answer suggestion: ${q.ai_answer_points.join('; ')}`);
          }
          if (q.discussion) lines.push(`  - Discussed: ${q.discussion}`);
          return lines.join('\n');
        }),
      ].join('\n'),
    );
  }

  if (actionItems.length > 0) {
    sections.push(
      [
        '## Action items',
        ...actionItems.map((a) => `- [ ] ${a.text}${a.owner ? ` (${a.owner})` : ''}`),
      ].join('\n'),
    );
  }

  return sections.join('\n\n');
}

/**
 * "New note, or add to one of these" for a thread that's already resolved --
 * same shape as MessageBubble's chat NotePicker, but `source: 'manual'`
 * (this is a saved analysis snapshot, not an AI chat reply) and no
 * model/question fields to carry.
 */
function InsightsNoteStep({
  body,
  threadId,
  onClose,
  onSaved,
  containerRef,
}: {
  body: string;
  threadId: string;
  onClose: () => void;
  onSaved: (title: string) => void;
  containerRef: React.RefObject<HTMLDivElement>;
}) {
  const { list, create, append } = useNotes({ kind: 'thread', threadId });
  const pending = create.isPending || append.isPending;
  const error = (create.error ?? append.error) as Error | null;
  const notes = list.data ?? [];

  return (
    <div
      ref={containerRef}
      role="group"
      aria-label="Save these insights as a note"
      className="mt-1 overflow-hidden rounded-lg border border-border bg-surface shadow-sm"
    >
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <p className="flex-1 text-xs font-semibold">Save these insights</p>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="rounded p-0.5 text-fg-faint hover:bg-surface-2 hover:text-fg"
        >
          <X className="size-3.5" aria-hidden />
        </button>
      </div>

      <button
        type="button"
        disabled={pending}
        onClick={() =>
          create.mutate(
            { body, source: 'manual' },
            { onSuccess: (note) => onSaved(note.title) },
          )
        }
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-surface-2 disabled:opacity-50"
      >
        {create.isPending ? (
          <Spinner className="size-3.5 shrink-0" />
        ) : (
          <Plus className="size-3.5 shrink-0 text-primary" aria-hidden />
        )}
        <span className="min-w-0 flex-1">
          New note
          <span className="block text-2xs text-fg-subtle">
            {create.isPending ? 'Naming it…' : 'Titled for you from the content'}
          </span>
        </span>
      </button>

      {notes.length > 0 && (
        <>
          <p className="border-t border-border px-3 pb-1 pt-2 text-2xs font-semibold uppercase tracking-wide text-fg-subtle">
            Or add to
          </p>
          <ul className="max-h-48 overflow-y-auto pb-1">
            {notes.map((note) => (
              <li key={note.id}>
                <button
                  type="button"
                  disabled={pending}
                  onClick={() =>
                    append.mutate(
                      { note, body },
                      { onSuccess: (updated) => onSaved(updated.title) },
                    )
                  }
                  className="block w-full truncate px-3 py-1.5 text-left text-sm hover:bg-surface-2 disabled:opacity-50"
                >
                  {note.title}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      {error && <p className="px-3 pb-2 text-2xs text-danger-ink">{error.message}</p>}
    </div>
  );
}

/**
 * "Which thread?" -- unlike MessageBubble's chat "Add to note" (which either
 * already knows its thread/meeting, or -- home chat only -- asks among
 * *existing* threads), a recording in `NewMeetingPage` has no thread of its
 * own at all yet (see that page: nothing is created server-side until Stop
 * + Submit). So this step also offers creating one inline, via the same
 * `POST /threads` ThreadsPage's "New thread" dialog uses.
 */
function InsightsThreadStep({
  onPicked,
  onClose,
  containerRef,
}: {
  onPicked: (threadId: string) => void;
  onClose: () => void;
  containerRef: React.RefObject<HTMLDivElement>;
}) {
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [newTitle, setNewTitle] = useState('');

  // Same query key ThreadNotePicker/MoveToThread use for their own thread
  // pickers, so this shares their cached fetch instead of paying for its own.
  const threads = useQuery({
    queryKey: ['threads', 'picker'],
    queryFn: () => api.get<Paginated<Thread>>('/threads', { page_size: 200 }),
  });

  const create = useMutation({
    mutationFn: () => api.post<Thread>('/threads', { title: newTitle, description: null }),
    onSuccess: (thread) => {
      void queryClient.invalidateQueries({ queryKey: ['threads'] });
      onPicked(String(thread.id));
    },
  });

  return (
    <div
      ref={containerRef}
      role="group"
      aria-label="Choose a thread for these insights"
      className="mt-1 overflow-hidden rounded-lg border border-border bg-surface shadow-sm"
    >
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <p className="flex-1 text-xs font-semibold">Save to which thread?</p>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="rounded p-0.5 text-fg-faint hover:bg-surface-2 hover:text-fg"
        >
          <X className="size-3.5" aria-hidden />
        </button>
      </div>

      {creating ? (
        <div className="space-y-2 p-3">
          <Input
            autoFocus
            placeholder="New thread title"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
          />
          {create.error && (
            <p className="text-2xs text-danger-ink">{(create.error as Error).message}</p>
          )}
          <div className="flex items-center gap-2">
            <Button
              type="button"
              size="sm"
              variant="primary"
              disabled={!newTitle.trim()}
              loading={create.isPending}
              onClick={() => create.mutate()}
            >
              Create
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setCreating(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setCreating(true)}
          className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-surface-2"
        >
          <Plus className="size-3.5 shrink-0 text-primary" aria-hidden />
          New thread
        </button>
      )}

      {threads.isLoading && (
        <p className="border-t border-border px-3 py-2 text-xs text-fg-subtle">
          Loading threads…
        </p>
      )}

      {threads.data && threads.data.items.length > 0 && (
        <ul className="max-h-48 overflow-y-auto border-t border-border py-1">
          {threads.data.items.map((t) => (
            <li key={t.id}>
              <button
                type="button"
                onClick={() => onPicked(String(t.id))}
                className="block w-full truncate px-3 py-1.5 text-left text-sm hover:bg-surface-2"
              >
                {t.title}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Two-step "Add to notes": pick or create a thread, then new-note-or-append
 * -- see InsightsThreadStep and InsightsNoteStep's own doc comments. */
function InsightsSavePicker({
  body,
  onClose,
  onSaved,
}: {
  body: string;
  onClose: () => void;
  onSaved: (title: string) => void;
}) {
  const [threadId, setThreadId] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKeyDown);
    ref.current?.scrollIntoView({ block: 'nearest' });
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  if (threadId) {
    return (
      <InsightsNoteStep
        body={body}
        threadId={threadId}
        onClose={onClose}
        onSaved={onSaved}
        containerRef={ref}
      />
    );
  }

  return <InsightsThreadStep onPicked={setThreadId} onClose={onClose} containerRef={ref} />;
}

/**
 * Sits under LiveTranscriptPanel in the wide recorder layout: periodic LLM
 * analysis of the same rolling transcript, always in three sections -- see
 * useInsights and app/services/insights.py. The type list itself is
 * admin-extensible (see app/services/insight_types.py and Settings ->
 * Meeting types), not a fixed pair, so it's fetched here rather than
 * hardcoded into this dropdown. Every type produces the same combined shape
 * now (topics + questions + action_items); what differs between types is
 * tone/framing (a plain meeting vs. an interview), not what sections show.
 *
 * Reads its own model/interval settings rather than taking them as props:
 * every other value this panel needs (captions, enabled) comes from the
 * recorder above it, but the Insights model is admin-configured app-wide
 * state that has nothing to do with this recording, so fetching it here
 * keeps RecorderPanel from having to know LLM settings exist at all.
 */
export function InsightsPanel({
  captions,
  enabled,
  sessionKey,
}: {
  captions: Caption[];
  enabled: boolean;
  /** Bumped by RecorderPanel only when the user deliberately starts a new
   * recording (Start / "Record again") -- see useInsights' sessionKey
   * param. Stopping on its own must not clear anything, which is the whole
   * point of this prop existing: `enabled` going false is not enough. */
  sessionKey?: number;
}) {
  const [meetingType, setMeetingType] = useState<string>('');
  // Manual fold overrides, keyed by topic.title / question.question -- both
  // persist unchanged across polls (the prompts guarantee it), so a key
  // survives long enough for "the user folded this" to still mean something
  // next tick. Reset whenever the type or recording session changes: a list
  // from a different prompt or a fresh recording shares no identity with
  // whatever was folded before.
  const [foldOverrides, setFoldOverrides] = useState<Map<string, boolean>>(new Map());
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  const types = useQuery({
    queryKey: ['insight-types'],
    queryFn: () => api.get<InsightType[]>('/insight-types'),
    staleTime: 60_000,
  });

  // Default to "general" if it's still there, else whatever sorts first --
  // an admin who deletes it entirely must not leave the picker stuck on a
  // slug that no longer exists.
  useEffect(() => {
    if (meetingType || !types.data?.length) return;
    setMeetingType(types.data.find((t) => t.slug === 'general')?.slug ?? types.data[0].slug);
  }, [meetingType, types.data]);

  useEffect(() => {
    setFoldOverrides(new Map());
  }, [meetingType, sessionKey]);

  useEffect(() => {
    if (!saved) return;
    const timer = window.setTimeout(() => setSaved(null), FLASH_MS);
    return () => window.clearTimeout(timer);
  }, [saved]);

  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.get<{ settings: Record<string, SettingEntry> }>('/settings'),
    // This panel only mounts during an active recording -- fresh enough not
    // to poll, but a Settings-page edit made earlier in the same session
    // should still be picked up on the next recording without a reload.
    staleTime: 60_000,
  });

  const configuredModel = settings.data?.settings.insights_model?.value;
  const insightsConfigured = typeof configuredModel === 'string' && configuredModel.length > 0;
  const intervalSec =
    Number(settings.data?.settings.insights_interval_sec?.value) || DEFAULT_INSIGHTS_INTERVAL_SEC;

  const { topics, questions, actionItems, error, loading, clear } = useInsights(
    captions,
    meetingType,
    enabled && insightsConfigured && !!meetingType,
    intervalSec,
    sessionKey,
  );

  const toggleFold = (key: string, defaultCollapsed: boolean) =>
    setFoldOverrides((prev) => {
      const next = new Map(prev);
      const current = prev.get(key) ?? defaultCollapsed;
      next.set(key, !current);
      return next;
    });

  // Stopping must not hide what was gathered -- only "nothing has ever come
  // back yet" (never enabled, or enabled but truly empty so far) falls back
  // to the "turn on captions" hint instead of the three sections.
  const hasAnyResults = topics.length > 0 || questions.length > 0 || actionItems.length > 0;

  function handleClean() {
    clear();
    setFoldOverrides(new Map());
  }

  const notesBody = useMemo(
    () => insightsToMarkdown(topics, questions, actionItems),
    [topics, questions, actionItems],
  );

  // "Newest" per list: the one topic the conversation is on right now, or
  // the last-appended question -- everything else starts folded so a long
  // session's list doesn't bury what just happened under everything before
  // it. A manual toggle (foldOverrides) always wins over this default.
  const visibleTopics = useMemo(() => topics.slice(-MAX_VISIBLE), [topics]);
  const visibleQuestions = useMemo(
    () =>
      questions.slice(-MAX_VISIBLE).map((question, i, arr) => ({
        question,
        isNewest: i === arr.length - 1,
      })),
    [questions],
  );
  const visibleActionItems = useMemo(() => actionItems.slice(-MAX_VISIBLE), [actionItems]);

  return (
    <Card className="space-y-3 p-5">
      <div className="flex items-center justify-between gap-3">
        <h2 className="font-display text-lg font-semibold">Insights</h2>
        <div className="flex items-center gap-1">
          {loading && <Loader2 className="size-4 animate-spin text-fg-faint" aria-hidden />}
          {hasAnyResults && (
            <>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={handleClean}
                title="Clear the topics/questions/action items gathered so far"
              >
                <Eraser />
                Clean
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => setSaving((v) => !v)}>
                <NotebookPen />
                Add to notes
              </Button>
            </>
          )}
        </div>
      </div>
      <p className="text-xs text-fg-subtle">
        Live analysis of the rough transcript above, refreshed every {intervalSec}s while
        recording. Not part of the real summary built after you stop, but stays on screen (and
        savable as a note) after you do -- starting a new recording clears it.
      </p>

      {saved && (
        <p role="status" className="text-xs text-success-ink">
          Saved to “{saved}”
        </p>
      )}
      {saving && (
        <InsightsSavePicker
          body={notesBody}
          onClose={() => setSaving(false)}
          onSaved={(title) => {
            setSaved(title);
            setSaving(false);
          }}
        />
      )}

      <div>
        <label htmlFor="insights-meeting-type" className="text-xs font-medium text-fg-subtle">
          Meeting type
        </label>
        <Select
          id="insights-meeting-type"
          className="mt-1.5"
          value={meetingType}
          disabled={!types.data?.length}
          onChange={(e) => setMeetingType(e.target.value)}
        >
          {types.data?.map((t) => (
            <option key={t.slug} value={t.slug}>
              {t.name}
            </option>
          ))}
        </Select>
        {!types.isLoading && !types.data?.length && (
          <p className="mt-1 text-xs text-fg-subtle">
            No meeting types configured. Add one in Settings → Meeting types.
          </p>
        )}
      </div>

      {!settings.isLoading && !insightsConfigured && (
        <p className="text-xs text-fg-subtle">
          No Insights model is configured. Set one in Settings → LLM to turn this on.
        </p>
      )}

      {insightsConfigured && !enabled && !hasAnyResults && (
        <p className="text-xs text-fg-subtle">
          Turn on "Show live captions" and start recording to begin analysis.
        </p>
      )}

      {error && (
        <p role="alert" className="text-xs text-danger-ink">
          {error}
        </p>
      )}

      {insightsConfigured && (enabled || hasAnyResults) && (
        <div className="space-y-4">
          <InsightSection
            title="Topics"
            count={topics.length}
            emptyText="Waiting to identify the first topic…"
          >
            {visibleTopics.map((topic, i) => {
              const key = topic.title;
              const collapsed = foldOverrides.get(key) ?? !topic.current;
              return (
                <FoldCard
                  key={i}
                  collapsed={collapsed}
                  onToggle={() => toggleFold(key, !topic.current)}
                  accent={topic.current}
                  summary={
                    <p className="flex items-center gap-1.5 text-sm font-medium">
                      <Radar className="size-3.5 shrink-0 text-fg-faint" aria-hidden />
                      {topic.title}
                      {topic.current && (
                        <Badge variant="primary" size="sm">
                          current
                        </Badge>
                      )}
                    </p>
                  }
                >
                  <p className="text-sm text-fg-muted">{topic.summary}</p>
                </FoldCard>
              );
            })}
          </InsightSection>

          <InsightSection
            title="Questions"
            count={questions.length}
            emptyText="Listening for questions worth prepping…"
          >
            {visibleQuestions.map(({ question, isNewest }, i) => {
              const key = question.question;
              const collapsed = foldOverrides.get(key) ?? !isNewest;
              return (
                <FoldCard
                  key={i}
                  collapsed={collapsed}
                  onToggle={() => toggleFold(key, !isNewest)}
                  summary={
                    <p className="flex items-start gap-1.5 text-sm font-medium">
                      <MessageCircleQuestion
                        className="mt-0.5 size-3.5 shrink-0 text-primary"
                        aria-hidden
                      />
                      {question.question}
                    </p>
                  }
                >
                  <div className="space-y-1.5">
                    {question.ai_answer_points.length > 0 && (
                      <div>
                        <p className="text-xs font-semibold uppercase tracking-wide text-fg-subtle">
                          Answer suggestion
                        </p>
                        <ul className="mt-0.5 list-inside list-disc space-y-0.5 text-sm text-fg-muted">
                          {question.ai_answer_points.map((point, j) => (
                            <li key={j}>{point}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {question.discussion && (
                      <p className="text-sm text-fg-muted">
                        <span className="font-medium text-fg-subtle">Discussed: </span>
                        {question.discussion}
                      </p>
                    )}
                  </div>
                </FoldCard>
              );
            })}
          </InsightSection>

          <InsightSection
            title="Action items"
            count={actionItems.length}
            emptyText="No commitments or follow-ups spotted yet…"
          >
            {visibleActionItems.map((item, i) => (
              <ActionItemRow key={i} item={item} />
            ))}
          </InsightSection>
        </div>
      )}
    </Card>
  );
}

/** Action items are one-liners, so unlike topics/questions they aren't
 * foldable -- there's nothing to hide underneath. */
function ActionItemRow({ item }: { item: InsightActionItem }) {
  return (
    <li className="flex items-start gap-1.5 rounded-md border border-border bg-surface-2/50 p-3 text-sm">
      <CheckSquare className="mt-0.5 size-3.5 shrink-0 text-fg-faint" aria-hidden />
      <span className="flex-1 text-fg-muted">{item.text}</span>
      {item.owner && (
        <Badge size="sm" className="shrink-0">
          {item.owner}
        </Badge>
      )}
    </li>
  );
}
