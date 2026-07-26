import { ChevronDown, ChevronUp, Loader2 } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useActiveJobs } from '@/hooks/useJob';
import { ElapsedClock } from './JobProgress';
import { cn } from '@/lib/cn';

const TYPE_LABEL: Record<string, string> = {
  ingest: 'Processing recording',
  diarize: 'Re-transcribing',
  summarize: 'Summarizing',
  match: 'Finding related items',
};

/**
 * Persistent active-job indicator.
 *
 * Rendered by the shell so it is present on every screen -- the second of the
 * three layers that make a minutes-long job impossible to lose track of (the
 * others being the /jobs/:id URL and the inline status on meeting rows).
 */
export function JobDock() {
  const { data: jobs } = useActiveJobs();
  const [collapsed, setCollapsed] = useState(false);

  if (!jobs?.length) return null;

  return (
    <div className="fixed bottom-0 right-0 z-40 w-full p-3 sm:bottom-4 sm:right-4 sm:w-auto sm:p-0"
         style={{ paddingBottom: 'max(0.75rem, env(safe-area-inset-bottom))' }}>
      <div className="overflow-hidden rounded-lg border border-border bg-surface shadow-lg sm:w-80">
        <button
          onClick={() => setCollapsed((v) => !v)}
          className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface-2"
          aria-expanded={!collapsed}
        >
          <Loader2 className="size-4 shrink-0 animate-spin text-primary" aria-hidden />
          <span className="flex-1 text-sm font-medium">
            {jobs.length} job{jobs.length > 1 ? 's' : ''} running
          </span>
          {collapsed ? (
            <ChevronUp className="size-4 text-fg-faint" aria-hidden />
          ) : (
            <ChevronDown className="size-4 text-fg-faint" aria-hidden />
          )}
        </button>

        {!collapsed && (
          <ul className="border-t border-border">
            {jobs.map((job) => (
              <li key={job.id}>
                <Link
                  to={`/jobs/${job.id}`}
                  className="flex items-center gap-3 px-3 py-2.5 hover:bg-surface-2"
                >
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">
                      {TYPE_LABEL[job.type] ?? job.type}
                    </p>
                    <p className="truncate text-xs text-fg-subtle">
                      {job.stage ?? job.status}
                    </p>
                  </div>
                  <div className="text-right">
                    <p className="text-xs text-fg-subtle">
                      <ElapsedClock since={job.started_at ?? job.created_at} />
                    </p>
                    <p className="text-2xs text-fg-faint tabular">
                      {Math.round(job.progress * 100)}%
                    </p>
                  </div>
                </Link>
                <div className="h-0.5 bg-surface-2">
                  <div
                    className={cn('h-full bg-primary transition-[width] duration-slow ease-out')}
                    style={{ width: `${Math.round(job.progress * 100)}%` }}
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
