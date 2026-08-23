import { useQuery } from '@tanstack/react-query';
import {
  CheckSquare,
  ChevronDown,
  Loader2,
  MessageCircleQuestion,
  Radar,
} from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Badge, Card, Select } from '@/components/ui/primitives';
import type { Caption } from '@/hooks/useLiveCaption';
import { DEFAULT_INSIGHTS_INTERVAL_SEC, useInsights } from '@/hooks/useInsights';
import type { InsightActionItem } from '@/hooks/useInsights';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import type { InsightType, SettingEntry } from '@/types/api';

// Only the most recent entries are worth surfacing live -- a long recording
// would otherwise grow each section unbounded. The full list still round-
// trips as previous_topics/previous_questions/previous_action_items (see
// useInsights), so capping the *display* here never affects what the model
// carries forward.
const MAX_VISIBLE = 5;

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
export function InsightsPanel({ captions, enabled }: { captions: Caption[]; enabled: boolean }) {
  const [meetingType, setMeetingType] = useState<string>('');
  // Manual fold overrides, keyed by topic.title / question.question -- both
  // persist unchanged across polls (the prompts guarantee it), so a key
  // survives long enough for "the user folded this" to still mean something
  // next tick. Reset whenever the type changes: a list from the other
  // prompt shares no identity with this one.
  const [foldOverrides, setFoldOverrides] = useState<Map<string, boolean>>(new Map());

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
  }, [meetingType]);

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

  const { topics, questions, actionItems, error, loading } = useInsights(
    captions,
    meetingType,
    enabled && insightsConfigured && !!meetingType,
    intervalSec,
  );

  const toggleFold = (key: string, defaultCollapsed: boolean) =>
    setFoldOverrides((prev) => {
      const next = new Map(prev);
      const current = prev.get(key) ?? defaultCollapsed;
      next.set(key, !current);
      return next;
    });

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
        {loading && <Loader2 className="size-4 animate-spin text-fg-faint" aria-hidden />}
      </div>
      <p className="text-xs text-fg-subtle">
        Live analysis of the rough transcript above, refreshed every {intervalSec}s while
        recording. Not saved, and separate from the real summary built after you stop.
      </p>

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

      {insightsConfigured && !enabled && (
        <p className="text-xs text-fg-subtle">
          Turn on "Show live captions" and start recording to begin analysis.
        </p>
      )}

      {error && (
        <p role="alert" className="text-xs text-danger-ink">
          {error}
        </p>
      )}

      {enabled && insightsConfigured && (
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
                      <ul className="list-inside list-disc space-y-0.5 text-sm text-fg-muted">
                        {question.ai_answer_points.map((point, j) => (
                          <li key={j}>{point}</li>
                        ))}
                      </ul>
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
