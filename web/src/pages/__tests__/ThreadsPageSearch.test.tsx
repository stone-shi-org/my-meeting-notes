import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ThreadsPage } from '../ThreadsPage';
import type { SearchResponse } from '@/types/api';

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), del: vi.fn(), patch: vi.fn(), put: vi.fn() },
}));

// The page's neighbours each run queries of their own; none of them is what
// these tests are about, so they are stubbed to markers.
vi.mock('@/components/thread/ThreadGroups', () => ({
  GroupedThreadList: ({ filters }: { filters: { q: string } }) => (
    <div data-testid="thread-list">threads filtered by “{filters.q}”</div>
  ),
  NewGroupButton: () => null,
}));
vi.mock('@/components/calendar/UpcomingPanel', () => ({ UpcomingPanel: () => null }));
vi.mock('@/components/home/HomeChatPanel', () => ({ HomeChatPanel: () => null }));

const { api } = await import('@/lib/api');

const RESULTS: SearchResponse = {
  query: 'budget',
  mode: 'hybrid',
  mode_used: 'hybrid',
  semantic: { available: true, reason: null },
  limit: 100,
  offset: 0,
  has_more: false,
  hits: [
    {
      kind: 'note',
      id: 1,
      ref_id: '1',
      thread_id: 3,
      thread_title: 'Q3 planning',
      meeting_id: null,
      meeting_title: null,
      start_sec: null,
      title: 'Budget notes',
      snippet: 'the \u0002budget\u0003 is fine',
      date: null,
      score: 1,
      matched_by: ['keyword'],
      url: '/threads/3',
    },
  ],
};

function Location() {
  const location = useLocation();
  return <output data-testid="location">{location.search}</output>;
}

function renderPage(initial = '/') {
  vi.mocked(api.get).mockResolvedValue(RESULTS as never);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route
            index
            element={
              <>
                <ThreadsPage />
                <Location />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function search() {
  return new URLSearchParams(screen.getByTestId('location').textContent ?? '');
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('ThreadsPage: search everything', () => {
  it('offers to search everything only once something is typed', async () => {
    const user = userEvent.setup();
    renderPage();
    expect(screen.queryByRole('button', { name: /Search everything/ })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText('Search threads'), 'budget');
    expect(
      screen.getByRole('button', { name: 'Search everything for “budget”' }),
    ).toBeInTheDocument();
    // Typing alone does not navigate or search.
    expect(search().get('search')).toBeNull();
    expect(api.get).not.toHaveBeenCalled();
  });

  it('opens results on Enter, and still applies the thread filter', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.type(screen.getByLabelText('Search threads'), 'budget{Enter}');

    expect(search().get('search')).toBe('budget');
    expect(search().get('q')).toBe('budget');
    expect(await screen.findByRole('link', { name: 'Budget notes' })).toBeInTheDocument();
    expect(screen.queryByTestId('thread-list')).not.toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/search', expect.objectContaining({ q: 'budget' }));
  });

  it('opens results from the "Search everything" row', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.type(screen.getByLabelText('Search threads'), 'budget');
    await user.click(screen.getByRole('button', { name: 'Search everything for “budget”' }));

    expect(search().get('search')).toBe('budget');
    expect(await screen.findByRole('heading', { name: /Notes/ })).toBeInTheDocument();
  });

  it('renders results straight from a shared ?search= link', async () => {
    renderPage('/?search=budget');
    expect(await screen.findByRole('link', { name: 'Budget notes' })).toBeInTheDocument();
    expect(screen.getByLabelText('Search threads')).toHaveValue('budget');
  });

  it('goes back to the filtered thread list, keeping the other filters', async () => {
    const user = userEvent.setup();
    renderPage('/?q=budget&search=budget&sort=title&archived=1');
    await screen.findByRole('link', { name: 'Budget notes' });

    await user.click(screen.getByRole('button', { name: /Back to threads/ }));

    expect(search().get('search')).toBeNull();
    expect(search().get('q')).toBe('budget');
    expect(search().get('sort')).toBe('title');
    expect(search().get('archived')).toBe('1');
    expect(screen.getByTestId('thread-list')).toHaveTextContent('threads filtered by “budget”');
  });

  it('clearing the input leaves both the filter and the results', async () => {
    const user = userEvent.setup();
    renderPage('/?q=budget&search=budget');
    await screen.findByRole('link', { name: 'Budget notes' });

    await user.click(screen.getByRole('button', { name: 'Clear search' }));

    expect(search().get('search')).toBeNull();
    expect(search().get('q')).toBeNull();
    expect(screen.getByTestId('thread-list')).toBeInTheDocument();
  });
});
