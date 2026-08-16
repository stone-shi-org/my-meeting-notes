import { useQuery } from '@tanstack/react-query';
import { Loader2, MessageCircleQuestion, Radar } from 'lucide-react';
import { useState } from 'react';
import { Badge, Card, Select } from '@/components/ui/primitives';
import type { Caption } from '@/hooks/useLiveCaption';
import { DEFAULT_INSIGHTS_INTERVAL_SEC, type MeetingType, useInsights } from '@/hooks/useInsights';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import type { SettingEntry } from '@/types/api';

/**
 * Sits under LiveTranscriptPanel in the wide recorder layout: periodic LLM
 * analysis of the same rolling transcript, in one of two shapes depending on
 * meeting type -- see useInsights and app/services/insights.py.
 *
 * Reads its own model/interval settings rather than taking them as props:
 * every other value this panel needs (captions, enabled) comes from the
 * recorder above it, but the Insights model is admin-configured app-wide
 * state that has nothing to do with this recording, so fetching it here
 * keeps RecorderPanel from having to know LLM settings exist at all.
 */
export function InsightsPanel({ captions, enabled }: { captions: Caption[]; enabled: boolean }) {
  const [meetingType, setMeetingType] = useState<MeetingType>('general');

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
    enabled && insightsConfigured,
    intervalSec,
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
          onChange={(e) => setMeetingType(e.target.value as MeetingType)}
        >
          <option value="general">General — live topic summary</option>
          <option value="interview">Interview — detect questions, draft answers</option>
        </Select>
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
        (meetingType === 'interview' ? (
          items.length === 0 ? (
            <p className="text-sm text-fg-faint">Listening for questions worth prepping…</p>
          ) : (
            <ul className="space-y-3">
              {items.map((item, i) => (
                <li key={i} className="rounded-md border border-border bg-surface-2/50 p-3">
                  <p className="flex items-start gap-1.5 text-sm font-medium">
                    <MessageCircleQuestion
                      className="mt-0.5 size-3.5 shrink-0 text-primary"
                      aria-hidden
                    />
                    {item.question}
                  </p>
                  {item.answer_points.length > 0 && (
                    <ul className="mt-1.5 list-inside list-disc space-y-0.5 text-sm text-fg-muted">
                      {item.answer_points.map((point, j) => (
                        <li key={j}>{point}</li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          )
        ) : topics.length === 0 ? (
          <p className="text-sm text-fg-faint">Waiting to identify the first topic…</p>
        ) : (
          <ul className="space-y-2">
            {topics.map((topic, i) => (
              <li
                key={i}
                className={cn(
                  'rounded-md border p-3',
                  topic.current
                    ? 'border-primary/40 bg-primary-soft/30'
                    : 'border-border bg-surface-2/50',
                )}
              >
                <p className="flex items-center gap-1.5 text-sm font-medium">
                  <Radar className="size-3.5 shrink-0 text-fg-faint" aria-hidden />
                  {topic.title}
                  {topic.current && (
                    <Badge variant="primary" size="sm">
                      current
                    </Badge>
                  )}
                </p>
                <p className="mt-1 text-sm text-fg-muted">{topic.summary}</p>
              </li>
            ))}
          </ul>
        ))}
    </Card>
  );
}
