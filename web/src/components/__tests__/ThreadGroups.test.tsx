import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { GroupedThreadList } from '../thread/ThreadGroups';
import type { Paginated, Thread, ThreadGroup } from '@/types/api';

vi.mock('@/lib/api', () => ({
  api: {
    get: vi.fn(),
    put: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  },
}));

const { api } = await import('@/lib/api');

function group(id: number, name: string, thread_count = 0): ThreadGroup {
  return {
    id,
    owner_id: 1,
    name,
    thread_count,
    created_at: '2026-01-01T00:00:00+00:00',
    updated_at: '2026-01-01T00:00:00+00:00',
  };
}

function thread(id: number, title: string, group_id: number | null): Thread {
  return {
    id,
    owner_id: 1,
    title,
    description: null,
    archived: false,
    created_at: '2026-01-01T00:00:00+00:00',
    updated_at: '2026-01-01T00:00:00+00:00',
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
    group_id,
    auto_match_enabled: true,
    auto_match_calendar_enabled: true,
    auto_match_email_enabled: true,
    next_step_enabled: true,
  };
}

function page(items: Thread[], total = items.length, pageSize = 6): Paginated<Thread> {
  return {
    items,
    page: 1,
    page_size: pageSize,
    total,
    total_pages: Math.max(1, Math.ceil(total / pageSize)),
  };
}

/** Threads in one group, and one loose. The default fixture for these tests. */
const GROUPS = [group(1, 'Clients', 1)];

function stubApi(threadsByGroup: Record<string, Paginated<Thread>>) {
  vi.mocked(api.get).mockImplementation((path: string, query?: Record<string, unknown>) => {
    if (path === '/thread-groups') return Promise.resolve(GROUPS as never);
    const key = String(query?.group);
    return Promise.resolve((threadsByGroup[key] ?? page([])) as never);
  });
}

function renderList() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <GroupedThreadList
          filters={{ q: '', sort: 'updated_at', archived: false }}
          emptyState={<p>No threads yet</p>}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** A drop carrying the card payload, which is the only kind a section accepts. */
function threadDrag(id: number) {
  const data: Record<string, string> = { 'application/x-mmn-thread-id': String(id) };
  return {
    dataTransfer: {
      types: Object.keys(data),
      getData: (type: string) => data[type] ?? '',
      setData: vi.fn(),
      dropEffect: '',
      effectAllowed: '',
    },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  stubApi({
    '1': page([thread(10, 'Atlas Migration', 1)]),
    none: page([thread(20, 'Loose end', null)]),
  });
  vi.mocked(api.put).mockResolvedValue(undefined as never);
});

describe('GroupedThreadList', () => {
  it('renders a section per group with Ungrouped last', async () => {
    renderList();
    await screen.findByRole('region', { name: 'Clients' });
    expect(screen.getAllByRole('region').map((s) => s.getAttribute('aria-label'))).toEqual([
      'Clients',
      'Ungrouped',
    ]);
  });

  it('asks the server for each group separately, so neither is split by paging', async () => {
    renderList();
    await screen.findByText('Atlas Migration');

    const groupParams = vi
      .mocked(api.get)
      .mock.calls.filter(([path]) => path === '/threads')
      .map(([, query]) => (query as Record<string, unknown>).group);
    expect(groupParams).toEqual(expect.arrayContaining(['1', 'none']));
  });

  it('files a thread into the group it is dropped on', async () => {
    renderList();
    await screen.findByText('Loose end');

    const clients = screen.getByRole('region', { name: 'Clients' });
    // dragover first: without its preventDefault a browser never fires the drop.
    fireEvent.dragOver(clients, threadDrag(20));
    fireEvent.drop(clients, threadDrag(20));

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/threads/20/group', { group_id: 1 }),
    );
  });

  it('sends null when a thread is dropped on Ungrouped', async () => {
    renderList();
    await screen.findByText('Atlas Migration');

    const ungrouped = screen.getByRole('region', { name: 'Ungrouped' });
    fireEvent.dragOver(ungrouped, threadDrag(10));
    fireEvent.drop(ungrouped, threadDrag(10));

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/threads/10/group', { group_id: null }),
    );
  });

  it('ignores a drop of something that is not a thread card', async () => {
    renderList();
    await screen.findByText('Atlas Migration');

    const clients = screen.getByRole('region', { name: 'Clients' });
    const link = {
      dataTransfer: { types: ['text/uri-list'], getData: () => 'http://example.com' },
    };
    fireEvent.drop(clients, link);

    expect(api.put).not.toHaveBeenCalled();
  });

  it('does not write when a card is dropped back where it already is', async () => {
    renderList();
    await screen.findByText('Atlas Migration');

    const clients = screen.getByRole('region', { name: 'Clients' });
    fireEvent.drop(clients, threadDrag(10));

    expect(api.put).not.toHaveBeenCalled();
  });

  it('moves a thread from the keyboard-reachable card menu too', async () => {
    const user = userEvent.setup();
    renderList();
    await screen.findByText('Loose end');

    await user.click(screen.getByRole('button', { name: 'Actions for Loose end' }));
    await user.click(screen.getByRole('menuitem', { name: 'Move to…' }));
    await user.click(screen.getByRole('menuitem', { name: 'Move Loose end to Clients' }));

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/threads/20/group', { group_id: 1 }),
    );
  });

  it('archives a thread from the card menu', async () => {
    const user = userEvent.setup();
    vi.mocked(api.patch).mockResolvedValue(undefined as never);
    renderList();
    await screen.findByText('Atlas Migration');

    await user.click(screen.getByRole('button', { name: 'Actions for Atlas Migration' }));
    await user.click(screen.getByRole('menuitem', { name: 'Archive' }));

    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith('/threads/10', { archived: true }),
    );
  });

  it('deletes a thread from the card menu after confirming', async () => {
    const user = userEvent.setup();
    vi.mocked(api.del).mockResolvedValue(undefined as never);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderList();
    await screen.findByText('Atlas Migration');

    await user.click(screen.getByRole('button', { name: 'Actions for Atlas Migration' }));
    await user.click(screen.getByRole('menuitem', { name: 'Delete' }));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => expect(api.del).toHaveBeenCalledWith('/threads/10'));
  });

  it('does not delete a thread when the confirmation is declined', async () => {
    const user = userEvent.setup();
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderList();
    await screen.findByText('Atlas Migration');

    await user.click(screen.getByRole('button', { name: 'Actions for Atlas Migration' }));
    await user.click(screen.getByRole('menuitem', { name: 'Delete' }));

    expect(api.del).not.toHaveBeenCalled();
  });

  it('collapses a section and remembers it', async () => {
    const user = userEvent.setup();
    renderList();

    const heading = await screen.findByRole('button', { name: /^Clients/ });
    await user.click(heading);

    expect(heading).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Atlas Migration')).not.toBeInTheDocument();
    expect(JSON.parse(window.localStorage.getItem('mmn.threadGroups.collapsed')!)).toEqual(['1']);
  });

  it('keeps one section collapsed when another is toggled', async () => {
    const user = userEvent.setup();
    renderList();

    await user.click(await screen.findByRole('button', { name: /^Clients/ }));
    await user.click(screen.getByRole('button', { name: /^Ungrouped/ }));

    const stored = JSON.parse(window.localStorage.getItem('mmn.threadGroups.collapsed')!);
    expect(new Set(stored)).toEqual(new Set(['1', 'none']));
  });

  it('shows the group total on a collapsed section', async () => {
    window.localStorage.setItem('mmn.threadGroups.collapsed', JSON.stringify(['1']));
    renderList();

    const heading = await screen.findByRole('button', { name: /^Clients/ });
    expect(within(heading).getByText('1')).toBeInTheDocument();
  });

  it('paints each card rail in its own group colour', async () => {
    renderList();
    await screen.findByText('Atlas Migration');

    const rail = (title: string) =>
      (screen.getByText(title).closest('[draggable]')!.querySelector('span[aria-hidden]') as
        HTMLElement).style.background;

    expect(rail('Atlas Migration')).toBe('var(--group-1)');
    // Ungrouped is the absence of a group colour, not a ninth one.
    expect(rail('Loose end')).toBe('var(--entity-meeting)');
  });

  it('hides Ungrouped once every thread has been filed', async () => {
    stubApi({ '1': page([thread(10, 'Atlas Migration', 1)]), none: page([]) });
    renderList();
    await screen.findByText('Atlas Migration');

    expect(screen.queryByRole('region', { name: 'Ungrouped' })).not.toBeInTheDocument();
  });

  it('brings Ungrouped back for the duration of a drag', async () => {
    // Otherwise dragging the last thread out of a group has nowhere to land.
    stubApi({ '1': page([thread(10, 'Atlas Migration', 1)]), none: page([]) });
    renderList();
    const card = (await screen.findByText('Atlas Migration')).closest('[draggable]')!;

    fireEvent.dragStart(card, threadDrag(10));
    expect(screen.getByRole('region', { name: 'Ungrouped' })).toBeInTheDocument();

    fireEvent.dragEnd(card, threadDrag(10));
    expect(screen.queryByRole('region', { name: 'Ungrouped' })).not.toBeInTheDocument();
  });

  it('keeps an empty Ungrouped when no group exists — it is the whole page', async () => {
    vi.mocked(api.get).mockImplementation((path: string) =>
      Promise.resolve((path === '/thread-groups' ? [] : page([])) as never),
    );
    renderList();
    expect(await screen.findByText('No threads yet')).toBeInTheDocument();
  });

  it('hints at the drag instead when a real group is empty', async () => {
    stubApi({ '1': page([]), none: page([thread(20, 'Loose end', null)]) });
    renderList();
    expect(await screen.findByText(/Drag a thread card here/)).toBeInTheDocument();
    expect(screen.queryByText('No threads yet')).not.toBeInTheDocument();
  });

  it("shows a thread's cached next step on its card", async () => {
    const withNextStep = {
      ...thread(10, 'Atlas Migration', 1),
      next_step: 'Send the cutover recap to Priya before Thursday.',
    };
    stubApi({ '1': page([withNextStep]), none: page([]) });
    renderList();

    expect(
      await screen.findByText('Send the cutover recap to Priya before Thursday.'),
    ).toBeInTheDocument();
  });

  it('shows nothing extra when a thread has no next step yet', async () => {
    renderList();
    await screen.findByText('Atlas Migration');

    // The card's other content renders fine; there's just no next-step line.
    expect(screen.queryByText(/before Thursday/)).not.toBeInTheDocument();
  });
});
