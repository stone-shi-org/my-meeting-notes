import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { isTerminal, unwatchJob, useJob, watchJob } from '@/hooks/useJob';
import { api } from '@/lib/api';
import type { Job } from '@/types/api';

/** Queue a re-diarization run against the meeting's existing audio and follow
 * it to the end. Mirrors useGenerateSummary: the POST only *queues* the work
 * (202 + job id), so the mutation resolving means nothing has happened yet --
 * without following the job the page keeps showing the old transcript until
 * someone reloads.
 *
 * `running` is what a spinner should key off, not `isPending`, and the
 * transcript/speaker/summary queries only get invalidated once the job
 * actually finishes.
 */
export function useRediarize(meetingId: number) {
  const queryClient = useQueryClient();
  const [jobId, setJobId] = useState<string | null>(null);
  const { job } = useJob(jobId ?? undefined, !!jobId);

  // Kept separately from `job`: clearing jobId disables the feed, so the job
  // object goes away at exactly the moment there is something to report.
  const [failure, setFailure] = useState<Job | null>(null);

  const mutation = useMutation({
    mutationFn: () => api.post<{ job_id: string }>(`/meetings/${meetingId}/rediarize`, {}),
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
      // New diarization row, new segments, possibly new speakers -- and the
      // existing summary's transcript hash no longer matches, so it also
      // flips to "stale" once this refetches.
      void queryClient.invalidateQueries({ queryKey: ['transcript', String(meetingId)] });
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
     * leaving "Redoing transcript…" up forever. */
    failure,
  };
}
