import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SearchIndexPanel } from '../SearchIndexPanel';
import type { SearchStatus } from '@/types/api';

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), del: vi.fn(), patch: vi.fn(), put: vi.fn() },
}));
vi.mock('@/hooks/useAuth', () => ({ useAuth: vi.fn() }));

const { api } = await import('@/lib/api');
const { useAuth } = await import('@/hooks/useAuth');

function status(over: Partial<SearchStatus> = {}): SearchStatus {
  return {
    kinds: [
      { kind: 'thread', indexed: 12 },
      { kind: 'meeting', indexed: 30 },
      { kind: 'segment', indexed: 812 },
      { kind: 'summary', indexed: 28 },
      { kind: 'action_item', indexed: 61 },
      { kind: 'note', indexed: 7 },
      { kind: 'email', indexed: 140 },
      { kind: 'event', indexed: 45 },
    ],
    pending_scopes: 0,
    last_indexed_at: '2026-10-01T10:00:00+00:00',
    embedding: {
      enabled: true,
      model: 'text-embedding-3-small',
      chunks: 1200,
      embedded: 1100,
      pending_scopes: 0,
      scale_warning: false,
      scale_limit: 25000,
      last_error: null,
    },
    is_admin: true,
    ...over,
  };
}

function asAdmin(isAdmin: boolean) {
  vi.mocked(useAuth).mockReturnValue({
    isAdmin,
    user: null,
    status: 'authenticated',
    mustChangePassword: false,
    login: vi.fn(),
    logout: vi.fn(),
    refresh: vi.fn(),
  });
}

function renderPanel(s: SearchStatus = status()) {
  vi.mocked(api.get).mockResolvedValue(s as never);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SearchIndexPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  asAdmin(true);
});

describe('SearchIndexPanel', () => {
  it('shows the indexed count for every kind, with human labels', async () => {
    renderPanel();
    expect(await screen.findByRole('rowheader', { name: 'Transcripts' })).toBeInTheDocument();
    expect(screen.getByText('812')).toBeInTheDocument();
    expect(screen.getByRole('rowheader', { name: 'Action items' })).toBeInTheDocument();
    expect(screen.getByText('61')).toBeInTheDocument();
    // 12+30+812+28+61+7+140+45
    expect(screen.getByText('1,135')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/search/status');
  });

  it('says how much is waiting to be indexed', async () => {
    renderPanel(status({ pending_scopes: 3 }));
    expect(await screen.findByText('3 threads and meetings')).toBeInTheDocument();
  });

  it('shows embedding coverage with the model', async () => {
    renderPanel();
    expect(await screen.findByText('1,100 / 1,200')).toBeInTheDocument();
    expect(screen.getByText('text-embedding-3-small')).toBeInTheDocument();
  });

  it('says semantic search is off, and where to turn it on', async () => {
    renderPanel(status({ embedding: { ...status().embedding, enabled: false } }));
    expect(await screen.findByText('Off')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Semantic pre-filter/ })).toHaveAttribute(
      'href',
      '/settings/matching',
    );
    expect(screen.queryByText('1,100 / 1,200')).not.toBeInTheDocument();
  });

  it('surfaces the last embedding error and the scale note', async () => {
    renderPanel(
      status({
        embedding: {
          ...status().embedding,
          chunks: 30000,
          scale_warning: true,
          last_error: 'HTTP 401 from embedding endpoint',
        },
      }),
    );
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 401 from embedding endpoint');
    expect(screen.getByText(/slows down past 25,000 passages/)).toBeInTheDocument();
  });

  it('offers no rebuild to a non-admin', async () => {
    asAdmin(false);
    renderPanel(status({ is_admin: false }));
    await screen.findByRole('rowheader', { name: 'Transcripts' });
    expect(screen.queryByRole('button', { name: /Rebuild index/ })).not.toBeInTheDocument();
  });

  it('lets an admin rebuild, then polls status', async () => {
    const user = userEvent.setup();
    renderPanel();
    vi.mocked(api.post).mockResolvedValue({ ok: true, queued_scopes: 42 } as never);
    vi.mocked(api.get).mockResolvedValue(status({ pending_scopes: 42 }) as never);

    await user.click(await screen.findByRole('button', { name: /Rebuild index/ }));

    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/search/rebuild'));
    expect(await screen.findByText(/42 threads and meetings queued/)).toBeInTheDocument();
    // The status is re-read after the rebuild is accepted.
    await waitFor(() => expect(vi.mocked(api.get).mock.calls.length).toBeGreaterThan(1));
  });
});
