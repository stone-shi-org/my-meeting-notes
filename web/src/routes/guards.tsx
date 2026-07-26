import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { Card } from '@/components/ui/primitives';
import { Spinner } from '@/components/ui/primitives';
import { useAuth } from '@/hooks/useAuth';

function FullPageSpinner() {
  return (
    <div className="grid min-h-dvh place-items-center">
      <Spinner className="size-6 text-primary" />
    </div>
  );
}

export function RequireAuth() {
  const { status } = useAuth();
  const location = useLocation();

  if (status === 'loading') return <FullPageSpinner />;
  if (status === 'anonymous') {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?next=${next}`} replace />;
  }
  return <Outlet />;
}

/**
 * Blocks everything until a forced password change is done.
 *
 * /change-password sits inside RequireAuth but *outside* this guard -- putting
 * it inside is the classic redirect loop in this flow.
 */
export function RequirePasswordChanged() {
  const { mustChangePassword } = useAuth();
  if (mustChangePassword) return <Navigate to="/change-password" replace />;
  return <Outlet />;
}

export function RequireAdmin() {
  const { isAdmin } = useAuth();

  // A 403 state rather than a redirect: silently bouncing an admin-only link
  // looks like the app is broken.
  if (!isAdmin) {
    return (
      <Card className="p-8 text-center">
        <h2 className="font-display text-lg font-semibold">Administrators only</h2>
        <p className="mt-1 text-sm text-fg-subtle">
          Ask an administrator if you need access to this section.
        </p>
      </Card>
    );
  }
  return <Outlet />;
}
