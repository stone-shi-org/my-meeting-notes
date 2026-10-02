import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SearchResults } from '../SearchResults';
import type { SearchHit, SearchResponse } from '@/types/api';

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), del: vi.fn(), patch: vi.fn(), put: vi.fn() },
}));

const { api } = await import('@/lib/api');

let nextId = 1;
function hit(over: Partial<SearchHit> = {}): SearchHit {
  const id = nextId++;
  return {
    kind: 'thread',
    id,
    ref_id: String(id),
    thread_id: 3,
    thread_title: 'Q3 planning',
    meeting_id: null,
    meeting_title: null,
    start_sec: null,
    title: `Hit ${id}`,
    snippet: null,
    date: '2026-09-30T15:00:00+00:00',
    score: 0.03,
    matched_by: ['keyword'],
    url: `/threads/${id}`,
    ...over,
  };
}

function response(hits: SearchHit[], over: Partial<SearchResponse> = {}): SearchResponse {
  return {
    query: 'budget',
    mode: 'hybrid',
    mode_used: 'hybrid',
    semantic: { available: true, reason: null },
    limit: 100,
    offset: 0,
    has_more: false,
    hits,
    ...over,
  };
}

function renderResults(r: SearchResponse, q = 'budget') {
  vi.mocked(api.get).mockResolvedValue(r as never);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SearchResults q={q} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  nextId = 1;
});

describe('SearchResults', () => {
  it('asks the server for one large page', async () => {
    renderResults(response([]));
    await screen.findByText(/No results for/);
    expect(api.get).toHaveBeenCalledWith('/search', expect.objectContaining({ q: 'budget', limit: 100 }));
  });

  it('groups by kind in the fixed order, skipping empty groups', async () => {
    renderResults(
      response([
        hit({ kind: 'thread' }),
        hit({ kind: 'email' }),
        hit({ kind: 'segment', meeting_id: 9, start_sec: 1 }),
        hit({ kind: 'meeting', meeting_id: 9 }),
        hit({ kind: 'summary', meeting_id: 9 }),
        hit({ kind: 'action_item', meeting_id: 9 }),
        hit({ kind: 'note' }),
        hit({ kind: 'event' }),
      ]),
    );
    const headings = (await screen.findAllByRole('heading', { level: 2 })).map(
      (h) => h.firstChild?.textContent,
    );
    expect(headings).toEqual([
      'Transcripts',
      'Summaries',
      'Notes',
      'Emails',
      'Events',
      'Action items',
      'Meetings',
      'Threads',
    ]);
  });

  it('does not render a heading for a kind with no hits', async () => {
    renderResults(response([hit({ kind: 'note' })]));
    await screen.findByRole('heading', { name: /Notes/ });
    expect(screen.queryByRole('heading', { name: /Transcripts/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /Threads/ })).not.toBeInTheDocument();
  });

  it('links a transcript hit to the moment it was said, with its meta line', async () => {
    renderResults(
      response([
        hit({
          kind: 'segment',
          meeting_id: 9,
          meeting_title: 'Weekly sync',
          start_sec: 754.2,
          title: 'Priya @ 12:34',
          url: '/meetings/9?t=754.2',
        }),
      ]),
    );
    const link = await screen.findByRole('link', { name: 'Priya @ 12:34' });
    expect(link).toHaveAttribute('href', '/meetings/9?t=754.2');
    const region = screen.getByRole('region', { name: /Transcripts/ });
    expect(within(region).getByText(/Q3 planning · Weekly sync · .* · 12:34/)).toBeInTheDocument();
  });

  it('highlights snippet terms and keeps HTML as text', async () => {
    const { container } = renderResults(
      response([hit({ kind: 'note', snippet: '<b>x</b> we cut the \u0002budget\u0003 for' })]),
    );
    const mark = await screen.findByText('budget');
    expect(mark.tagName).toBe('MARK');
    expect(container.querySelector('b')).toBeNull();
    expect(screen.getByText(/<b>x<\/b> we cut the/)).toBeInTheDocument();
  });

  it('says when results are keyword matches only', async () => {
    renderResults(
      response([hit()], {
        mode_used: 'keyword',
        semantic: { available: false, reason: 'Semantic search is turned off' },
      }),
    );
    expect(
      await screen.findByText(
        'Keyword matches only — semantic search is unavailable: Semantic search is turned off',
      ),
    ).toBeInTheDocument();
  });

  it('does not show the hint when semantic search is available', async () => {
    renderResults(response([hit()]));
    await screen.findByRole('heading', { name: /Threads/ });
    expect(screen.queryByText(/Keyword matches only/)).not.toBeInTheDocument();
  });

  it('shows five per group and reveals the rest on "Show more"', async () => {
    const user = userEvent.setup();
    const hits = Array.from({ length: 7 }, () => hit({ kind: 'email' }));
    renderResults(response(hits));

    const region = await screen.findByRole('region', { name: /Emails/ });
    expect(within(region).getAllByRole('link')).toHaveLength(5);
    expect(within(region).queryByText('Hit 6')).not.toBeInTheDocument();

    await user.click(within(region).getByRole('button', { name: 'Show more (2)' }));
    expect(within(region).getAllByRole('link')).toHaveLength(7);
    expect(within(region).getByText('Hit 7')).toBeInTheDocument();

    await user.click(within(region).getByRole('button', { name: 'Show fewer' }));
    expect(within(region).getAllByRole('link')).toHaveLength(5);
  });

  it('offers no "Show more" for a group of five or fewer', async () => {
    renderResults(response(Array.from({ length: 5 }, () => hit({ kind: 'note' }))));
    await screen.findByRole('region', { name: /Notes/ });
    expect(screen.queryByRole('button', { name: /Show more/ })).not.toBeInTheDocument();
  });

  it('says so when nothing matches', async () => {
    renderResults(response([]), 'zebra');
    expect(await screen.findByText('No results for “zebra”')).toBeInTheDocument();
  });

  it('surfaces a server error', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('the index is on fire'));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <SearchResults q="budget" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText('the index is on fire')).toBeInTheDocument();
  });
});
