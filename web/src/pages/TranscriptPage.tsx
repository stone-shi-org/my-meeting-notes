import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import DOMPurify from 'dompurify';
import { CheckSquare, RefreshCw, Square, Sparkles } from 'lucide-react';
import { marked } from 'marked';
import { useEffect, useMemo, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { Badge, Card, Input, Select, Skeleton } from '@/components/ui/primitives';
import { ErrorState } from '@/components/ui/states';
import { PlayerBar } from '@/components/transcript/PlayerBar';
import { TranscriptView } from '@/components/transcript/TranscriptView';
import { McpMatchPanel } from '@/components/mcp/McpMatchPanel';
import { PlayerProvider, usePlayer } from '@/player/PlayerProvider';
import { usePlayerStore } from '@/player/playerStore';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import { initials, speakerVars } from '@/lib/speakerColors';
import { watchJob } from '@/hooks/useJob';
import { ApiError, type ActionItem, type Meeting, type Summary, type Transcript } from '@/types/api';

type Tab = 'summary' | 'actions' | 'speakers';

function SpeakerLegend({
  meetingId,
  transcript,
}: {
  meetingId: number;
  transcript: Transcript;
}) {
  const queryClient = useQueryClient();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<string | null>(null);

  const rename = useMutation({
    mutationFn: (payload: { speaker_id: string; display_name: string }) =>
      api.put(`/meetings/${meetingId}/speakers`, [payload]),
    onMutate: async ({ speaker_id, display_name }) => {
      const key = ['transcript', String(meetingId)];
      await queryClient.cancelQueries({ queryKey: key });
      const previous = queryClient.getQueryData<Transcript>(key);

      // Optimistic: renaming is a map edit, never a walk over the segments.
      queryClient.setQueryData<Transcript>(key, (old) =>
        old
          ? {
              ...old,
              speakers: old.speakers.map((s) =>
                s.id === speaker_id ? { ...s, display_name: display_name || null } : s,
              ),
              segments: old.segments.map((seg) =>
                seg.speaker === speaker_id
                  ? { ...seg, speaker_name: display_name || speaker_id }
                  : seg,
              ),
            }
          : old,
      );
      return { previous };
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) {
        queryClient.setQueryData(['transcript', String(meetingId)], context.previous);
      }
    },
    onSuccess: (_data, vars) => {
      setSaved(vars.speaker_id);
      window.setTimeout(() => setSaved(null), 1500);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ['transcript', String(meetingId)] });
      void queryClient.invalidateQueries({ queryKey: ['summary', String(meetingId)] });
    },
  });

  function commit(speakerId: string) {
    const value = drafts[speakerId];
    if (value === undefined) return;
    rename.mutate({ speaker_id: speakerId, display_name: value.trim() });
  }

  return (
    <ul className="space-y-2">
      {transcript.speakers.map((speaker) => {
        const value = drafts[speaker.id] ?? speaker.display_name ?? '';
        const share = speaker.share ?? 0;
        return (
          <li key={speaker.id} className="flex items-center gap-2" style={speakerVars(speaker.id)}>
            <span
              aria-hidden
              className="grid size-6 shrink-0 place-items-center rounded-full text-[10px] font-semibold"
              style={{
                backgroundColor: 'color-mix(in srgb, var(--sp) 18%, transparent)',
                color: 'var(--sp-ink)',
              }}
            >
              {initials(speaker.display_name || speaker.id)}
            </span>

            <Input
              className="h-8 flex-1 border-transparent bg-transparent px-2 hover:border-border-strong focus:border-border-strong"
              value={value}
              placeholder={speaker.id}
              aria-label={`Name for ${speaker.id}`}
              onChange={(e) => setDrafts((d) => ({ ...d, [speaker.id]: e.target.value }))}
              onBlur={() => commit(speaker.id)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.currentTarget.blur();
                } else if (e.key === 'Escape') {
                  setDrafts((d) => {
                    const next = { ...d };
                    delete next[speaker.id];
                    return next;
                  });
                  e.currentTarget.blur();
                }
              }}
            />

            <span className="w-16 shrink-0 text-right text-xs text-fg-subtle tabular">
              {speaker.duration_human} · {Math.round(share * 100)}%
            </span>

            {saved === speaker.id && (
              <span className="text-xs text-success-ink" role="status">
                ✓
              </span>
            )}
          </li>
        );
      })}
      <li className="pt-1 text-xs text-fg-subtle">
        Renaming only changes how the transcript is displayed. The original is kept untouched.
      </li>
    </ul>
  );
}

function ActionItemList({ meetingId, items }: { meetingId: number; items: ActionItem[] }) {
  const queryClient = useQueryClient();

  const toggle = useMutation({
    mutationFn: (item: ActionItem) =>
      api.patch<ActionItem>(`/action-items/${item.id}`, {
        status: item.status === 'done' ? 'open' : 'done',
      }),
    onSettled: () =>
      void queryClient.invalidateQueries({ queryKey: ['summary', String(meetingId)] }),
  });

  if (!items.length) {
    return <p className="text-sm text-fg-subtle">No action items were detected.</p>;
  }

  return (
    <ul className="space-y-2">
      {items.map((item) => (
        <li key={item.id} className="flex items-start gap-2">
          <button
            onClick={() => toggle.mutate(item)}
            aria-label={item.status === 'done' ? 'Reopen' : 'Mark done'}
            className="mt-0.5 shrink-0 text-fg-faint hover:text-primary"
          >
            {item.status === 'done' ? (
              <CheckSquare className="size-4 text-success" />
            ) : (
              <Square className="size-4" />
            )}
          </button>
          <div className="min-w-0 flex-1">
            <p
              className={cn(
                'text-sm',
                item.status === 'done' && 'text-fg-subtle line-through',
              )}
            >
              {item.text}
            </p>
            <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-fg-subtle">
              {item.owner_label && <span>{item.owner_label}</span>}
              {(item.due_date || item.due_text) && (
                <span>· {item.due_date || item.due_text}</span>
              )}
              {item.priority === 'high' && (
                <Badge variant="warning" size="sm">
                  high
                </Badge>
              )}
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}

function SummaryPanel({ meetingId, summary }: { meetingId: number; summary: Summary }) {
  const queryClient = useQueryClient();

  const regenerate = useMutation({
    mutationFn: () => api.post<{ job_id: string }>(`/meetings/${meetingId}/summary/regenerate`, {}),
    onSuccess: (data) => {
      watchJob(data.job_id);
      void queryClient.invalidateQueries({ queryKey: ['jobs', 'active'] });
    },
  });

  const html = useMemo(() => {
    if (!summary.summary_md) return '';
    // The summary is LLM-authored text rendered as HTML: sanitize it.
    return DOMPurify.sanitize(marked.parse(summary.summary_md, { async: false }) as string);
  }, [summary.summary_md]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="neutral" size="sm">
          v{summary.version}
        </Badge>
        <span className="text-xs text-fg-subtle">{summary.model}</span>
        {summary.stale && (
          <Badge variant="warning" size="sm">
            Speakers changed since this ran
          </Badge>
        )}
        <Button
          size="xs"
          variant="ghost"
          className="ml-auto"
          onClick={() => regenerate.mutate()}
          loading={regenerate.isPending}
        >
          <RefreshCw />
          Regenerate
        </Button>
      </div>

      {regenerate.isSuccess && (
        <p className="rounded border border-border bg-surface-2 p-2 text-xs text-fg-muted">
          Regenerating…{' '}
          <Link to={`/jobs/${regenerate.data.job_id}`} className="text-primary hover:underline">
            follow progress
          </Link>
        </p>
      )}

      {summary.tldr && <p className="text-md leading-relaxed text-fg">{summary.tldr}</p>}

      {html && (
        <div
          className="prose prose-sm max-w-none dark:prose-invert prose-headings:font-display"
          dangerouslySetInnerHTML={{ __html: html }}
        />
      )}

      {summary.key_decisions.length > 0 && (
        <div>
          <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-fg-subtle">
            Decisions
          </h4>
          <ul className="space-y-1.5">
            {summary.key_decisions.map((decision, i) => (
              <li key={i} className="text-sm">
                {decision.decision}
                {decision.made_by && (
                  <span className="text-fg-subtle"> — {decision.made_by}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {summary.open_questions.length > 0 && (
        <div>
          <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-fg-subtle">
            Open questions
          </h4>
          <ul className="list-inside list-disc space-y-1 text-sm text-fg-muted">
            {summary.open_questions.map((question, i) => (
              <li key={i}>{question}</li>
            ))}
          </ul>
        </div>
      )}

      {summary.topics.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {summary.topics.map((topic) => (
            <Badge key={topic} variant="primary" size="sm">
              {topic}
            </Badge>
          ))}
        </div>
      )}
    </div>
  );
}

/** Seeks to ?t= once metadata is available, so a shared link lands correctly. */
function DeepLinkSeek() {
  const [params] = useSearchParams();
  const { seek } = usePlayer();
  const duration = usePlayerStore((s) => s.duration);
  const [done, setDone] = useState(false);

  useEffect(() => {
    if (done || !duration) return;
    const t = Number(params.get('t'));
    if (Number.isFinite(t) && t > 0) seek(t);
    setDone(true);
  }, [params, duration, done, seek]);

  return null;
}

export function TranscriptPage() {
  const { meetingId } = useParams<{ meetingId: string }>();
  const [tab, setTab] = useState<Tab>('summary');

  const meeting = useQuery({
    queryKey: ['meeting', meetingId],
    queryFn: () => api.get<Meeting>(`/meetings/${meetingId}`),
    enabled: !!meetingId,
  });

  const transcript = useQuery({
    queryKey: ['transcript', meetingId],
    queryFn: () => api.get<Transcript>(`/meetings/${meetingId}/transcript`),
    enabled: !!meetingId && !!meeting.data?.has_transcript,
  });

  const summary = useQuery({
    queryKey: ['summary', meetingId],
    queryFn: () => api.get<Summary>(`/meetings/${meetingId}/summary`),
    enabled: !!meetingId,
    retry: (_, error) => !(error instanceof ApiError && error.status === 404),
  });

  if (meeting.isError) return <ErrorState error={meeting.error} />;
  if (meeting.isLoading || !meeting.data) return <Skeleton className="h-96 w-full" />;

  const m = meeting.data;

  if (!m.has_transcript) {
    return (
      <div className="mx-auto max-w-2xl space-y-4">
        <Link to={`/threads/${m.thread_id}`} className="text-sm text-fg-subtle hover:text-fg">
          ← Back to thread
        </Link>
        <Card className="p-6">
          <h1 className="font-display text-xl font-semibold">{m.title}</h1>
          <p className="mt-2 text-sm text-fg-subtle">
            {m.status === 'processing'
              ? 'This recording is still being processed.'
              : 'This meeting has no transcript yet.'}
          </p>
        </Card>
      </div>
    );
  }

  return (
    <PlayerProvider
      src={`/api/meetings/${m.id}/audio`}
      segments={transcript.data?.segments ?? []}
    >
      <DeepLinkSeek />

      <div className="space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <Link
              to={`/threads/${m.thread_id}`}
              className="text-sm text-fg-subtle hover:text-fg"
            >
              ← Back to thread
            </Link>
            <h1 className="mt-1 font-display text-2xl font-semibold">{m.title}</h1>
            <p className="mt-1 text-sm text-fg-subtle">
              {m.meeting_at && new Date(m.meeting_at).toLocaleString()} ·{' '}
              {transcript.data?.num_speakers ?? m.speaker_count} speakers ·{' '}
              {transcript.data?.segments.length ?? 0} segments
            </p>
          </div>

          <Select
            className="w-auto"
            aria-label="Export transcript"
            defaultValue=""
            onChange={(e) => {
              if (!e.target.value) return;
              window.open(
                `/api/meetings/${m.id}/transcript?format=${e.target.value}`,
                '_blank',
              );
              e.target.value = '';
            }}
          >
            <option value="">Export…</option>
            <option value="text">Plain text</option>
            <option value="md">Markdown</option>
            <option value="vtt">WebVTT</option>
            <option value="json">JSON</option>
          </Select>
        </div>

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
          <Card className="overflow-hidden">
            {transcript.isLoading && <Skeleton className="h-96 w-full" />}
            {transcript.data && (
              <>
                <TranscriptView
                  segments={transcript.data.segments}
                  meetingId={m.id}
                  onRename={() => setTab('speakers')}
                />
                <PlayerBar />
              </>
            )}
          </Card>

          <div className="space-y-4 lg:sticky lg:top-20 lg:self-start">
            <Card>
              <div className="flex border-b border-border" role="tablist">
                {(['summary', 'actions', 'speakers'] as Tab[]).map((t) => (
                  <button
                    key={t}
                    role="tab"
                    aria-selected={tab === t}
                    onClick={() => setTab(t)}
                    className={cn(
                      'flex-1 border-b-2 px-3 py-2 text-sm font-medium capitalize transition-colors duration-fast',
                      tab === t
                        ? 'border-primary text-fg'
                        : 'border-transparent text-fg-subtle hover:text-fg',
                    )}
                  >
                    {t}
                  </button>
                ))}
              </div>

              <div className="p-4">
                {tab === 'summary' &&
                  (summary.data ? (
                    <SummaryPanel meetingId={m.id} summary={summary.data} />
                  ) : (
                    <div className="text-center">
                      <Sparkles className="mx-auto size-6 text-fg-faint" aria-hidden />
                      <p className="mt-2 text-sm text-fg-subtle">No summary yet.</p>
                    </div>
                  ))}

                {tab === 'actions' && (
                  <ActionItemList meetingId={m.id} items={summary.data?.action_items ?? []} />
                )}

                {tab === 'speakers' && transcript.data && (
                  <SpeakerLegend meetingId={m.id} transcript={transcript.data} />
                )}
              </div>
            </Card>

            <McpMatchPanel meeting={m} onAttached={() => meeting.refetch()} />
          </div>
        </div>
      </div>
    </PlayerProvider>
  );
}
