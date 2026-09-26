import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UsersSettingsPage } from '../SettingsPage';
import type { UserStatisticsDashboard } from '@/types/api';

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), del: vi.fn(), patch: vi.fn(), put: vi.fn() },
}));

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { id: 1, username: 'admin', is_admin: true }, isAdmin: true }),
}));

const { api } = await import('@/lib/api');

const SAMPLE_DASHBOARD: UserStatisticsDashboard = {
  summary: {
    total_users: 2,
    active_users: 2,
    problems_solved_7d: 5,
    problems_solved_total: 12,
    problems_open: 3,
    total_meetings: 8,
  },
  users: [
    {
      id: 1,
      username: 'admin',
      display_name: 'Admin User',
      is_admin: true,
      is_active: true,
      must_change_password: false,
      created_at: '2026-09-01T00:00:00Z',
      last_login_at: '2026-09-26T15:00:00Z',
      problems_solved_7d: 3,
      problems_solved_total: 8,
      problems_open: 2,
      meeting_count: 5,
      thread_count: 3,
    },
    {
      id: 2,
      username: 'jdoe',
      display_name: 'Jane Doe',
      is_admin: false,
      is_active: true,
      must_change_password: false,
      created_at: '2026-09-10T00:00:00Z',
      last_login_at: null,
      problems_solved_7d: 2,
      problems_solved_total: 4,
      problems_open: 1,
      meeting_count: 3,
      thread_count: 2,
    },
  ],
};

function renderPage(data: UserStatisticsDashboard = SAMPLE_DASHBOARD) {
  vi.mocked(api.get).mockImplementation((url: string) => {
    if (url === '/users/statistics') return Promise.resolve(data);
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });

  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  return render(
    <QueryClientProvider client={client}>
      <UsersSettingsPage />
    </QueryClientProvider>,
  );
}

describe('UsersSettingsPage - User Statistic Dashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders summary overview cards', async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Total Users')).toBeInTheDocument();
    });

    expect(screen.getAllByText('Solved (7d)')).toHaveLength(2);
    expect(screen.getAllByText('Solved (Total)')).toHaveLength(2);
    expect(screen.getByText('Open Problems')).toBeInTheDocument();
    expect(screen.getByText('Total Meetings')).toBeInTheDocument();

    // Verify values from SAMPLE_DASHBOARD
    expect(screen.getByText('2 active')).toBeInTheDocument();
    expect(screen.getAllByText('5').length).toBeGreaterThanOrEqual(1); // solved 7d
    expect(screen.getByText('12')).toBeInTheDocument(); // solved total
    expect(screen.getAllByText('3').length).toBeGreaterThanOrEqual(1); // open problems
    expect(screen.getAllByText('8').length).toBeGreaterThanOrEqual(1); // total meetings
  });

  it('renders user table with statistics and handles search filtering', async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Admin User')).toBeInTheDocument();
      expect(screen.getByText('Jane Doe')).toBeInTheDocument();
    });

    expect(screen.getByText('Never')).toBeInTheDocument(); // jdoe has null last_login_at

    // Filter by search
    const searchInput = screen.getByPlaceholderText('Filter users...');
    await user.type(searchInput, 'Jane');

    expect(screen.queryByText('Admin User')).not.toBeInTheDocument();
    expect(screen.getByText('Jane Doe')).toBeInTheDocument();

    await user.clear(searchInput);
    expect(screen.getByText('Admin User')).toBeInTheDocument();
  });

  it('handles password reset', async () => {
    const user = userEvent.setup();
    vi.mocked(api.post).mockResolvedValueOnce({ temporary_password: 'temp-secret-pwd' });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Jane Doe')).toBeInTheDocument();
    });

    const resetButtons = screen.getAllByRole('button', { name: 'Reset password' });
    await user.click(resetButtons[1]); // Reset jdoe's password

    expect(api.post).toHaveBeenCalledWith('/users/2/reset-password', {});
    await waitFor(() => {
      expect(screen.getByText('temp-secret-pwd')).toBeInTheDocument();
    });
  });
});
