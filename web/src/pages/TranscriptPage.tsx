import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  CheckSquare,
  Download,
  Eye,
  EyeOff,
  FileText,
  RefreshCw,
  Square,
  Sparkles,
  User,
} from 'lucide-react';
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
import { MeetingNotesCard } from '@/components/notes/MeetingNotesCard';
import { AddRecordingCard } from '@/components/record/AddRecordingCard';
import { PlayerProvider, usePlayer } from '@/player/PlayerProvider';
import { usePlayerStore } from '@/player/playerStore';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import { renderMarkdown } from '@/lib/markdown';
import { initials, speakerVars } from '@/lib/speakerColors';
import { useGenerateSummary } from '@/hooks/useGenerateSummary';
import { ApiError, type ActionItem, type Meeting, type Summary, type Transcript } from '@/types/api';

const TRANSCRIPT_PREF_KEY = 'mmn.showTranscript';

type SpeakerPatch = {
  speaker_id: string;
  display_name?: string;
  hidden?: boolean;
  is_me?: boolean;
  merge_into?: string;
};

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

  const update = useMutation({
    mutationFn: (payload: SpeakerPatch[]) => api.put(`/meetings/${meetingId}/speakers`, payload),
    onMutate: async (payload) => {
      const key = ['transcript', String(meetingId)];
      await queryClient.cancelQueries({ queryKey: key });
      const previous = queryClient.getQueryData<Transcript>(key);

      // Optimistic for rename/hide/me: a map edit over speakers (and, for a
      // rename, the segments that carry that name). A merge changes segment
      // identity and colour across the whole transcript, so it skips this
      // and just waits for the refetch below.
      const merging = payload.some((p) => p.merge_into !== undefined);
      if (!merging) {
        queryClient.setQueryData<Transcript>(key, (old) => {
          if (!old) return old;
          let speakers = old.speakers;
          for (const p of payload) {
            speakers = speakers.map((s) => {
              if (s.id !== p.speaker_id) {
                // Setting "me" on one speaker clears every other one.
                return p.is_me === true && s.is_me ? { ...s, is_me: false } : s;
              }
              return {
                ...s,
                ...(p.display_name !== undefined ? { display_name: p.display_name || null } : {}),
                ...(p.hidden !== undefined ? { hidden: p.hidden } : {}),
                ...(p.is_me !== undefined ? { is_me: p.is_me } : {}),
              };
            });
          }
          const renamed = payload.find((p) => p.display_name !== undefined);
          return {
            ...old,
            speakers,
            segments: renamed
              ? old.segments.map((seg) =>
                  seg.speaker === renamed.speaker_id
                    ? { ...seg, speaker_name: renamed.display_name || renamed.speaker_id }
                    : seg,
                )
              : old.segments,
          };
        });
      }
      return { previous };
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) {
        queryClient.setQueryData(['transcript', String(meetingId)], context.previous);
      }
    },
    onSuccess: (_data, payload) => {
      if (payload.length === 1 && payload[0].display_name !== undefined) {
        setSaved(payload[0].speaker_id);
        window.setTimeout(() => setSaved(null), 1500);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ['transcript', String(meetingId)] });
      void queryClient.invalidateQueries({ queryKey: ['summary', String(meetingId)] });
    },
  });

  function commit(speakerId: string) {
    const value = drafts[speakerId];
    if (value === undefined) return;
    update.mutate([{ speaker_id: speakerId, display_name: value.trim() }]);
  }

  const canonical = transcript.speakers.filter((s) => !s.merged_into);
  const mergedAway = transcript.speakers.filter((s) => s.merged_into);
  const nameForCanonical = (id: string) => canonical.find((s) => s.id === id)?.display_name || id;
  const anyHidden = canonical.some((s) => s.hidden);
  const allHidden = canonical.length > 0 && canonical.every((s) => s.hidden);

  return (
    <div className="space-y-2">
      {canonical.length > 0 && (
        <div className="flex items-center justify-end gap-1">
          <Button
            size="xs"
            variant="ghost"
            disabled={!anyHidden}
            onClick={() => update.mutate(canonical.map((s) => ({ speaker_id: s.id, hidden: false })))}
          >
            Show all
          </Button>
          <Button
            size="xs"
            variant="ghost"
            disabled={allHidden}
            onClick={() => update.mutate(canonical.map((s) => ({ speaker_id: s.id, hidden: true })))}
          >
            Hide all
          </Button>
        </div>
      )}

      <ul className="space-y-2">
        {canonical.map((speaker) => {
          const value = drafts[speaker.id] ?? speaker.display_name ?? '';
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

              {speaker.is_me && (
                <Badge variant="primary" size="sm">
                  You
                </Badge>
              )}

              <span className="shrink-0 whitespace-nowrap text-right text-xs text-fg-subtle tabular">
                {speaker.duration_human} · {Math.round(speaker.share * 100)}%
              </span>

              <button
                onClick={() => update.mutate([{ speaker_id: speaker.id, hidden: !speaker.hidden }])}
                aria-pressed={speaker.hidden}
                aria-label={speaker.hidden ? `Show ${speaker.id} in the transcript` : `Hide ${speaker.id} from the transcript`}
                className="shrink-0 rounded p-1 text-fg-faint hover:text-fg"
              >
                {speaker.hidden ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
              </button>

              <button
                onClick={() => update.mutate([{ speaker_id: speaker.id, is_me: !speaker.is_me }])}
                aria-pressed={speaker.is_me}
                aria-label={speaker.is_me ? `Unmark ${speaker.id} as me` : `Mark ${speaker.id} as me`}
                className={cn(
                  'shrink-0 rounded p-1 text-fg-faint hover:text-fg',
                  speaker.is_me && 'text-primary',
                )}
              >
                <User className="size-3.5" />
              </button>

              {canonical.length > 1 && (
                <Select
                  className="h-7 w-auto shrink-0 text-xs"
                  aria-label={`Merge ${speaker.id} into another speaker`}
                  value=""
                  onChange={(e) => {
                    if (!e.target.value) return;
                    update.mutate([{ speaker_id: speaker.id, merge_into: e.target.value }]);
                  }}
                >
                  <option value="">Merge into…</option>
                  {canonical
                    .filter((other) => other.id !== speaker.id)
                    .map((other) => (
                      <option key={other.id} value={other.id}>
                        {other.display_name || other.id}
                      </option>
                    ))}
                </Select>
              )}

              {saved === speaker.id && (
                <span className="text-xs text-success-ink" role="status">
                  ✓
                </span>
              )}
            </li>
          );
        })}

        {mergedAway.map((speaker) => (
          <li key={speaker.id} className="flex items-center gap-2 text-sm text-fg-subtle">
            <span className="min-w-0 flex-1 truncate">
              {speaker.id} → merged into {nameForCanonical(speaker.merged_into!)}
            </span>
            <Button
              size="xs"
              variant="ghost"
              onClick={() => update.mutate([{ speaker_id: speaker.id, merge_into: '' }])}
            >
              Unmerge
            </Button>
          </li>
        ))}

        <li className="pt-1 text-xs text-fg-subtle">
          Renaming, hiding and merging only change how the transcript is displayed. The
          original is kept untouched.
        </li>
      </ul>
    </div>
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
            loading={generate.isPending || generate.running}
          >
            <RefreshCw />
            Generate summary
          </Button>

          {generate.error && (
            <p className="mt-2 text-sm text-danger-ink">
              {(generate.error as Error).message}
            </p>
          )}

          {generate.running && generate.data && (
            <p className="mt-2 text-xs text-fg-muted">
              Working on it — this panel will fill in on its own.{' '}
              <Link
                to={`/jobs/${generate.data.job_id}`}
                className="text-primary hover:underline"
              >
                Follow progress
              </Link>
            </p>
          )}

          {generate.failure && (
            <p className="mt-2 text-sm text-danger-ink">
              That run {generate.failure.status}.{' '}
              {generate.failure.error ?? 'No summary was written.'}
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
          loading={regenerate.isPending || regenerate.running}
          disabled={regenerate.running}
        >
          <RefreshCw />
          Regenerate
        </Button>
      </div>

      {/* Driven by the job, not by the 202 that queued it -- otherwise this
          notice never comes down and the summary below it never changes. */}
      {regenerate.running && regenerate.data && (
        <p className="rounded border border-border bg-surface-2 p-2 text-xs text-fg-muted">
          Regenerating… the summary below will update when it finishes.{' '}
          <Link to={`/jobs/${regenerate.data.job_id}`} className="text-primary hover:underline">
            follow progress
          </Link>
        </p>
      )}

      {regenerate.failure && (
        <p className="rounded border border-danger/30 bg-danger-soft/40 p-2 text-xs text-danger-ink">
          That run {regenerate.failure.status}.{' '}
          {regenerate.failure.error ?? 'The previous summary is unchanged.'}
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

  // Hiding is display-only (exports and AI chat call the API directly, not
  // this array), so both the transcript pane and the player -- which tracks
  // "active segment" by index into whatever array it's given -- read the
  // same filtered list, or their indices would drift apart.
  const visibleSegments = useMemo(() => {
    const segments = transcript.data?.segments ?? [];
    const hiddenIds = new Set(
      transcript.data?.speakers.filter((s) => s.hidden).map((s) => s.id) ?? [],
    );
    return hiddenIds.size ? segments.filter((s) => !hiddenIds.has(s.speaker)) : segments;
  }, [transcript.data]);

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
      segments={visibleSegments}
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

            <MeetingNotesCard meetingId={m.id} />

            <MatchPanel meeting={m} onAttached={() => meeting.refetch()} />
          </div>

          {showTranscript && (
            <Card className="flex max-h-[calc(100dvh-11rem)] flex-col overflow-hidden xl:sticky xl:top-20">
              {transcript.isLoading && <Skeleton className="h-96 w-full" />}
              {transcript.data && (
                <div className="min-h-0 flex-1 overflow-y-auto">
                  <TranscriptView
                    segments={visibleSegments}
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
