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
    summarisable: false,
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

function renderCard(c: EmailChain, opts: { hydrating?: boolean } = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <EmailChainCard chain={c} threadId="7" hydrating={opts.hydrating} />
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
    expect(screen.getByText('Needs your reply')).toBeInTheDocument();
    unmount();

    renderCard(chain({ awaiting: null, last_message_from: null }));
    expect(screen.queryByText('Needs your reply')).not.toBeInTheDocument();
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

describe('opt-in summaries', () => {
  it('offers to summarise only messages that have a body but no summary', async () => {
    renderCard(
      chain({
        messages: [
          email({ id: 1, has_body: true, ai_summary: 'already done', summarisable: false }),
          email({ id: 2, message_id: 'm2', has_body: true, summarisable: true }),
          email({ id: 3, message_id: 'm3', has_body: false, summarisable: false }),
        ],
      }),
    );

    // One eligible: #1 is done, #3 has no text to summarise.
    expect(screen.getByRole('button', { name: /Summarise message/ })).toBeInTheDocument();
  });

  it('says how many messages it will summarise', () => {
    renderCard(
      chain({
        messages: [
          email({ id: 1, has_body: true, summarisable: true }),
          email({ id: 2, message_id: 'm2', has_body: true, summarisable: true }),
        ],
      }),
    );
    expect(screen.getByRole('button', { name: /Summarise 2 messages/ })).toBeInTheDocument();
  });

  it('posts only the eligible ids, so one conversation does not pay for the thread', async () => {
    const user = userEvent.setup();
    vi.mocked(api.post).mockResolvedValue({ requested: 1, summarised: 1, failed: 0, remaining: 0 });
    renderCard(
      chain({
        messages: [
          email({ id: 11, has_body: true, ai_summary: 'done', summarisable: false }),
          email({ id: 22, message_id: 'm2', has_body: true, summarisable: true }),
        ],
      }),
    );

    await user.click(screen.getByRole('button', { name: /Summarise/ }));

    expect(api.post).toHaveBeenCalledWith('/threads/7/emails/summarise', {
      email_ids: [22],
    });
  });

  it('offers nothing when every message is already summarised', () => {
    renderCard(chain({ messages: [email({ has_body: true, ai_summary: 'done' })] }));
    expect(screen.queryByRole('button', { name: /Summarise/ })).not.toBeInTheDocument();
  });

  it('offers nothing when no message has a body yet', () => {
    // Nothing to summarise until hydration has run.
    renderCard(chain({ messages: [email({ has_body: false })] }));
    expect(screen.queryByRole('button', { name: /Summarise/ })).not.toBeInTheDocument();
  });

  it('surfaces a failure so the retry is discoverable', async () => {
    const user = userEvent.setup();
    vi.mocked(api.post).mockRejectedValue(new Error('llm down'));
    renderCard(chain({ messages: [email({ has_body: true, summarisable: true })] }));

    await user.click(screen.getByRole('button', { name: /Summarise/ }));

    await waitFor(() =>
      expect(screen.getByText(/Could not summarise/)).toBeInTheDocument(),
    );
  });
});

describe('bulk hydration in flight', () => {
  it('does not offer a Load button for work already under way', async () => {
    // Otherwise the card invites you to re-request a body the page is fetching.
    const user = userEvent.setup();
    renderCard(
      chain({ messages: [email({ has_body: false, body_fetched_at: null })] }),
      { hydrating: true },
    );
    await user.click(screen.getByRole('button', { name: /Atlas cutover/ }));

    expect(screen.getByText(/Fetching the full message/)).toBeInTheDocument();
  });

  it('offers the Load button once hydration has settled', async () => {
    const user = userEvent.setup();
    renderCard(chain({ messages: [email({ has_body: false, body_fetched_at: null })] }));
    await user.click(screen.getByRole('button', { name: /Atlas cutover/ }));

    expect(screen.getByRole('button', { name: /Load full message/ })).toBeInTheDocument();
  });
});

describe('collapsed messages inside an expanded chain', () => {
  it('shows a one-line preview rather than an empty row', async () => {
    // Found in a browser: a collapsed message rendered as a sender and a date
    // over blank space, which reads as content that failed to load. A
    // single-message chain already showed its preview while collapsed, so the
    // two were inconsistent as well.
    const user = userEvent.setup();
    renderCard(
      chain({
        messages: [
          email({ id: 1, snippet: 'The older message preview', date: '2026-08-19T09:00:00+00:00' }),
          email({ id: 2, message_id: 'm2', snippet: 'The newest one', date: '2026-08-20T09:00:00+00:00' }),
        ],
      }),
    );
    await user.click(screen.getByRole('button', { name: /Atlas cutover/ }));

    expect(screen.getByText('The older message preview')).toBeInTheDocument();
  });

  it('prefers the summary over the snippet in that preview', async () => {
    const user = userEvent.setup();
    renderCard(
      chain({
        messages: [
          email({ id: 1, snippet: 'raw snippet', ai_summary: 'the summary', date: '2026-08-19T09:00:00+00:00' }),
          email({ id: 2, message_id: 'm2', date: '2026-08-20T09:00:00+00:00' }),
        ],
      }),
    );
    await user.click(screen.getByRole('button', { name: /Atlas cutover/ }));

    expect(screen.getByText('the summary')).toBeInTheDocument();
    expect(screen.queryByText('raw snippet')).not.toBeInTheDocument();
  });

  it('replaces the preview with the real body once that message is opened', async () => {
    const user = userEvent.setup();
    vi.mocked(api.get).mockResolvedValue({
      id: 1, body: 'The full text of the older message.',
      body_fetched_at: 'x', has_body: true, ai_summary: null, ai_summary_model: null,
    });
    renderCard(
      chain({
        messages: [
          email({ id: 1, snippet: 'Only shown while folded', has_body: true,
                  date: '2026-08-19T09:00:00+00:00' }),
          email({ id: 2, message_id: 'm2', date: '2026-08-20T09:00:00+00:00' }),
        ],
      }),
    );
    await user.click(screen.getByRole('button', { name: /Atlas cutover/ }));
    expect(screen.getByText('Only shown while folded')).toBeInTheDocument();

    await user.click(screen.getAllByRole('button', { expanded: false })[0]);

    await waitFor(() =>
      expect(screen.getByText('The full text of the older message.')).toBeInTheDocument(),
    );
    expect(screen.queryByText('Only shown while folded')).not.toBeInTheDocument();
  });
});


describe('the summarise offer matches what the server will do', () => {
  it('offers nothing for a body the server considers too short', async () => {
    // Found in a browser: the card filtered on has_body && !ai_summary, which
    // omits the minimum-length rule. It offered "Summarise 3 messages", the
    // request came back `requested: 0`, and nothing changed. Only the server
    // knows the threshold, so only the server decides.
    renderCard(
      chain({
        messages: [email({ has_body: true, ai_summary: null, summarisable: false })],
      }),
    );
    expect(screen.queryByRole('button', { name: /Summarise/ })).not.toBeInTheDocument();
  });

  it('counts only the messages the server flagged', async () => {
    renderCard(
      chain({
        messages: [
          email({ id: 1, has_body: true, summarisable: true }),
          email({ id: 2, message_id: 'm2', has_body: true, summarisable: false }),
          email({ id: 3, message_id: 'm3', has_body: true, summarisable: true }),
        ],
      }),
    );
    expect(screen.getByRole('button', { name: /Summarise 2 messages/ })).toBeInTheDocument();
  });
});


describe('the awaiting badge says who owes what', () => {
  it('never labels an incoming message as something you wrote', async () => {
    // The old copy was "Your reply", a noun phrase -- on a message *from*
    // someone else it read as the app calling their mail your reply, which is
    // the opposite of what awaiting: 'you' means.
    renderCard(
      chain({
        awaiting: 'you',
        last_message_from: 'them',
        messages: [email({ direction: 'inbound', sender: 'james@acme.com' })],
      }),
    );

    expect(screen.getByText('Needs your reply')).toBeInTheDocument();
    expect(screen.queryByText('Your reply')).not.toBeInTheDocument();
  });

  it('uses the other label when you wrote last', async () => {
    renderCard(
      chain({
        awaiting: 'them',
        last_message_from: 'you',
        messages: [email({ direction: 'outbound' })],
      }),
    );

    expect(screen.getByText('Waiting on them')).toBeInTheDocument();
    expect(screen.queryByText('Needs your reply')).not.toBeInTheDocument();
  });

  it('allows dismissing the Needs your reply tag', async () => {
    const user = userEvent.setup();
    vi.mocked(api.post).mockResolvedValue({ ok: true });
    renderCard(
      chain({
        awaiting: 'you',
        last_message_from: 'them',
        messages: [email({ id: 42, direction: 'inbound', sender: 'james@acme.com' })],
      }),
    );

    expect(screen.getByText('Needs your reply')).toBeInTheDocument();
    const dismissBtn = screen.getByRole('button', { name: 'Dismiss reply tag' });
    expect(dismissBtn).toBeInTheDocument();

    await user.click(dismissBtn);

    expect(api.post).toHaveBeenCalledWith('/threads/7/emails/42/dismiss-reply');
  });
});

