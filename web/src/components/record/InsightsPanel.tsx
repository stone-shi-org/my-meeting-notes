import { useQuery } from '@tanstack/react-query';
import { ChevronDown, Loader2, MessageCircleQuestion, Radar } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Badge, Card, Select } from '@/components/ui/primitives';
import type { Caption } from '@/hooks/useLiveCaption';
import { DEFAULT_INSIGHTS_INTERVAL_SEC, useInsights } from '@/hooks/useInsights';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import type { InsightType, SettingEntry } from '@/types/api';

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
 * Sits under LiveTranscriptPanel in the wide recorder layout: periodic LLM
 * analysis of the same rolling transcript, in one of two shapes depending on
 * the selected meeting type's `kind` -- see useInsights and
 * app/services/insights.py. The type list itself is admin-extensible (see
 * app/services/insight_types.py and Settings -> Meeting types), not a fixed
 * pair, so it's fetched here rather than hardcoded into this dropdown.
 *
 * Reads its own model/interval settings rather than taking them as props:
 * every other value this panel needs (captions, enabled) comes from the
 * recorder above it, but the Insights model is admin-configured app-wide
 * state that has nothing to do with this recording, so fetching it here
 * keeps RecorderPanel from having to know LLM settings exist at all.
 */
export function InsightsPanel({ captions, enabled }: { captions: Caption[]; enabled: boolean }) {
  const [meetingType, setMeetingType] = useState<string>('');
  // Manual fold overrides, keyed by topic.title / item.question -- both
  // persist unchanged across polls (the prompts guarantee it), so a key
  // survives long enough for "the user folded this" to still mean something
  // next tick. Reset whenever the type changes: a question list from the
  // other prompt shares no identity with this one.
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

  const selectedType = types.data?.find((t) => t.slug === meetingType);
  const kind = selectedType?.kind ?? 'topics';

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

  const { items, topics, error, loading } = useInsights(
    captions,
    meetingType,
    kind,
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

  // "Newest" per shape: the one topic the conversation is on right now, or
  // the last-appended question -- everything else starts folded so a long
  // session's list doesn't bury what just happened under everything before
  // it. A manual toggle (foldOverrides) always wins over this default.
  const itemRows = useMemo(
    () =>
      items.map((item, i) => ({
        item,
        isNewest: i === items.length - 1,
      })),
    [items],
  );

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

      {enabled &&
        insightsConfigured &&
        (kind === 'questions' ? (
          itemRows.length === 0 ? (
            <p className="text-sm text-fg-faint">Listening for questions worth prepping…</p>
          ) : (
            <ul className="space-y-3">
              {itemRows.map(({ item, isNewest }, i) => {
                const key = item.question;
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
                        {item.question}
                      </p>
                    }
                  >
                    {item.answer_points.length > 0 && (
                      <ul className="list-inside list-disc space-y-0.5 text-sm text-fg-muted">
                        {item.answer_points.map((point, j) => (
                          <li key={j}>{point}</li>
                        ))}
                      </ul>
                    )}
                  </FoldCard>
                );
              })}
            </ul>
          )
        ) : topics.length === 0 ? (
          <p className="text-sm text-fg-faint">Waiting to identify the first topic…</p>
        ) : (
          <ul className="space-y-2">
            {topics.map((topic, i) => {
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
          </ul>
        ))}
    </Card>
  );
}
