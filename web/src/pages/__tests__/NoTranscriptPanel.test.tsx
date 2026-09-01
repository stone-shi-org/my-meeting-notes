import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { NoTranscriptPanel } from '../TranscriptPage';
import type { Job, Meeting } from '@/types/api';

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn(), put: vi.fn() },
}));

const { api } = await import('@/lib/api');

function meeting(over: Partial<Meeting> = {}): Meeting {
  return {
    id: 6,
    thread_id: 42,
    owner_id: 1,
    title: 'Atlas Migration standup',
    meeting_at: '2026-08-05T00:00:00+00:00',
    status: 'failed',
    original_filename: 'standup.webm',
    original_bytes: 1000,
    audio_duration_sec: 120,
    audio_sample_rate: 16000,
    audio_channels: 1,
    audio_converted: true,
    has_audio: true,
    has_transcript: false,
    has_summary: false,
    notes: null,
    created_at: '2026-08-05T00:00:00+00:00',
    updated_at: '2026-08-05T00:00:00+00:00',
    summary_tldr: null,
    open_action_items: 0,
    speaker_count: 0,
    ...over,
  };
}

function job(over: Partial<Job> = {}): Job {
  return {
    id: 'job-9',
    type: 'ingest',
    status: 'failed',
    stage: 'transcribe',
    progress: 0.5,
    meeting_id: 6,
    thread_id: null,
    payload: {},
    result: null,
    error: 'Diarization service unreachable',
    error_stage: 'transcribe',
    attempts: 1,
    max_attempts: 3,
    cancel_requested: false,
    created_at: '2026-08-05T00:00:00+00:00',
    started_at: '2026-08-05T00:00:00+00:00',
    finished_at: '2026-08-05T00:00:05+00:00',
    heartbeat_at: null,
    stages: [],
    ...over,
  };
}

function renderPanel(m: Meeting = meeting()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <NoTranscriptPanel meeting={m} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.get).mockResolvedValue({ items: [] });
});

describe('NoTranscriptPanel', () => {
  it('offers to download the recording when there is audio', async () => {
    renderPanel();
    expect(await screen.findByRole('combobox', { name: 'Download audio' })).toBeInTheDocument();
  });

  it('offers no download control without a recording', async () => {
    renderPanel(meeting({ has_audio: false, status: 'new' }));
    await screen.findByText(/No recording yet/);
    expect(screen.queryByRole('combobox', { name: 'Download audio' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Download audio/ })).not.toBeInTheDocument();
  });

  it('says a recording is still processing, and skips the job lookup', async () => {
    renderPanel(meeting({ status: 'processing' }));
    expect(await screen.findByText('This recording is still being processed.')).toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalledWith('/jobs', expect.anything());
  });

  it('surfaces the failed ingest job and offers to retry it', async () => {
    vi.mocked(api.get).mockResolvedValue({ items: [job()] });
    renderPanel();

    expect(api.get).toHaveBeenCalledWith('/jobs', {
      meeting_id: 6,
      type: 'ingest',
      page_size: 1,
    });
    expect(await screen.findByText('Diarization service unreachable')).toBeInTheDocument();
    expect(screen.getByText(/Failed during transcribe/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Retry/ })).toBeInTheDocument();
  });

  it('retries the failed job and follows it to the job page', async () => {
    vi.mocked(api.get).mockResolvedValue({ items: [job()] });
    vi.mocked(api.post).mockResolvedValue({ ok: true, job_id: 'job-9' });
    const user = userEvent.setup();
    renderPanel();

    await user.click(await screen.findByRole('button', { name: /Retry/ }));

    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/jobs/job-9/retry'));
  });

  it('shows nothing extra when the last run has not failed', async () => {
    vi.mocked(api.get).mockResolvedValue({ items: [job({ status: 'succeeded', error: null })] });
    renderPanel();

    await screen.findByRole('combobox', { name: 'Download audio' });
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument();
  });
});
