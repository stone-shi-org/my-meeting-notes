import {
  ChevronDown,
  LogOut,
  Mic,
  Monitor,
  Moon,
  Settings as SettingsIcon,
  Sun,
  Layers,
} from 'lucide-react';
import { useState } from 'react';
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { Footer } from '@/components/layout/Footer';
import { JobDock } from '@/components/jobs/JobDock';
import { useAuth } from '@/hooks/useAuth';
import { useTheme } from '@/hooks/useTheme';
import { cn } from '@/lib/cn';

const NAV = [
  { to: '/', label: 'Threads', icon: Layers, end: true },
  { to: '/settings', label: 'Settings', icon: SettingsIcon, end: false },
];

function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const next = theme === 'light' ? 'dark' : theme === 'dark' ? 'system' : 'light';
  const Icon = theme === 'light' ? Sun : theme === 'dark' ? Moon : Monitor;

  return (
    <Button
      size="icon"
      variant="ghost"
      onClick={() => setTheme(next)}
      aria-label={`Theme: ${theme}. Switch to ${next}.`}
      title={`Theme: ${theme}`}
    >
      <Icon />
    </Button>
  );
}

function UserMenu() {
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  if (!user) return null;

  const initials = (user.display_name || user.username).slice(0, 2).toUpperCase();

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        className="flex items-center gap-2 rounded px-1.5 py-1 hover:bg-surface-2"
        aria-expanded={open}
        aria-haspopup="menu"
      >
        <span className="grid size-7 place-items-center rounded-full bg-primary-soft text-xs font-semibold text-primary-soft-fg">
          {initials}
        </span>
        <ChevronDown className="size-3.5 text-fg-faint" aria-hidden />
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full z-50 mt-1 w-56 rounded-md border border-border bg-surface p-1 shadow-lg animate-fade-in"
        >
          <div className="border-b border-border px-3 py-2">
            <p className="truncate text-sm font-medium">{user.display_name || user.username}</p>
            <p className="truncate text-xs text-fg-subtle">
              {user.username}
              {user.is_admin && ' · admin'}
            </p>
          </div>
          <Link
            to="/settings"
            role="menuitem"
            className="flex w-full items-center gap-2 rounded px-3 py-2 text-sm hover:bg-surface-2"
          >
            <SettingsIcon className="size-4" aria-hidden />
            Settings
          </Link>
          <button
            role="menuitem"
            onClick={() => void logout()}
            className="flex w-full items-center gap-2 rounded px-3 py-2 text-sm text-danger-ink hover:bg-surface-2"
          >
            <LogOut className="size-4" aria-hidden />
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}

export function AppShell() {
  const location = useLocation();

  return (
    <div className="min-h-dvh bg-bg">
      <a
        href="#main"
        className="sr-only-focusable absolute left-4 top-4 z-[100] rounded bg-primary px-3 py-2 text-sm text-primary-fg"
      >
        Skip to content
      </a>

      <header className="sticky top-0 z-40 border-b border-border bg-surface/85 backdrop-blur-xl">
        <div className="mx-auto flex h-14 max-w-[1600px] items-center gap-6 px-4 sm:px-6 lg:px-8">
          <Link to="/" className="flex items-center gap-2">
            <Mic className="size-5 text-primary" aria-hidden />
            <span className="bg-gradient-to-br from-primary to-primary-hover bg-clip-text font-display text-lg font-bold text-transparent">
              Meeting Notes
            </span>
          </Link>

          <nav aria-label="Main" className="hidden items-center gap-1 sm:flex">
            {NAV.map(({ to, label, icon: Icon, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  cn(
                    'flex items-center gap-1.5 rounded px-3 py-1.5 text-base font-medium transition-colors duration-fast',
                    isActive
                      ? 'bg-primary-soft text-primary-soft-fg'
                      : 'text-fg-muted hover:bg-surface-2 hover:text-fg',
                  )
                }
              >
                <Icon className="size-4" aria-hidden />
                {label}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-1">
            <ThemeToggle />
            <UserMenu />
          </div>
        </div>

        {/* Mobile nav: the top bar is too tight for labels under 640px. */}
        <nav
          aria-label="Main"
          className="flex items-center gap-1 overflow-x-auto border-t border-border px-4 py-1.5 sm:hidden"
        >
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-1.5 whitespace-nowrap rounded px-3 py-1.5 text-sm font-medium',
                  isActive ? 'bg-primary-soft text-primary-soft-fg' : 'text-fg-muted',
                )
              }
            >
              <Icon className="size-4" aria-hidden />
              {label}
            </NavLink>
          ))}
        </nav>
      </header>

      <main id="main" key={location.pathname} className="mx-auto max-w-[1600px] px-4 py-6 sm:px-6 lg:px-8">
        <Outlet />
      </main>

      <Footer />

      <JobDock />
    </div>
  );
}
