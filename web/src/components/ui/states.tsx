import { AlertTriangle, type LucideIcon } from 'lucide-react';
import { Button } from './Button';
import { cn } from '@/lib/cn';
import { ApiError } from '@/types/api';
import { Link } from 'react-router-dom';

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: {
  icon: LucideIcon;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('flex flex-col items-center px-6 py-16 text-center', className)}>
      <div className="mb-4 grid size-14 place-items-center rounded-2xl bg-primary-soft">
        <Icon className="size-6 text-primary-soft-fg" aria-hidden />
      </div>
      <h3 className="font-display text-lg font-semibold">{title}</h3>
      {description && <p className="mt-1 max-w-sm text-sm text-fg-subtle">{description}</p>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
  className,
}: {
  error: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  const apiError = error instanceof ApiError ? error : null;
  const message = error instanceof Error ? error.message : String(error);
  const hint = apiError?.settingsHint ?? null;

  return (
    <div className={cn('rounded-lg border border-danger/30 bg-danger-soft/40 p-5', className)}>
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 size-5 shrink-0 text-danger" aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="font-medium text-danger-ink">Something went wrong</p>
          <p className="mt-1 break-words text-sm text-fg-muted">{message}</p>

          {hint && (
            <p className="mt-2 text-sm">
              <Link to={hint} className="text-primary underline-offset-4 hover:underline">
                This looks like a configuration problem — open settings
              </Link>
            </p>
          )}

          {onRetry && (
            <Button size="sm" variant="secondary" className="mt-3" onClick={onRetry}>
              Try again
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

export function Pagination({
  page,
  pageSize,
  total,
  totalPages,
  onPage,
  onPageSize,
}: {
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
  onPage: (page: number) => void;
  onPageSize?: (size: number) => void;
}) {
  if (total === 0) return null;

  const first = (page - 1) * pageSize + 1;
  const last = Math.min(page * pageSize, total);

  // Windowed page numbers with ellipses, so 40 pages don't wrap the bar.
  const numbers: (number | '…')[] = [];
  const window = 1;
  for (let p = 1; p <= totalPages; p++) {
    if (p === 1 || p === totalPages || Math.abs(p - page) <= window) {
      numbers.push(p);
    } else if (numbers[numbers.length - 1] !== '…') {
      numbers.push('…');
    }
  }

  return (
    <nav
      aria-label="Pagination"
      className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4"
    >
      <p className="text-sm text-fg-subtle">
        Showing <span className="tabular">{first}</span>–<span className="tabular">{last}</span> of{' '}
        <span className="tabular">{total}</span>
      </p>

      <div className="flex items-center gap-2">
        {onPageSize && (
          <select
            aria-label="Items per page"
            value={pageSize}
            onChange={(e) => onPageSize(Number(e.target.value))}
            className="h-8 rounded border border-border-strong bg-surface px-2 text-sm"
          >
            {[9, 12, 20, 50].map((n) => (
              <option key={n} value={n}>
                {n} / page
              </option>
            ))}
          </select>
        )}

        <Button size="sm" variant="ghost" disabled={page <= 1} onClick={() => onPage(page - 1)}>
          Prev
        </Button>

        <div className="hidden items-center gap-1 sm:flex">
          {numbers.map((n, i) =>
            n === '…' ? (
              <span key={`gap-${i}`} className="px-1 text-fg-faint">
                …
              </span>
            ) : (
              <Button
                key={n}
                size="icon-sm"
                variant={n === page ? 'primary' : 'ghost'}
                aria-current={n === page ? 'page' : undefined}
                onClick={() => onPage(n)}
              >
                {n}
              </Button>
            ),
          )}
        </div>
        <span className="text-sm text-fg-subtle sm:hidden tabular">
          {page} / {totalPages}
        </span>

        <Button
          size="sm"
          variant="ghost"
          disabled={page >= totalPages}
          onClick={() => onPage(page + 1)}
        >
          Next
        </Button>
      </div>
    </nav>
  );
}
