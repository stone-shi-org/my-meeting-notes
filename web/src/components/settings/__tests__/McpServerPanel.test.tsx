import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { clientSnippets, McpServerPanel } from '../McpServerPanel';
import type { ApiToken, ApiTokenList } from '@/types/api';

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), del: vi.fn(), patch: vi.fn(), put: vi.fn() },
}));
vi.mock('@/hooks/useAuth', () => ({ useAuth: vi.fn() }));
vi.mock('@/lib/clipboard', () => ({ copyText: vi.fn() }));

const { api } = await import('@/lib/api');
const { useAuth } = await import('@/hooks/useAuth');
const { copyText } = await import('@/lib/clipboard');

function token(over: Partial<ApiToken> = {}): ApiToken {
  return {
    id: 1,
    name: 'Claude Code',
    prefix: 'mmn_AbCd',
    scope: 'read',
    created_at: '2026-10-01T10:00:00+00:00',
    last_used_at: null,
    expires_at: null,
    revoked_at: null,
    state: 'active',
    ...over,
  };
}

function listing(over: Partial<ApiTokenList> = {}): ApiTokenList {
  return {
    mcp_enabled: true,
    endpoint_path: '/mcp',
    tools: [
      { name: 'search', title: 'Search everything', write: false },
      { name: 'get_meeting_transcript', title: 'Get a meeting transcript', write: false },
      { name: 'create_note', title: 'Create a note', write: true },
    ],
    tokens: [],
    ...over,
  };
}

function renderPanel(list: ApiTokenList = listing(), mcpEnabled: boolean | undefined = undefined) {
  vi.mocked(api.get).mockImplementation(((path: string) => {
    if (path === '/tokens') return Promise.resolve(list);
    if (path === '/settings') {
      return Promise.resolve({
        settings:
          mcpEnabled === undefined
            ? {}
            : { mcp_enabled: { value: mcpEnabled, type: 'bool', is_secret: false, overridden: true } },
      });
    }
    return Promise.reject(new Error(`unexpected GET ${path}`));
  }) as never);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <McpServerPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useAuth).mockReturnValue({
    isAdmin: false,
    user: null,
    status: 'authenticated',
    mustChangePassword: false,
    login: vi.fn(),
    logout: vi.fn(),
    refresh: vi.fn(),
  });
  vi.mocked(copyText).mockResolvedValue(true);
});

describe('McpServerPanel', () => {
  it('shows the endpoint for this origin and the tool list split by scope', async () => {
    renderPanel();
    expect(await screen.findByText(`${window.location.origin}/mcp`)).toBeInTheDocument();
    expect(screen.getByText('3 tools available')).toBeInTheDocument();
    expect(screen.getByText('create_note')).toBeInTheDocument();
    expect(screen.getByText('No tokens yet.')).toBeInTheDocument();
    expect(screen.getByLabelText('MCP server is on')).toBeInTheDocument();
  });

  it('reflects the switch from app settings, falling back to the listing', async () => {
    renderPanel(listing({ mcp_enabled: true }), false);
    expect(await screen.findByLabelText('MCP server is off')).toBeInTheDocument();
  });

  it('creates a token and reveals it once, with ready-to-paste client config', async () => {
    const user = userEvent.setup();
    vi.mocked(api.post).mockResolvedValue({
      ...token({ scope: 'read_write' }),
      token: 'mmn_secret-value',
    } as never);
    renderPanel();

    await user.type(await screen.findByLabelText('Name'), 'Claude Code');
    await user.selectOptions(screen.getByLabelText('Access'), 'read_write');
    await user.selectOptions(screen.getByLabelText('Expires'), '90');
    await user.click(screen.getByRole('button', { name: 'Create token' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/tokens', {
        name: 'Claude Code',
        scope: 'read_write',
        expires_in_days: 90,
      }),
    );
    const reveal = await screen.findByRole('status');
    expect(within(reveal).getByText('mmn_secret-value')).toBeInTheDocument();
    expect(within(reveal).getByText(/only time it is shown/)).toBeInTheDocument();
    expect(
      within(reveal).getByText(/claude mcp add --transport http my-meeting-notes/),
    ).toBeInTheDocument();

    await user.click(within(reveal).getAllByRole('button', { name: 'Copy' })[0]);
    expect(copyText).toHaveBeenCalledWith('mmn_secret-value');
    expect(await within(reveal).findByText('Copied')).toBeInTheDocument();

    await user.click(within(reveal).getByRole('button', { name: 'Done' }));
    expect(screen.queryByText('mmn_secret-value')).not.toBeInTheDocument();
  });

  it('says so when the copy did not happen', async () => {
    const user = userEvent.setup();
    vi.mocked(copyText).mockResolvedValue(false);
    renderPanel();
    await user.click((await screen.findAllByRole('button', { name: 'Copy' }))[0]);
    expect(await screen.findByText(/Copy failed/)).toBeInTheDocument();
  });

  it('lists tokens and revokes one through a confirmation dialog', async () => {
    const user = userEvent.setup();
    vi.mocked(api.del).mockResolvedValue(token({ state: 'revoked' }) as never);
    renderPanel(
      listing({
        tokens: [
          token({ last_used_at: '2026-10-01T11:00:00+00:00' }),
          token({ id: 2, name: 'Old laptop', state: 'revoked', revoked_at: '2026-09-01T00:00:00+00:00' }),
        ],
      }),
    );

    const row = (await screen.findByRole('rowheader', { name: /Claude Code/ })).closest('tr')!;
    expect(within(row).getByText('Read only')).toBeInTheDocument();
    expect(within(row).getByText('Active')).toBeInTheDocument();
    const revokedRow = screen.getByRole('rowheader', { name: /Old laptop/ }).closest('tr')!;
    expect(within(revokedRow).queryByRole('button', { name: /Revoke/ })).not.toBeInTheDocument();

    await user.click(within(row).getByRole('button', { name: 'Revoke Claude Code' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/stop working immediately/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Revoke' }));
    await waitFor(() => expect(api.del).toHaveBeenCalledWith('/tokens/1'));
  });
});

describe('clientSnippets', () => {
  it('builds a Claude Code command and an mcp-remote config around the token', () => {
    const s = clientSnippets('https://notes.example/mcp', 'mmn_x');
    expect(s.claudeCode).toBe(
      'claude mcp add --transport http my-meeting-notes https://notes.example/mcp --header "Authorization: Bearer mmn_x"',
    );
    const config = JSON.parse(s.json);
    expect(config.mcpServers['my-meeting-notes'].args).toContain('https://notes.example/mcp');
    expect(config.mcpServers['my-meeting-notes'].env.MMN_AUTH).toBe('Bearer mmn_x');
  });
});
