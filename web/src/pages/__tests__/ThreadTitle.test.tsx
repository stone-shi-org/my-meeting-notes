import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ThreadDetailPage, ThreadTitle } from '../ThreadDetailPage';
import type { Thread } from '@/types/api';

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), patch: vi.fn(), post: vi.fn(), del: vi.fn(), put: vi.fn() },
}));

const { api } = await import('@/lib/api');

function thread(over: Partial<Thread> = {}): Thread {
  return {
    id: 42,
    owner_id: 1,
    title: 'Atlas Migration',
    description: null,
    archived: false,
    created_at: '2026-08-01T00:00:00+00:00',
    updated_at: '2026-08-01T00:00:00+00:00',
    meeting_count: 0,
    last_meeting_at: null,
    email_count: 0,
    event_count: 0,
    note_count: 0,
    unread_count: 0,
    auto_match_at: null,
    auto_match_error: null,
    next_step: null,
    next_step_generated_at: null,
    next_step_stale: false,
    group_id: null,
    ...over,
  };
}

function renderTitle(t: Thread) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, 'invalidateQueries');
  render(
    <QueryClientProvider client={client}>
      <ThreadTitle thread={t} />
    </QueryClientProvider>,
  );
  return { invalidate };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('ThreadTitle', () => {
  it('shows the title and a hidden-until-hover rename button', () => {
    renderTitle(thread());
    expect(screen.getByRole('heading', { name: 'Atlas Migration' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Rename thread' })).toBeInTheDocument();
  });

  it('swaps to an editable field on click, preloaded with the current title', async () => {
    const user = userEvent.setup();
    renderTitle(thread());
    await user.click(screen.getByRole('button', { name: 'Rename thread' }));

    expect(screen.getByLabelText('Thread title')).toHaveValue('Atlas Migration');
    expect(screen.queryByRole('heading')).not.toBeInTheDocument();
  });

  it('saves the new title on Enter', async () => {
    vi.mocked(api.patch).mockResolvedValue(thread({ title: 'Renamed' }) as never);
    const user = userEvent.setup();
    const { invalidate } = renderTitle(thread());

    await user.click(screen.getByRole('button', { name: 'Rename thread' }));
    const input = screen.getByLabelText('Thread title');
    await user.clear(input);
    await user.type(input, 'Renamed{Enter}');

    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith('/threads/42', { title: 'Renamed' }),
    );
    // Both the thread and the home-screen list need to pick up the new title.
    const keys = invalidate.mock.calls.map(([arg]) =>
      JSON.stringify((arg as { queryKey?: unknown[] } | undefined)?.queryKey),
    );
    expect(keys).toContain(JSON.stringify(['thread', '42']));
    expect(keys).toContain(JSON.stringify(['threads']));
  });

  it('saves on blur too, not only Enter', async () => {
    vi.mocked(api.patch).mockResolvedValue(thread({ title: 'Blurred save' }) as never);
    const user = userEvent.setup();
    renderTitle(thread());

    await user.click(screen.getByRole('button', { name: 'Rename thread' }));
    const input = screen.getByLabelText('Thread title');
    await user.clear(input);
    await user.type(input, 'Blurred save');
    await user.tab();

    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith('/threads/42', { title: 'Blurred save' }),
    );
  });

  it('reverts without saving on Escape', async () => {
    const user = userEvent.setup();
    renderTitle(thread());

    await user.click(screen.getByRole('button', { name: 'Rename thread' }));
    const input = screen.getByLabelText('Thread title');
    await user.clear(input);
    await user.type(input, 'Abandoned edit{Escape}');

    expect(api.patch).not.toHaveBeenCalled();
    expect(screen.getByRole('heading', { name: 'Atlas Migration' })).toBeInTheDocument();
  });

  it('is a no-op when the draft is empty', async () => {
    // Nothing useful to save, and the server would reject it anyway
    // (ThreadUpdateRequest requires min_length=1).
    const user = userEvent.setup();
    renderTitle(thread());

    await user.click(screen.getByRole('button', { name: 'Rename thread' }));
    await user.clear(screen.getByLabelText('Thread title'));
    await user.keyboard('{Enter}');

    expect(api.patch).not.toHaveBeenCalled();
    expect(screen.getByRole('heading', { name: 'Atlas Migration' })).toBeInTheDocument();
  });

  it('is a no-op when the draft is unchanged', async () => {
    const user = userEvent.setup();
    renderTitle(thread());

    await user.click(screen.getByRole('button', { name: 'Rename thread' }));
    await user.keyboard('{Enter}');

    expect(api.patch).not.toHaveBeenCalled();
  });

  it('shows a validation error inline and stays editable', async () => {
    const { ApiError } = await import('@/types/api');
    vi.mocked(api.patch).mockRejectedValue(
      new ApiError(400, 'validation_error', 'Title is too long'),
    );
    const user = userEvent.setup();
    renderTitle(thread());

    await user.click(screen.getByRole('button', { name: 'Rename thread' }));
    await user.type(screen.getByLabelText('Thread title'), ' more{Enter}');

    expect(await screen.findByRole('alert')).toHaveTextContent('Title is too long');
    expect(screen.getByLabelText('Thread title')).toBeInTheDocument();
  });
});

describe('ThreadDetailPage timeline filters persistence', () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/threads/42') return Promise.resolve(thread()) as never;
      if (url === '/threads/42/timeline') {
        return Promise.resolve([
          {
            kind: 'meeting',
            id: 101,
            at: '2026-08-01T10:00:00+00:00',
            payload: {
              id: 101,
              title: 'Project Kickoff',
              has_audio: true,
              status: 'ready',
              speaker_count: 2,
              open_action_items: 0,
              thread_id: 42,
              created_at: '2026-08-01T10:00:00+00:00',
            },
          },
          {
            kind: 'email_chain',
            id: 201,
            at: '2026-08-01T11:00:00+00:00',
            payload: {
              id: 201,
              subject: 'Kickoff notes and follow-ups',
              message_count: 1,
              unread_count: 0,
              awaiting: null,
              thread_id: 42,
              participants: [],
              last_message_at: '2026-08-01T11:00:00+00:00',
              messages: [],
            },
          },
        ]) as never;
      }
      return Promise.resolve({}) as never;
    });
  });

  function renderDetailPage() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/threads/42']}>
          <Routes>
            <Route path="/threads/:threadId" element={<ThreadDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it('persists filter toggles to localStorage', async () => {
    const user = userEvent.setup();
    renderDetailPage();

    const emailsBtn = await screen.findByRole('button', { name: 'Emails' });
    expect(emailsBtn).toHaveAttribute('aria-pressed', 'true');

    await user.click(emailsBtn);
    expect(emailsBtn).toHaveAttribute('aria-pressed', 'false');

    const saved = JSON.parse(window.localStorage.getItem('mmn.threadTimeline.filters')!);
    expect(saved).not.toContain('email');
    expect(saved).toContain('meeting');
  });

  it('restores filters from localStorage on mount', async () => {
    window.localStorage.setItem('mmn.threadTimeline.filters', JSON.stringify(['meeting']));
    renderDetailPage();

    const meetingsBtn = await screen.findByRole('button', { name: 'Meetings' });
    const emailsBtn = screen.getByRole('button', { name: 'Emails' });

    expect(meetingsBtn).toHaveAttribute('aria-pressed', 'true');
    expect(emailsBtn).toHaveAttribute('aria-pressed', 'false');
  });
});

