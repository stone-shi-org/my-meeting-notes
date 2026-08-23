import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DevDataPanel } from '../dev/DevDataPanel';
import type { DevDraft, DevEmail, Integration, Meeting, Paginated, Thread } from '@/types/api';

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn(), put: vi.fn() },
}));

const { api } = await import('@/lib/api');

const ACCOUNT = {
  id: 7,
  provider: 'dev',
  provider_label: 'Development (fake data)',
  account_key: 'default',
  account_label: 'Fixtures',
} as unknown as Integration;

const MEETING = { id: 3, title: 'Atlas kickoff' } as unknown as Meeting;
const THREAD = { id: 1, title: 'Atlas Migration' } as unknown as Thread;

function paged<T>(items: T[]): Paginated<T> {
  return { items, page: 1, page_size: 100, total: items.length, total_pages: 1 };
}

function email(over: Partial<DevEmail> = {}): DevEmail {
  return {
    id: 1,
    integration_id: 7,
    subject: 'Re: Atlas cutover',
    sender: 'Jane Doe <jane@example.com>',
    snippet: null,
    account: null,
    rfc2822_date: false,
    date_mode: 'relative',
    at: null,
    offset_minutes: -1440,
    anchor_meeting_id: null,
    expected_relevant: true,
    created_at: '2026-08-01T00:00:00+00:00',
    updated_at: '2026-08-01T00:00:00+00:00',
    ...over,
  };
}

function stub(emails: DevEmail[] = []) {
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path === '/integrations') return Promise.resolve([ACCOUNT] as never);
    if (path === '/threads') return Promise.resolve(paged([THREAD]) as never);
    if (path === '/meetings') return Promise.resolve(paged([MEETING]) as never);
    if (path.endsWith('/emails')) return Promise.resolve(emails as never);
    return Promise.resolve([] as never);
  });
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DevDataPanel />
    </QueryClientProvider>,
  );
}

/** The Emails card, which is where the add form and the list both live.
 * Async because it is also the first thing that needs the account query. */
async function emailsCard(): Promise<HTMLElement> {
  const heading = await screen.findByRole('heading', { name: /Emails/ });
  return heading.closest('div')!.parentElement as HTMLElement;
}

/** Open the add-email form and hand back the card it lives in, so the form's
 * own "Add" is never confused with the Events card's. */
async function openEmailForm(user: ReturnType<typeof userEvent.setup>) {
  const card = await emailsCard();
  await user.click(within(card).getByRole('button', { name: 'Add' }));
  return card;
}

beforeEach(() => {
  vi.clearAllMocks();
  stub();
  vi.mocked(api.post).mockResolvedValue({} as never);
});

describe('DevDataPanel', () => {
  it('says where to get an account when there is none', async () => {
    vi.mocked(api.get).mockImplementation((path: string) =>
      Promise.resolve((path === '/integrations' ? [] : paged([])) as never),
    );
    renderPanel();
    expect(await screen.findByText(/No Development account yet/)).toBeInTheDocument();
  });

  it('defaults a new item to an offset, not a pinned date', async () => {
    // A pinned date falls out of the match window in a couple of months.
    const user = userEvent.setup();
    renderPanel();
    await openEmailForm(user);

    expect(screen.getByLabelText('When')).toHaveValue('relative');
    expect(screen.getByLabelText('Offset in minutes')).toHaveValue(-1440);
  });

  it('posts a relative item as an offset from now', async () => {
    const user = userEvent.setup();
    renderPanel();
    const card = await openEmailForm(user);

    await user.type(screen.getByLabelText('Subject'), 'Rollback window');
    await user.click(within(card).getByRole('button', { name: 'Add' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        '/dev/integrations/7/emails',
        expect.objectContaining({
          subject: 'Rollback window',
          date_mode: 'relative',
          offset_minutes: -1440,
          anchor_meeting_id: null,
        }),
      ),
    );
  });

  it('posts an anchored item with the meeting it hangs off', async () => {
    const user = userEvent.setup();
    renderPanel();
    const card = await openEmailForm(user);

    await user.type(screen.getByLabelText('Subject'), 'Follow-up');
    await user.selectOptions(screen.getByLabelText('When'), 'anchored');
    await user.selectOptions(screen.getByLabelText('Anchor meeting'), '3');
    await user.selectOptions(screen.getByLabelText('Offset preset'), '2880');
    await user.click(within(card).getByRole('button', { name: 'Add' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        '/dev/integrations/7/emails',
        expect.objectContaining({
          date_mode: 'anchored',
          anchor_meeting_id: 3,
          offset_minutes: 2880,
        }),
      ),
    );
  });

  it('drops the anchor when the mode moves away from it', async () => {
    // The server rejects an anchored item with no meeting, so the payload must
    // not keep a stale one either.
    const user = userEvent.setup();
    renderPanel();
    const card = await openEmailForm(user);

    await user.type(screen.getByLabelText('Subject'), 'Whatever');
    await user.selectOptions(screen.getByLabelText('When'), 'anchored');
    await user.selectOptions(screen.getByLabelText('Anchor meeting'), '3');
    await user.selectOptions(screen.getByLabelText('When'), 'relative');
    await user.click(within(card).getByRole('button', { name: 'Add' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        '/dev/integrations/7/emails',
        expect.objectContaining({ date_mode: 'relative', anchor_meeting_id: null }),
      ),
    );
  });

  it('describes when an item happens in words', async () => {
    stub([email({ date_mode: 'anchored', anchor_meeting_id: 3, offset_minutes: 2880 })]);
    renderPanel();
    expect(await screen.findByText(/2d after Atlas kickoff/)).toBeInTheDocument();
  });

  it('says so when an anchor meeting has been deleted', async () => {
    stub([email({ date_mode: 'anchored', anchor_meeting_id: 999, offset_minutes: 60 })]);
    renderPanel();
    expect(await screen.findByText(/a deleted meeting/)).toBeInTheDocument();
  });

  it('marks a decoy as such', async () => {
    stub([email({ expected_relevant: false })]);
    renderPanel();
    expect(await screen.findByText('decoy')).toBeInTheDocument();
  });
});

describe('draft review', () => {
  const DRAFTS: DevDraft[] = [
    {
      kind: 'emails',
      subject: 'Real follow-up',
      sender: 'Jane',
      date_mode: 'relative',
      offset_minutes: 60,
      anchor_meeting_id: null,
      expected_relevant: true,
      note: 'direct follow-up',
    },
    {
      kind: 'emails',
      subject: 'Expenses reminder',
      sender: 'Finance',
      date_mode: 'relative',
      offset_minutes: 120,
      anchor_meeting_id: null,
      expected_relevant: false,
      note: 'office noise',
    },
  ];

  /** `/generate` is now an SSE stream (see DevDataPanel's `streamGenerate`),
   * not a plain `api.post` round trip, so it is exercised through a stubbed
   * `fetch` returning one `done` frame instead of through the `api` mock. */
  function stubGenerateStream(drafts: DevDraft[]) {
    const body = `event: done\ndata: ${JSON.stringify({ drafts, model: 'test-model' })}\n\n`;
    const encoder = new TextEncoder();
    let sent = false;
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: async () => {
            if (sent) return { value: undefined, done: true };
            sent = true;
            return { value: encoder.encode(body), done: false };
          },
        }),
      },
      json: async () => ({}),
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  beforeEach(() => {
    stub();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function generate(user: ReturnType<typeof userEvent.setup>) {
    stubGenerateStream(DRAFTS);
    renderPanel();
    await user.selectOptions(await screen.findByLabelText('Thread'), '1');
    await user.click(screen.getByRole('button', { name: 'Generate' }));
    await screen.findByText('Real follow-up');
  }

  it('writes nothing until the drafts are accepted', async () => {
    const user = userEvent.setup();
    await generate(user);

    expect(api.post).not.toHaveBeenCalled();
  });

  it('only writes the drafts that were kept', async () => {
    const user = userEvent.setup();
    await generate(user);

    await user.click(screen.getByLabelText('Keep Expenses reminder'));
    await user.click(screen.getByRole('button', { name: /Add 1 item/ }));

    await waitFor(() => {
      const writes = vi
        .mocked(api.post)
        .mock.calls.filter(([path]) => path === '/dev/integrations/7/emails');
      expect(writes).toHaveLength(1);
      expect((writes[0][1] as { subject: string }).subject).toBe('Real follow-up');
    });
  });

  it('accepts through the ordinary create route, with kind and note stripped', async () => {
    const user = userEvent.setup();
    await generate(user);
    await user.click(screen.getByRole('button', { name: /Add 2 items/ }));

    await waitFor(() => {
      const body = vi
        .mocked(api.post)
        .mock.calls.find(([path]) => path === '/dev/integrations/7/emails')![1] as object;
      expect(body).not.toHaveProperty('kind');
      expect(body).not.toHaveProperty('note');
      expect(body).toHaveProperty('expected_relevant');
    });
  });

  it('discards the whole batch on request', async () => {
    const user = userEvent.setup();
    await generate(user);
    await user.click(screen.getByRole('button', { name: 'Discard all' }));

    expect(screen.queryByText('Real follow-up')).not.toBeInTheDocument();
    expect(
      vi.mocked(api.post).mock.calls.filter(([p]) => p === '/dev/integrations/7/emails'),
    ).toHaveLength(0);
  });
});
