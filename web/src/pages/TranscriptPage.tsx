import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckSquare, Download, FileText, RefreshCw, Square, Sparkles } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { Badge, Card, Input, Select, Skeleton } from '@/components/ui/primitives';
import { ErrorState } from '@/components/ui/states';
import { PlayerBar } from '@/components/transcript/PlayerBar';
import { TranscriptChatPanel } from '@/components/transcript/TranscriptChatPanel';
import { TranscriptView } from '@/components/transcript/TranscriptView';
import { MatchPanel } from '@/components/match/MatchPanel';
import { DeleteMeetingButton } from '@/components/meetings/DeleteMeetingButton';
import { AddRecordingCard } from '@/components/record/AddRecordingCard';
import { PlayerProvider, usePlayer } from '@/player/PlayerProvider';
import { usePlayerStore } from '@/player/playerStore';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import { renderMarkdown } from '@/lib/markdown';
import { initials, speakerVars } from '@/lib/speakerColors';
import { watchJob } from '@/hooks/useJob';
import { ApiError, type ActionItem, type Meeting, type Summary, type Transcript } from '@/types/api';

const TRANSCRIPT_PREF_KEY = 'mmn.showTranscript';

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

/** Queue a summary run. Shared by the regenerate button and the empty state,
 * since "generate the first one" and "redo it" are the same request. */
function useGenerateSummary(meetingId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<{ job_id: string }>(`/meetings/${meetingId}/summary/regenerate`, {}),
    onSuccess: (data) => {
      watchJob(data.job_id);
      void queryClient.invalidateQueries({ queryKey: ['jobs', 'active'] });
    },
  });
}

/**
 * Shown when a meeting has a transcript but no summary -- normally because
 * the summarize stage failed while the transcript succeeded. The ingest job
 * deliberately doesn't fail in that case (a costly transcript shouldn't be
 * thrown away over a summary), so without this the user is stuck with no way
 * to ask for one.
 */
function NoSummaryPanel({ meetingId, canGenerate }: { meetingId: number; canGenerate: boolean }) {
  const generate = useGenerateSummary(meetingId);

  return (
    <div className="py-4 text-center">
      <Sparkles className="mx-auto size-6 text-fg-faint" aria-hidden />
      <p className="mt-2 text-sm text-fg-subtle">
        {canGenerate
          ? 'No summary yet. This usually means the language model was unavailable when the recording was processed.'
          : 'No summary yet.'}
      </p>

      {canGenerate && (
        <>
          <Button
            size="sm"
            variant="primary"
            className="mt-3"
            onClick={() => generate.mutate()}
            loading={generate.isPending}
          >
            <RefreshCw />
            Generate summary
          </Button>

          {generate.error && (
            <p className="mt-2 text-sm text-danger-ink">
              {(generate.error as Error).message}
            </p>
          )}

          {generate.isSuccess && (
            <p className="mt-2 text-xs text-fg-muted">
              Started.{' '}
              <Link
                to={`/jobs/${generate.data.job_id}`}
                className="text-primary hover:underline"
              >
                Follow progress
              </Link>
            </p>
          )}
        </>
      )}
    </div>
  );
}

function SummaryPanel({ meetingId, summary }: { meetingId: number; summary: Summary }) {
  const regenerate = useGenerateSummary(meetingId);

  const html = useMemo(() => renderMarkdown(summary.summary_md ?? ''), [summary.summary_md]);

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
  const speakersRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Collapsed by default -- the transcript is reference material, not the
  // headline. Remembered so anyone who does read along isn't re-opening it
  // on every meeting.
  const [showTranscript, setShowTranscript] = useState(
    () => localStorage.getItem(TRANSCRIPT_PREF_KEY) === '1',
  );

  useEffect(() => {
    localStorage.setItem(TRANSCRIPT_PREF_KEY, showTranscript ? '1' : '0');
  }, [showTranscript]);

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
    const processing = m.status === 'processing';
    return (
      <div className="mx-auto max-w-2xl space-y-4">
        <Link to={`/threads/${m.thread_id}`} className="text-sm text-fg-subtle hover:text-fg">
          ← Back to thread
        </Link>
        <Card className="p-6">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <h1 className="font-display text-xl font-semibold">{m.title}</h1>
              <p className="mt-2 text-sm text-fg-subtle">
                {processing
                  ? 'This recording is still being processed.'
                  : m.has_audio
                    ? 'This meeting has a recording but no transcript — the last run did not finish.'
                    : 'No recording yet. Add one below and it will be transcribed and summarized.'}
              </p>
            </div>
            <DeleteMeetingButton
              meeting={m}
              onDeleted={() => {
                void queryClient.invalidateQueries({ queryKey: ['thread-timeline', String(m.thread_id)] });
                void queryClient.invalidateQueries({ queryKey: ['threads'] });
                navigate(`/threads/${m.thread_id}`, { replace: true });
              }}
            />
          </div>
        </Card>

        {/* The dead end this used to be: a meeting created from a calendar
            event had no way to ever receive its audio. */}
        {!processing && <AddRecordingCard meeting={m} />}
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

          <div className="flex items-center gap-2">
            <Button
              variant={showTranscript ? 'secondary' : 'ghost'}
              onClick={() => setShowTranscript((v) => !v)}
              aria-expanded={showTranscript}
            >
              <FileText />
              {showTranscript ? 'Hide transcript' : 'Show transcript'}
            </Button>

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

            {m.audio_converted ? (
              <Select
                className="w-auto"
                aria-label="Download audio"
                defaultValue=""
                onChange={(e) => {
                  if (!e.target.value) return;
                  window.open(`/api/meetings/${m.id}/audio?original=${e.target.value}`, '_blank');
                  e.target.value = '';
                }}
              >
                <option value="">Download audio…</option>
                <option value="false">Converted (16kHz mono)</option>
                <option value="true">Original recording</option>
              </Select>
            ) : (
              <Button
                variant="ghost"
                onClick={() => window.open(`/api/meetings/${m.id}/audio`, '_blank')}
              >
                <Download />
                Download audio
              </Button>
            )}
          </div>
        </div>

        {/* Summary, actions and speakers are what people come here for, so
            they get the main column and are all visible at once rather than
            hidden behind tabs. The transcript is reference material: it moves
            to a side panel, collapsed by default. */}
        <div
          className={cn(
            'grid items-start gap-4',
            showTranscript && 'xl:grid-cols-[minmax(0,1fr)_460px]',
          )}
        >
          <div className="min-w-0 space-y-4">
            <Card className="p-5">
              <h2 className="mb-3 font-display text-lg font-semibold">Summary</h2>
              {summary.data ? (
                <SummaryPanel meetingId={m.id} summary={summary.data} />
              ) : summary.isLoading ? (
                <Skeleton className="h-32 w-full" />
              ) : (
                <NoSummaryPanel meetingId={m.id} canGenerate={m.has_transcript} />
              )}
            </Card>

            <div className="grid gap-4 md:grid-cols-2">
              <Card className="p-5">
                <h2 className="mb-3 font-display text-lg font-semibold">
                  Action items
                  {summary.data && summary.data.action_items.length > 0 && (
                    <span className="ml-2 text-sm font-normal text-fg-subtle tabular">
                      {summary.data.action_items.filter((a) => a.status === 'open').length} open
                    </span>
                  )}
                </h2>
                {/* Action items come out of the summary, so with no summary
                    "none detected" would be a lie -- nothing has looked yet. */}
                {summary.data ? (
                  <ActionItemList meetingId={m.id} items={summary.data.action_items} />
                ) : summary.isLoading ? (
                  <Skeleton className="h-24 w-full" />
                ) : (
                  <NoSummaryPanel meetingId={m.id} canGenerate={m.has_transcript} />
                )}
              </Card>

              <Card className="p-5" ref={speakersRef}>
                <h2 className="mb-3 font-display text-lg font-semibold">Speakers</h2>
                {transcript.data ? (
                  <SpeakerLegend meetingId={m.id} transcript={transcript.data} />
                ) : (
                  <Skeleton className="h-24 w-full" />
                )}
              </Card>
            </div>

            <MatchPanel meeting={m} onAttached={() => meeting.refetch()} />
          </div>

          {showTranscript && (
            <Card className="flex max-h-[calc(100dvh-11rem)] flex-col overflow-hidden xl:sticky xl:top-20">
              {transcript.isLoading && <Skeleton className="h-96 w-full" />}
              {transcript.data && (
                <div className="min-h-0 flex-1 overflow-y-auto">
                  <TranscriptView
                    segments={transcript.data.segments}
                    meetingId={m.id}
                    onRename={() =>
                      speakersRef.current?.scrollIntoView({
                        behavior: 'smooth',
                        block: 'center',
                      })
                    }
                  />
                </div>
              )}
            </Card>
          )}
        </div>
      </div>

      {/* Outside the grid so playback stays available with the transcript
          collapsed -- listening back doesn't require reading along. */}
      <PlayerBar />

      <TranscriptChatPanel meetingId={String(m.id)} />
    </PlayerProvider>
  );
}
