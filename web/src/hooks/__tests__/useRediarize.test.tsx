/**
 * Redoing a transcript answers 202 and diarizes in the background, so the
 * mutation resolving means nothing has happened yet. Mirrors
 * useGenerateSummary.test.tsx: pins the same shape of bug -- the page only
 * learns to refetch when the *job* ends, not the POST.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Job } from '@/types/api';

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn(), put: vi.fn() },
}));

const { api } = await import('@/lib/api');
const { useRediarize } = await import('../useRediarize');

const MEETING_ID = 6;
const JOB_ID = 'job-123';

function job(status: Job['status'], error: string | null = null): Job {
  return {
    id: JOB_ID,
    type: 'diarize',
    status,
    stage: null,
    progress: status === 'succeeded' ? 1 : 0.5,
    meeting_id: MEETING_ID,
    thread_id: null,
    payload: {},
    result: null,
    error,
    error_stage: null,
    attempts: 1,
    max_attempts: 1,
    cancel_requested: false,
    created_at: '2026-08-05T00:00:00+00:00',
    started_at: null,
    finished_at: null,
    heartbeat_at: null,
    stages: [],
  };
}

/** The job feed useJob polls. Swap `current` to move the job on. */
let current: Job = job('running');

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, 'invalidateQueries');

  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const view = renderHook(() => useRediarize(MEETING_ID), { wrapper });

  /** Move the job on and pull the feed immediately, rather than waiting out
   * useJob's 2s poll. refetchQueries, not invalidateQueries, so this does not
   * show up in what the spy recorded. */
  async function advance(next: Job) {
    current = next;
    await act(async () => {
      await client.refetchQueries({ queryKey: ['job-events', JOB_ID] });
    });
  }

  return { ...view, invalidate, advance };
}

type InvalidateSpy = { mock: { calls: unknown[][] } };

/** The query keys the hook asked to refetch, as comparable strings. */
function invalidatedKeys(invalidate: InvalidateSpy): string[] {
  return invalidate.mock.calls.map(([arg]) =>
    JSON.stringify((arg as { queryKey?: unknown[] } | undefined)?.queryKey),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  current = job('running');

  vi.mocked(api.post).mockResolvedValue({ job_id: JOB_ID } as never);
  vi.mocked(api.get).mockImplementation(() =>
    Promise.resolve({ job: current, events: [], next_after_id: 0 } as never),
  );
});

describe('useRediarize', () => {
  it('posts to the rediarize route, not summary/regenerate', async () => {
    const { result } = setup();
    await act(async () => {
      await result.current.mutateAsync();
    });
    expect(api.post).toHaveBeenCalledWith(`/meetings/${MEETING_ID}/rediarize`, {});
  });

  it('stays "running" after the 202, not just for the request', async () => {
    const { result } = setup();
    expect(result.current.running).toBe(false);

    await act(async () => {
      await result.current.mutateAsync();
    });

    expect(result.current.isPending).toBe(false);
    expect(result.current.running).toBe(true);
  });

  it('refetches the transcript, summary and meeting once the job succeeds', async () => {
    const { result, invalidate, advance } = setup();
    await act(async () => {
      await result.current.mutateAsync();
    });

    invalidate.mockClear();
    await advance(job('succeeded'));

    await waitFor(() => expect(result.current.running).toBe(false));
    const keys = invalidatedKeys(invalidate);
    expect(keys).toContain(JSON.stringify(['transcript', '6']));
    expect(keys).toContain(JSON.stringify(['summary', '6']));
    expect(keys).toContain(JSON.stringify(['meeting', '6']));
  });

  it('reports a failed run instead of leaving the notice up forever', async () => {
    const { result, invalidate, advance } = setup();
    await act(async () => {
      await result.current.mutateAsync();
    });

    invalidate.mockClear();
    await advance(job('failed', 'Diarization service unreachable'));

    await waitFor(() => expect(result.current.failure?.status).toBe('failed'));
    expect(result.current.failure?.error).toBe('Diarization service unreachable');
    expect(result.current.running).toBe(false);
  });

  it('does not refetch the transcript when the run failed', async () => {
    // There is no new diarization to fetch, and the old transcript is still
    // the truth -- rediarize never UPDATEs the stored diarization in place.
    const { result, invalidate, advance } = setup();
    await act(async () => {
      await result.current.mutateAsync();
    });

    invalidate.mockClear();
    await advance(job('failed'));

    await waitFor(() => expect(result.current.running).toBe(false));
    expect(invalidatedKeys(invalidate)).not.toContain(JSON.stringify(['transcript', '6']));
  });

  it('clears a previous failure when a new run starts', async () => {
    const { result, advance } = setup();
    await act(async () => {
      await result.current.mutateAsync();
    });
    await advance(job('failed'));
    await waitFor(() => expect(result.current.failure).not.toBeNull());

    current = job('running');
    vi.mocked(api.post).mockResolvedValue({ job_id: 'job-456' } as never);
    await act(async () => {
      await result.current.mutateAsync();
    });

    expect(result.current.failure).toBeNull();
    expect(result.current.running).toBe(true);
  });

  it('stops watching the job once it has ended', async () => {
    const { result, advance } = setup();
    await act(async () => {
      await result.current.mutateAsync();
    });
    expect(window.localStorage.getItem('mmn.watchedJobs')).toContain(JOB_ID);

    await advance(job('succeeded'));
    await waitFor(() =>
      expect(window.localStorage.getItem('mmn.watchedJobs')).not.toContain(JOB_ID),
    );
  });
});
