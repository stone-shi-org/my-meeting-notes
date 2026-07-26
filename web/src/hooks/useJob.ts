import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import type { Job, JobEvent } from '@/types/api';

const POLL_MS = 2000;
const SLOW_POLL_MS = 5000;
const SLOW_AFTER_MS = 120_000;
const SILENCE_LIMIT_MS = 45_000;

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

export interface JobFeed {
  job: Job | undefined;
  events: JobEvent[];
  transport: 'sse' | 'poll' | 'offline';
  isLoading: boolean;
  error: unknown;
}

/**
 * Follow a job's progress.
 *
 * Polling with a monotonic cursor is the baseline because it survives every
 * proxy. SSE is an opt-in upgrade that falls back automatically -- a four-minute
 * diarization will outlive some idle timeouts, and silently freezing the bar is
 * worse than a slightly chattier poll.
 */
export function useJob(jobId: string | undefined, enabled = true): JobFeed {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [transport, setTransport] = useState<'sse' | 'poll' | 'offline'>('poll');
  const cursor = useRef(0);
  const startedAt = useRef(Date.now());

  // Reset when the job under observation changes.
  useEffect(() => {
    setEvents([]);
    cursor.current = 0;
    startedAt.current = Date.now();
  }, [jobId]);

  const query = useQuery({
    queryKey: ['job-events', jobId],
    enabled: enabled && !!jobId,
    queryFn: async () => {
      const data = await api.get<{
        job: Job;
        events: JobEvent[];
        next_after_id: number;
      }>(`/jobs/${jobId}/events`, { after_id: cursor.current, limit: 500 });

      if (data.events.length) {
        cursor.current = data.next_after_id;
        setEvents((prev) => [...prev, ...data.events]);
      }
      return data.job;
    },
    refetchInterval: (q) => {
      const job = q.state.data as Job | undefined;
      if (job && TERMINAL.has(job.status)) return false;
      return Date.now() - startedAt.current > SLOW_AFTER_MS ? SLOW_POLL_MS : POLL_MS;
    },
    refetchIntervalInBackground: true,
  });

  // SSE upgrade. Same event table underneath, so behaviour is identical.
  useEffect(() => {
    if (!enabled || !jobId) return;
    const job = query.data;
    if (job && TERMINAL.has(job.status)) return;

    let source: EventSource | null = null;
    let errors = 0;
    let lastMessage = Date.now();
    let closed = false;

    const silenceCheck = window.setInterval(() => {
      if (Date.now() - lastMessage > SILENCE_LIMIT_MS) {
        close('poll');
      }
    }, 5000);

    function close(next: 'poll' | 'offline') {
      if (closed) return;
      closed = true;
      source?.close();
      window.clearInterval(silenceCheck);
      setTransport(next);
    }

    try {
      source = new EventSource(`/api/jobs/${jobId}/stream?after_id=${cursor.current}`);
      setTransport('sse');

      source.addEventListener('progress', (e) => {
        lastMessage = Date.now();
        try {
          const event = JSON.parse((e as MessageEvent).data) as JobEvent;
          if (event.id > cursor.current) {
            cursor.current = event.id;
            setEvents((prev) => [...prev, event]);
          }
        } catch {
          /* ignore a malformed frame */
        }
      });

      source.addEventListener('done', () => {
        lastMessage = Date.now();
        void query.refetch();
        close('poll');
      });

      source.onerror = () => {
        errors += 1;
        // Two failures inside 30s means the stream is not viable here.
        if (errors >= 2 || Date.now() - lastMessage > 30_000) close('poll');
      };
    } catch {
      close('poll');
    }

    return () => close('poll');
    // Re-establish only when the job identity changes, not on every refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, enabled]);

  return {
    job: query.data,
    events,
    transport,
    isLoading: query.isLoading,
    error: query.error,
  };
}

const WATCHED_KEY = 'mmn.watchedJobs';

function readWatched(): string[] {
  try {
    const raw = localStorage.getItem(WATCHED_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

export function watchJob(jobId: string) {
  const current = readWatched();
  if (!current.includes(jobId)) {
    localStorage.setItem(WATCHED_KEY, JSON.stringify([...current, jobId].slice(-20)));
  }
}

export function unwatchJob(jobId: string) {
  localStorage.setItem(
    WATCHED_KEY,
    JSON.stringify(readWatched().filter((id) => id !== jobId)),
  );
}

/**
 * Active jobs for the dock.
 *
 * Unions locally-remembered ids with the server's list, so the dock survives a
 * hard refresh *and* picks up a job started in another tab.
 */
export function useActiveJobs() {
  return useQuery({
    queryKey: ['jobs', 'active'],
    queryFn: async () => {
      const data = await api.get<{ items: Job[] }>('/jobs', {
        status: 'active',
        page_size: 20,
      });
      const server = data.items;
      const serverIds = new Set(server.map((j) => j.id));

      const extra = await Promise.all(
        readWatched()
          .filter((id) => !serverIds.has(id))
          .map((id) => api.get<Job>(`/jobs/${id}`).catch(() => null)),
      );

      // Drop anything the server has since finished or forgotten.
      for (const job of extra) {
        if (!job || TERMINAL.has(job.status)) {
          if (job) unwatchJob(job.id);
        }
      }
      return server;
    },
    refetchInterval: 4000,
    refetchIntervalInBackground: true,
  });
}
