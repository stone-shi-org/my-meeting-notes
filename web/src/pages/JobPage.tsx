import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, Radio, RefreshCw } from 'lucide-react';
import { useEffect } from 'react';
import { Link, useParams } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { Card, Skeleton } from '@/components/ui/primitives';
import { ErrorState } from '@/components/ui/states';
import {
  ElapsedClock,
  JobErrorPanel,
  JobLogStream,
  JobStageStepper,
} from '@/components/jobs/JobProgress';
import { MatchPanel } from '@/components/match/MatchPanel';
import { api } from '@/lib/api';
import { unwatchJob, useJob } from '@/hooks/useJob';
import type { Meeting } from '@/types/api';

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

export function JobPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const queryClient = useQueryClient();
  const { job, events, transport, isLoading, error } = useJob(jobId);

  const meeting = useQuery({
    queryKey: ['meeting', String(job?.meeting_id)],
    queryFn: () => api.get<Meeting>(`/meetings/${job!.meeting_id}`),
    enabled: !!job?.meeting_id,
    refetchInterval: job && !TERMINAL.has(job.status) ? 5000 : false,
  });

  const retry = useMutation({
    mutationFn: () => api.post(`/jobs/${jobId}/retry`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['job-events', jobId] }),
  });

  const cancel = useMutation({
    mutationFn: () => api.post(`/jobs/${jobId}/cancel`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['job-events', jobId] }),
  });

  const done = job && TERMINAL.has(job.status);

  useEffect(() => {
    if (done && jobId) unwatchJob(jobId);
  }, [done, jobId]);

  if (error) return <ErrorState error={error} />;
  if (isLoading || !job) return <Skeleton className="h-64 w-full max-w-2xl" />;

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      {meeting.data && (
        <Link
          to={`/threads/${meeting.data.thread_id}`}
          className="text-sm text-fg-subtle hover:text-fg"
        >
          ← Back to thread
        </Link>
      )}

      <div>
        <h1 className="font-display text-2xl font-semibold">
          {meeting.data?.title ?? 'Processing'}
        </h1>
        <p className="mt-1 text-sm text-fg-subtle">
          {job.type === 'ingest' && 'Converting, transcribing and summarizing your recording'}
          {job.type === 'diarize' && 'Re-running transcription'}
          {job.type === 'summarize' && 'Regenerating the summary'}
          {job.type === 'match' && 'Searching calendar and email'}
        </p>
      </div>

      <Card className="p-5">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <h2 className="font-display text-lg font-semibold">
              {job.status === 'succeeded'
                ? 'Complete'
                : job.status === 'failed'
                  ? 'Failed'
                  : job.status === 'cancelled'
                    ? 'Cancelled'
                    : 'Processing'}
            </h2>
            {!done && (
              <span
                className="inline-flex items-center gap-1 text-xs text-fg-subtle"
                title={
                  transport === 'sse'
                    ? 'Live updates over a streaming connection'
                    : 'Polling every 2 seconds'
                }
              >
                {transport === 'sse' ? (
                  <>
                    <Radio className="size-3 text-success" aria-hidden /> live
                  </>
                ) : (
                  <>
                    <RefreshCw className="size-3" aria-hidden /> every 2s
                  </>
                )}
              </span>
            )}
          </div>

          <span className="text-sm text-fg-subtle">
            <ElapsedClock since={job.started_at ?? job.created_at} frozen={job.finished_at} />
          </span>
        </div>

        <div className="mb-5 h-1.5 overflow-hidden rounded-full bg-surface-2">
          <div
            className="h-full rounded-full bg-primary transition-[width] duration-slow ease-out"
            style={{ width: `${Math.round(job.progress * 100)}%` }}
            role="progressbar"
            aria-valuenow={Math.round(job.progress * 100)}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuetext={`${Math.round(job.progress * 100)} percent`}
          />
        </div>

        <JobStageStepper job={job} events={events} />

        {job.status === 'failed' && (
          <div className="mt-4">
            <JobErrorPanel job={job} onRetry={() => retry.mutate()} retrying={retry.isPending} />
          </div>
        )}

        {!done && (
          <div className="mt-5 flex items-center justify-between gap-3 border-t border-border pt-4">
            <p className="text-sm text-fg-subtle">
              Safe to leave — we&apos;ll keep working.
            </p>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => cancel.mutate()}
              loading={cancel.isPending}
              disabled={job.cancel_requested}
            >
              {job.cancel_requested ? 'Cancelling…' : 'Cancel job'}
            </Button>
          </div>
        )}

        {job.status === 'succeeded' && meeting.data && (
          <div className="mt-5 flex items-center gap-3 border-t border-border pt-4">
            <CheckCircle2 className="size-5 text-success" aria-hidden />
            <p className="flex-1 text-sm text-fg-muted">
              {meeting.data.has_transcript ? 'Transcript ready.' : 'Finished.'}
            </p>
            <Button variant="primary" asChild>
              <Link to={`/meetings/${meeting.data.id}`}>Open transcript</Link>
            </Button>
          </div>
        )}

        <JobLogStream events={events} />
      </Card>

      {/* The productive-wait slot: diarization takes minutes, so give the user
          something genuinely useful to do in that window. */}
      {meeting.data && job.type === 'ingest' && (
        <div>
          <h2 className="mb-3 font-display text-lg font-semibold">
            {done ? 'Related items' : 'While you wait'}
          </h2>
          <MatchPanel meeting={meeting.data} onAttached={() => meeting.refetch()} />
        </div>
      )}
    </div>
  );
}
