import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { EmailChainCard } from '../EmailChainCard';
import type { Email, EmailChain } from '@/types/api';

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), patch: vi.fn(), post: vi.fn(), del: vi.fn(), put: vi.fn() },
}));

const { api } = await import('@/lib/api');

function email(over: Partial<Email> = {}): Email {
  return {
    id: 1,
    message_id: 'google:5:m1',
    sender: 'priya@acme.com',
    subject: 'Atlas cutover',
    date: '2026-08-20T09:00:00+00:00',
    snippet: 'A snippet of the message',
    account: 'me@acme.com',
    triage_level: null,
    tag: null,
    summary: null,
    score: null,
    relevance_score: null,
    relevance_reason: null,
    direction: 'inbound',
    has_body: false,
    body_fetched_at: null,
    ...over,
  };
}

function chain(over: Partial<EmailChain> = {}): EmailChain {
  const messages = over.messages ?? [email()];
  return {
    key: 'google:5:m1',
    root_id: 1,
    subject: 'Atlas cutover',
    participants: ['priya@acme.com'],
    message_count: messages.length,
    first_message_at: messages[0].date,
    last_message_at: messages[messages.length - 1].date,
    last_message_from: 'them',
    awaiting: 'you',
    unread_count: 0,
    ...over,
    messages,
  };
}

function renderCard(c: EmailChain) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <EmailChainCard chain={c} threadId="7" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('collapsed', () => {
  it('shows the subject and does not render the message list', async () => {
    const c = chain({
      messages: [email({ id: 1 }), email({ id: 2, message_id: 'google:5:m2' })],
    });
    renderCard(c);

    expect(screen.getByText('Atlas cutover')).toBeInTheDocument();
    expect(screen.getByText(/2 messages/)).toBeInTheDocument();
    // Not merely hidden -- not mounted, so no body query fires for a collapsed chain.
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });

  it('prefers the AI summary over the snippet as the preview', () => {
    renderCard(
      chain({ messages: [email({ ai_summary: 'Priya confirms the Friday window' })] }),
    );
    expect(screen.getByText('Priya confirms the Friday window')).toBeInTheDocument();
    expect(screen.queryByText('A snippet of the message')).not.toBeInTheDocument();
  });

  it('shows an awaiting badge only when a side is known', () => {
    const { unmount } = renderCard(chain({ awaiting: 'you' }));
    expect(screen.getByText('Your reply')).toBeInTheDocument();
    unmount();

    renderCard(chain({ awaiting: null, last_message_from: null }));
    expect(screen.queryByText('Your reply')).not.toBeInTheDocument();
    expect(screen.queryByText('Waiting on them')).not.toBeInTheDocument();
  });

  it('drops the chain chrome for a single message', () => {
    renderCard(chain({ messages: [email()] }));
    expect(screen.queryByText(/1 messages?/)).not.toBeInTheDocument();
  });
});

describe('expanding', () => {
  it('reveals the messages and opens the newest body', async () => {
    const user = userEvent.setup();
    const c = chain({
      messages: [
        email({ id: 1, direction: 'outbound', date: '2026-08-19T09:00:00+00:00' }),
        email({
          id: 2,
          message_id: 'google:5:m2',
          has_body: true,
          date: '2026-08-20T09:00:00+00:00',
        }),
      ],
    });
    vi.mocked(api.get).mockResolvedValue({
      id: 2,
      body: 'The rollback rehearsal is booked.',
      body_fetched_at: '2026-08-27T00:00:00+00:00',
      has_body: true,
      ai_summary: null,
      ai_summary_model: null,
    });

    renderCard(c);
    const toggle = screen.getByRole('button', { name: /Atlas cutover/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    await user.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    // One click gets you what you wanted: the newest message's body is open.
    await waitFor(() =>
      expect(screen.getByText('The rollback rehearsal is booked.')).toBeInTheDocument(),
    );
    expect(api.get).toHaveBeenCalledWith('/threads/7/emails/2/body');
  });
});

describe('direction', () => {
  it('labels an outbound message "You"', async () => {
    const user = userEvent.setup();
    renderCard(chain({ messages: [email({ direction: 'outbound' })] }));
    await user.click(screen.getByRole('button', { name: /Atlas cutover/ }));
    expect(screen.getByText('You')).toBeInTheDocument();
  });

  it('labels an inbound message with its sender', async () => {
    const user = userEvent.setup();
    renderCard(chain({ messages: [email({ direction: 'inbound' })] }));
    await user.click(screen.getByRole('button', { name: /Atlas cutover/ }));
    expect(screen.getByText('priya@acme.com')).toBeInTheDocument();
  });

  it('renders neither "You" nor any guess when direction is null', async () => {
    // The requirement-3 regression guard, and the single most valuable assertion
    // here. A NULL rendered as a side is what tells the reader -- and the
    // summarizer -- that someone else asked a question they asked themselves.
    const user = userEvent.setup();
    renderCard(
      chain({
        awaiting: null,
        last_message_from: null,
        messages: [email({ direction: null })],
      }),
    );
    await user.click(screen.getByRole('button', { name: /Atlas cutover/ }));

    expect(screen.queryByText('You')).not.toBeInTheDocument();
    expect(screen.queryByText('Them')).not.toBeInTheDocument();
    expect(screen.queryByText('Unknown')).not.toBeInTheDocument();
    // The sender is still shown -- unknown means unlabelled, not unrendered.
    expect(screen.getByText('priya@acme.com')).toBeInTheDocument();
  });
});

describe('body states', () => {
  it('offers to load a body that was never fetched', async () => {
    const user = userEvent.setup();
    vi.mocked(api.post).mockResolvedValue({ requested: 1, fetched: 1 });
    renderCard(chain({ messages: [email({ has_body: false, body_fetched_at: null })] }));

    await user.click(screen.getByRole('button', { name: /Atlas cutover/ }));
    const load = screen.getByRole('button', { name: /Load full message/ });
    await user.click(load);

    expect(api.post).toHaveBeenCalledWith('/threads/7/emails/1/hydrate');
  });

  it('offers no retry when the account cannot supply a body', async () => {
    // Terminal state: a retry that cannot succeed is a lie.
    const user = userEvent.setup();
    renderCard(
      chain({
        messages: [
          email({ has_body: false, body_fetched_at: '2026-08-27T00:00:00+00:00' }),
        ],
      }),
    );
    await user.click(screen.getByRole('button', { name: /Atlas cutover/ }));

    expect(screen.getByText(/can't supply the full message/)).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /load full message|try again/i }),
    ).not.toBeInTheDocument();
    // ...and the snippet stays, so the row is never blank.
    expect(screen.getByText('A snippet of the message')).toBeInTheDocument();
  });

  it('shows an AI summary even when the body is unavailable', async () => {
    const user = userEvent.setup();
    renderCard(
      chain({
        messages: [
          email({
            has_body: false,
            body_fetched_at: '2026-08-27T00:00:00+00:00',
            ai_summary: 'Priya confirms Friday',
          }),
        ],
      }),
    );
    await user.click(screen.getByRole('button', { name: /Atlas cutover/ }));

    expect(screen.getByText('Priya confirms Friday')).toBeInTheDocument();
    // Attributed, never passed off as the sender's own words.
    expect(screen.getByText('Summary')).toBeInTheDocument();
  });
});

describe('per-message actions survive the regrouping', () => {
  it('detaches the row that was actually clicked', async () => {
    // The thing most likely to break silently: these were always keyed on the
    // attachment's own row id, and a chain is only a view over those rows.
    const user = userEvent.setup();
    vi.mocked(api.del).mockResolvedValue({ ok: true });
    const c = chain({
      messages: [
        email({ id: 11, date: '2026-08-19T09:00:00+00:00' }),
        email({ id: 22, message_id: 'google:5:m2', date: '2026-08-20T09:00:00+00:00' }),
      ],
    });
    renderCard(c);
    await user.click(screen.getByRole('button', { name: /Atlas cutover/ }));

    const detachButtons = screen.getAllByRole('button', { name: 'Detach this email' });
    await user.click(detachButtons[1]);

    expect(api.del).toHaveBeenCalledWith('/threads/7/emails/22');
  });

  it('marks a message read when its body is revealed, not when the chain opens', async () => {
    const user = userEvent.setup();
    vi.mocked(api.post).mockResolvedValue({ ok: true });
    const c = chain({
      unread_count: 1,
      messages: [
        email({ id: 11, date: '2026-08-19T09:00:00+00:00' }),
        email({
          id: 22,
          message_id: 'google:5:m2',
          unread: true,
          auto_attached: true,
          date: '2026-08-20T09:00:00+00:00',
        }),
      ],
    });
    renderCard(c);

    await user.click(screen.getByRole('button', { name: /Atlas cutover/ }));

    // The newest opens by default, so that one is read; the other is not.
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/threads/7/emails/22/read'));
    expect(api.post).not.toHaveBeenCalledWith('/threads/7/emails/11/read');
  });

  it('marks every unread message read from the header button', async () => {
    const user = userEvent.setup();
    vi.mocked(api.post).mockResolvedValue({ ok: true });
    renderCard(
      chain({
        unread_count: 2,
        messages: [
          email({ id: 11, unread: true, date: '2026-08-19T09:00:00+00:00' }),
          email({
            id: 22,
            message_id: 'google:5:m2',
            unread: true,
            date: '2026-08-20T09:00:00+00:00',
          }),
        ],
      }),
    );

    await user.click(screen.getByRole('button', { name: /Mark 2 read/ }));

    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/threads/7/emails/11/read'));
    expect(api.post).toHaveBeenCalledWith('/threads/7/emails/22/read');
  });
});

describe('the iCloud gap', () => {
  it('says outbound mail may be missing on an all-inbound apple chain', () => {
    // An incomplete conversation presented as whole is the failure mode this
    // whole feature exists to remove.
    renderCard(
      chain({
        messages: [email({ provider: 'apple', direction: 'inbound' })],
      }),
    );
    expect(screen.getByText(/Outbound messages from this account may be missing/)).toBeInTheDocument();
  });

  it('says nothing for a gmail chain', () => {
    renderCard(chain({ messages: [email({ provider: 'google', direction: 'inbound' })] }));
    expect(
      screen.queryByText(/Outbound messages from this account may be missing/),
    ).not.toBeInTheDocument();
  });

  it('says nothing when the apple chain does contain outbound mail', () => {
    renderCard(
      chain({
        messages: [
          email({ id: 1, provider: 'apple', direction: 'outbound' }),
          email({ id: 2, message_id: 'm2', provider: 'apple', direction: 'inbound' }),
        ],
      }),
    );
    expect(
      screen.queryByText(/Outbound messages from this account may be missing/),
    ).not.toBeInTheDocument();
  });
});
