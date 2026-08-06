import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { isTerminal, unwatchJob, useJob, watchJob } from '@/hooks/useJob';
import { api } from '@/lib/api';
import type { Job } from '@/types/api';

/** Queue a summary run and follow it to the end. Shared by the regenerate
 * button and the empty state, since "generate the first one" and "redo it" are
 * the same request.
 *
 * The POST only *queues* the work -- it answers 202 with a job id and the
 * summarizer runs in the background. So the mutation resolving means nothing
 * has happened yet: without following the job, the page keeps showing the old
 * summary (or none) until someone reloads, and the button's `isPending` blinks
 * off after a few milliseconds as if the click did nothing.
 *
 * `running` is therefore what the UI should show a spinner for, not
 * `isPending`, and the invalidation happens when the *job* finishes.
 */
export function useGenerateSummary(meetingId: number) {
  const queryClient = useQueryClient();
  const [jobId, setJobId] = useState<string | null>(null);
  const { job } = useJob(jobId ?? undefined, !!jobId);

  // Kept separately from `job`: clearing jobId disables the feed, so the job
  // object goes away at exactly the moment there is something to report.
  const [failure, setFailure] = useState<Job | null>(null);

  const mutation = useMutation({
    mutationFn: () => api.post<{ job_id: string }>(`/meetings/${meetingId}/summary/regenerate`, {}),
    onMutate: () => setFailure(null),
    onSuccess: (data) => {
      watchJob(data.job_id);
      setJobId(data.job_id);
      void queryClient.invalidateQueries({ queryKey: ['jobs', 'active'] });
    },
  });

  const status = job?.status;
  useEffect(() => {
    if (!jobId || !isTerminal(status)) return;

    unwatchJob(jobId);
    setJobId(null);

    if (status === 'succeeded') {
      // The summary itself, and the meeting -- has_summary flips on the first
      // one, and the action items are read off the summary response.
      // String(), because the page keys these off the router param.
      void queryClient.invalidateQueries({ queryKey: ['summary', String(meetingId)] });
      void queryClient.invalidateQueries({ queryKey: ['meeting', String(meetingId)] });
    } else {
      setFailure(job ?? null);
    }
  }, [status, job, jobId, meetingId, queryClient]);

  return {
    ...mutation,
    /** Queued or still running. This, not `isPending`, is what a spinner wants:
     * the mutation itself only lasts as long as the 202. */
    running: jobId !== null,
    /** The last run that ended badly, so the caller can say so instead of
     * leaving "Regenerating…" up forever. */
    failure,
  };
}
