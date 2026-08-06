import { useQuery } from '@tanstack/react-query';
import { ArrowRightLeft } from 'lucide-react';
import { useState } from 'react';
import { Select } from '@/components/ui/primitives';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import type { Paginated, Thread } from '@/types/api';

/**
 * "Move to…" control for one attached item: an icon that swaps to a thread
 * picker on click, the same reveal-on-click shape as ThreadTitle's rename.
 *
 * A native <select> because that is the only thread-picker widget anywhere in
 * the app (NewMeetingPage, UpcomingPanel both use one off the same
 * `['threads', 'picker']` query) -- there is no combobox component to reach
 * for instead. The list is fetched lazily, only once the picker opens, so
 * every timeline item doesn't pay for it up front.
 */
export function MoveToThread({
  currentThreadId,
  onMove,
  pending,
  label,
  className,
}: {
  currentThreadId: string;
  onMove: (targetThreadId: number) => void;
  pending: boolean;
  label: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const threads = useQuery({
    queryKey: ['threads', 'picker'],
    queryFn: () => api.get<Paginated<Thread>>('/threads', { page_size: 200 }),
    enabled: open,
  });

  if (open) {
    const options = (threads.data?.items ?? []).filter(
      (t) => String(t.id) !== currentThreadId,
    );
    return (
      <Select
        autoFocus
        aria-label={label}
        disabled={pending}
        className={cn('h-7 w-40 text-xs', className)}
        defaultValue=""
        onChange={(e) => {
          const id = Number(e.target.value);
          setOpen(false);
          if (id) onMove(id);
        }}
        onBlur={() => setOpen(false)}
      >
        <option value="" disabled>
          {threads.isLoading ? 'Loading threads…' : 'Move to…'}
        </option>
        {options.map((t) => (
          <option key={t.id} value={t.id}>
            {t.title}
          </option>
        ))}
      </Select>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setOpen(true)}
      disabled={pending}
      aria-label={label}
      title={label}
      className={cn(
        'shrink-0 rounded p-1 text-fg-faint transition-opacity',
        'opacity-0 group-hover:opacity-100 focus-visible:opacity-100',
        'hover:text-fg disabled:opacity-50',
        className,
      )}
    >
      <ArrowRightLeft className="size-3.5" aria-hidden />
    </button>
  );
}
