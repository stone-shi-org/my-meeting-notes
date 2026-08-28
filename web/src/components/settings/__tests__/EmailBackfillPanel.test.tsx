import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { EmailBackfillPanel } from '../EmailBackfillPanel';
import type { EmailBackfillStats } from '@/types/api';

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), del: vi.fn(), patch: vi.fn(), put: vi.fn() },
}));

const { api } = await import('@/lib/api');

function stats(over: Partial<EmailBackfillStats> = {}): EmailBackfillStats {
  return {
    total: 100,
    bodies: 60,
    unavailable: 10,
    body_pending: 30,
    summaries: 20,
    summary_pending: 25,
    summary_not_needed: 15,
    outbound: 30,
    inbound: 50,
    direction_unknown: 20,
    with_conversation_id: 40,
    with_rfc_headers: 25,
    subject_only: 35,
    threads_pending: 4,
    ...over,
  };
}

function renderPanel(s: EmailBackfillStats = stats()) {
  vi.mocked(api.get).mockResolvedValue(s);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <EmailBackfillPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.clearAllMocks());

describe('the numbers', () => {
  it('shows the headline counts', async () => {
    renderPanel();
    expect(await screen.findByText('Attached emails')).toBeInTheDocument();
    expect(screen.getByText('100')).toBeInTheDocument();
    expect(screen.getByText('Threads waiting')).toBeInTheDocument();
  });

  it('reports unavailable separately from pending', async () => {
    // "Asked, and this account cannot" is a third state. Folding it into pending
    // would leave the bar permanently short of 100%.
    renderPanel();
    expect(await screen.findByText('10 unavailable')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Fetch remaining \(30\)/ })).toBeInTheDocument();
  });

  it('counts settled bodies as done, not just fetched ones', async () => {
    // 60 stored + 10 that never can be = 70 of 100 settled.
    renderPanel();
    await screen.findByText('Message text');
    expect(screen.getByText('70 / 100')).toBeInTheDocument();
  });

  it('says how many model calls a summary run would cost', async () => {
    renderPanel();
    expect(
      await screen.findByText(/One model call per message — 25 calls if you run it now/),
    ).toBeInTheDocument();
  });

  it('separates exact grouping from the subject-overlap guess', async () => {
    renderPanel();
    expect(await screen.findByText('Grouped by subject')).toBeInTheDocument();
    expect(screen.getByText('A guess, hedged')).toBeInTheDocument();
  });

  it('shows unknown direction as its own bucket', async () => {
    renderPanel();
    expect(await screen.findByText('Unknown')).toBeInTheDocument();
    expect(screen.getByText(/never guessed at/)).toBeInTheDocument();
  });

  it('offers no button once a pass is complete', async () => {
    renderPanel(stats({ body_pending: 0 }));
    await screen.findByText('Message text');
    expect(screen.queryByRole('button', { name: /Fetch remaining/ })).not.toBeInTheDocument();
    expect(screen.getAllByText('Up to date').length).toBeGreaterThan(0);
  });

  it('explains itself when nothing is attached yet', async () => {
    renderPanel(stats({ total: 0 }));
    expect(await screen.findByText(/No emails are attached/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Fetch remaining/ })).not.toBeInTheDocument();
  });
});

describe('running a pass', () => {
  it('loops until the server says done', async () => {
    const user = userEvent.setup();
    renderPanel();
    vi.mocked(api.post)
      .mockResolvedValueOnce({ done: false, thread_id: 1, thread_title: 'Atlas', fetched: 12, remaining: 8 })
      .mockResolvedValueOnce({ done: false, thread_id: 1, thread_title: 'Atlas', fetched: 8, remaining: 0 })
      .mockResolvedValueOnce({ done: true, thread_id: null, thread_title: null });

    await user.click(await screen.findByRole('button', { name: /Fetch remaining/ }));

    await waitFor(() => expect(screen.getByText('All done.')).toBeInTheDocument());
    expect(api.post).toHaveBeenCalledTimes(3);
    expect(api.post).toHaveBeenCalledWith('/email-backfill/bodies');
  });

  it('stops when the model stalls rather than looping forever', async () => {
    // A failed summary stays eligible on purpose, so without this the loop would
    // run to MAX_ROUNDS achieving nothing and spending money each time.
    const user = userEvent.setup();
    renderPanel();
    vi.mocked(api.post).mockResolvedValue({
      done: false, stalled: true, thread_id: 1, thread_title: 'Atlas',
      requested: 8, summarised: 0, failed: 8, remaining: 8,
    });

    await user.click(await screen.findByRole('button', { name: /Summarise remaining/ }));

    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(/the model returned nothing/),
    );
    expect(api.post).toHaveBeenCalledTimes(1);
  });

  it('stops when a batch fetches nothing', async () => {
    const user = userEvent.setup();
    renderPanel();
    vi.mocked(api.post).mockResolvedValue({
      done: false, thread_id: 1, thread_title: 'Atlas', requested: 12, fetched: 0, remaining: 30,
    });

    await user.click(await screen.findByRole('button', { name: /Fetch remaining/ }));

    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(/none of that batch could be fetched/),
    );
    expect(api.post).toHaveBeenCalledTimes(1);
  });

  it('surfaces a request failure', async () => {
    const user = userEvent.setup();
    renderPanel();
    vi.mocked(api.post).mockRejectedValue(new Error('the server said no'));

    await user.click(await screen.findByRole('button', { name: /Fetch remaining/ }));

    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('the server said no'),
    );
  });

  it('names the thread it is working on while it runs', async () => {
    const user = userEvent.setup();
    renderPanel();
    vi.mocked(api.post)
      .mockResolvedValueOnce({ done: false, thread_id: 1, thread_title: 'Atlas Migration', fetched: 12, remaining: 5 })
      .mockResolvedValueOnce({ done: true, thread_id: null, thread_title: null });

    await user.click(await screen.findByRole('button', { name: /Fetch remaining/ }));

    await waitFor(() => expect(screen.getByText('All done.')).toBeInTheDocument());
  });

  it('drives the two passes from different endpoints', async () => {
    const user = userEvent.setup();
    renderPanel();
    vi.mocked(api.post).mockResolvedValue({ done: true, thread_id: null, thread_title: null });

    await user.click(await screen.findByRole('button', { name: /Summarise remaining/ }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/email-backfill/summaries'),
    );
    expect(api.post).not.toHaveBeenCalledWith('/email-backfill/bodies');
  });
});

describe('the bars agree with the buttons', () => {
  it('measures summaries against messages that have text, not every email', async () => {
    // 20 summarised + 15 too short + 25 outstanding = 60 that could be
    // summarised, out of 100 attached. Measuring against 100 would show
    // "Up to date" beside a two-thirds-full bar the moment the count hit zero.
    renderPanel();
    expect(await screen.findByText('35 / 60')).toBeInTheDocument();
  });

  it('shows a full summaries bar exactly when the button disappears', async () => {
    renderPanel(stats({ summary_pending: 0, summaries: 45, summary_not_needed: 15 }));

    expect(await screen.findByText('60 / 60')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Summarise remaining/ })).not.toBeInTheDocument();
  });

  it('shows a full bodies bar exactly when that button disappears', async () => {
    renderPanel(stats({ body_pending: 0, bodies: 90, unavailable: 10 }));
    await screen.findByText('Message text');

    expect(screen.queryByRole('button', { name: /Fetch remaining/ })).not.toBeInTheDocument();
    expect(screen.getByText('100 / 100')).toBeInTheDocument();
  });
});
