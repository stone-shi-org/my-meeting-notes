import { KeyRound } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { Input, Label } from '@/components/ui/primitives';
import { useAuth } from '@/hooks/useAuth';
import { api } from '@/lib/api';

export function ChangePasswordPage() {
  const { user, mustChangePassword, refresh, logout } = useAuth();
  const navigate = useNavigate();

  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (next !== confirm) {
      setError('The two new passwords do not match');
      return;
    }

    setBusy(true);
    try {
      await api.post('/auth/change-password', {
        current_password: current,
        new_password: next,
      });
      refresh();
      navigate('/', { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not change password');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-dvh place-items-center bg-bg px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 grid size-12 place-items-center rounded-2xl bg-warning-soft">
            <KeyRound className="size-6 text-warning-ink" aria-hidden />
          </div>
          <h1 className="font-display text-2xl font-semibold">
            {mustChangePassword ? 'Choose a new password' : 'Change your password'}
          </h1>
          {mustChangePassword && (
            <p className="mt-1 text-sm text-fg-subtle">
              {user?.username} is still on its initial password. Pick a new one to continue.
            </p>
          )}
        </div>

        <form
          onSubmit={submit}
          className="rounded-xl border border-border bg-surface p-6 shadow-sm"
        >
          <div className="space-y-4">
            <div>
              <Label htmlFor="current">Current password</Label>
              <Input
                id="current"
                className="mt-1.5"
                type="password"
                autoComplete="current-password"
                required
                autoFocus
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
              />
            </div>

            <div>
              <Label htmlFor="next">New password</Label>
              <Input
                id="next"
                className="mt-1.5"
                type="password"
                autoComplete="new-password"
                required
                minLength={10}
                value={next}
                onChange={(e) => setNext(e.target.value)}
              />
              <p className="mt-1 text-xs text-fg-subtle">At least 10 characters.</p>
            </div>

            <div>
              <Label htmlFor="confirm">Confirm new password</Label>
              <Input
                id="confirm"
                className="mt-1.5"
                type="password"
                autoComplete="new-password"
                required
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </div>

            {error && (
              <p role="alert" className="text-sm text-danger-ink">
                {error}
              </p>
            )}

            <Button type="submit" variant="primary" size="lg" className="w-full" loading={busy}>
              Update password
            </Button>

            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="w-full"
              onClick={() => void logout()}
            >
              Sign out instead
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
