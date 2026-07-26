import { useQuery, useQueryClient } from '@tanstack/react-query';
import { createContext, useCallback, useContext, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, setPasswordChangeHandler, setUnauthorizedHandler } from '@/lib/api';
import { ApiError, type User } from '@/types/api';

interface AuthValue {
  user: User | null;
  status: 'loading' | 'authenticated' | 'anonymous';
  isAdmin: boolean;
  mustChangePassword: boolean;
  login: (username: string, password: string) => Promise<{ must_change_password: boolean }>;
  logout: () => Promise<void>;
  refresh: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const { data, isLoading, isError } = useQuery({
    queryKey: ['me'],
    queryFn: () => api.get<User>('/auth/me'),
    retry: (count, error) => !(error instanceof ApiError && error.status === 401) && count < 2,
    staleTime: 30_000,
  });

  // A session that expires mid-use should degrade to the login screen from any
  // route, not leave the user staring at empty cards.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      queryClient.setQueryData(['me'], null);
      const here = window.location.pathname + window.location.search;
      if (!here.startsWith('/login')) {
        navigate(`/login?next=${encodeURIComponent(here)}`, { replace: true });
      }
    });
    setPasswordChangeHandler(() => {
      if (window.location.pathname !== '/change-password') {
        navigate('/change-password', { replace: true });
      }
    });
  }, [queryClient, navigate]);

  const login = useCallback(
    async (username: string, password: string) => {
      const result = await api.post<{ user: User; must_change_password: boolean }>(
        '/auth/login',
        { username, password },
      );
      queryClient.setQueryData(['me'], result.user);
      await queryClient.invalidateQueries({ queryKey: ['me'] });
      return { must_change_password: result.must_change_password };
    },
    [queryClient],
  );

  const logout = useCallback(async () => {
    try {
      await api.post('/auth/logout');
    } finally {
      queryClient.clear();
      navigate('/login', { replace: true });
    }
  }, [queryClient, navigate]);

  const value = useMemo<AuthValue>(() => {
    const user = isError ? null : (data ?? null);
    return {
      user,
      status: isLoading ? 'loading' : user ? 'authenticated' : 'anonymous',
      isAdmin: !!user?.is_admin,
      mustChangePassword: !!user?.must_change_password,
      login,
      logout,
      refresh: () => queryClient.invalidateQueries({ queryKey: ['me'] }),
    };
  }, [data, isLoading, isError, login, logout, queryClient]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside AuthProvider');
  return value;
}
