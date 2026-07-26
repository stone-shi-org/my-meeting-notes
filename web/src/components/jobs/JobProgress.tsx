import { AlertCircle, Check, Circle, Loader2, RotateCcw } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { cn } from '@/lib/cn';
import { fmtElapsed } from '@/lib/time';
import type { Job, JobEvent } from '@/types/api';

/** Ticking elapsed clock. Tabular so the digits don't jitter. */
export function ElapsedClock({ since, frozen }: { since: string | null; frozen?: string | null }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (frozen || !since) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [since, frozen]);

  if (!since) return <span className="font-mono tabular text-fg-faint">--:--</span>;

  const end = frozen ? new Date(frozen).getTime() : now;
  const seconds = Math.max(0, (end - new Date(since).getTime()) / 1000);
  return <span className="font-mono tabular">{fmtElapsed(seconds)}</span>;
}

type StageState = 'pending' | 'active' | 'done' | 'failed' | 'skipped';

function stageStates(job: Job, events: JobEvent[]): Record<string, StageState> {
  const states: Record<string, StageState> = {};
  const skipped = new Set(
    events
      .filter((e) => /^Skipped |already |no conversion needed|Already /i.test(e.message))
      .map((e) => e.stage)
      .filter(Boolean) as string[],
  );

  const currentIndex = job.stages.findIndex((s) => s.key === job.stage);

  job.stages.forEach((stage, i) => {
    if (job.status === 'failed' && stage.key === job.error_stage) {
      states[stage.key] = 'failed';
    } else if (job.status === 'succeeded') {
      states[stage.key] = skipped.has(stage.key) ? 'skipped' : 'done';
    } else if (currentIndex === -1) {
      states[stage.key] = 'pending';
    } else if (i < currentIndex) {
      states[stage.key] = skipped.has(stage.key) ? 'skipped' : 'done';
    } else if (i === currentIndex) {
      states[stage.key] = job.status === 'running' ? 'active' : 'pending';
    } else {
      states[stage.key] = 'pending';
    }
  });

  return states;
}

// Icon + shape + label, never colour alone.
function StageIcon({ state }: { state: StageState }) {
  if (state === 'done')
    return <Check className="size-4 text-success" aria-hidden />;
  if (state === 'skipped')
    return <Check className="size-4 text-fg-faint" aria-hidden />;
  if (state === 'failed')
    return <AlertCircle className="size-4 text-danger" aria-hidden />;
  if (state === 'active')
    return <Loader2 className="size-4 animate-spin text-primary" aria-hidden />;
  return <Circle className="size-4 text-fg-faint" aria-hidden />;
}

export function JobStageStepper({
  job,
  events,
}: {
  job: Job;
  events: JobEvent[];
}) {
  const states = stageStates(job, events);

  const lastPerStage = new Map<string, JobEvent>();
  for (const event of events) {
    if (event.stage) lastPerStage.set(event.stage, event);
  }

  return (
    <ol className="space-y-0" aria-live="polite" aria-atomic="false">
      {job.stages.map((stage, i) => {
        const state = states[stage.key];
        const detail = lastPerStage.get(stage.key);
        const isLast = i === job.stages.length - 1;

        return (
          <li
            key={stage.key}
            aria-current={state === 'active' ? 'step' : undefined}
            className="flex gap-3"
          >
            <div className="flex flex-col items-center">
              <div className="grid size-7 place-items-center">
                <StageIcon state={state} />
              </div>
              {!isLast && (
                <div
                  className={cn(
                    'w-px flex-1 transition-colors duration-slow',
                    state === 'done' || state === 'skipped' ? 'bg-success' : 'bg-border',
                  )}
                />
              )}
            </div>

            <div className={cn('min-w-0 flex-1', isLast ? 'pb-0' : 'pb-4')}>
              <div className="flex items-baseline justify-between gap-3">
                <span
                  className={cn(
                    'text-base',
                    state === 'active' && 'font-medium text-fg',
                    state === 'pending' && 'text-fg-faint',
                    state === 'skipped' && 'text-fg-subtle line-through decoration-fg-faint',
                    (state === 'done' || state === 'failed') && 'text-fg-muted',
                  )}
                >
                  {stage.label}
                </span>
              </div>

              {detail && state !== 'pending' && (
                <p
                  className={cn(
                    'mt-0.5 text-sm',
                    detail.level === 'error' ? 'text-danger-ink' : 'text-fg-subtle',
                  )}
                >
                  {detail.message}
                </p>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

export function JobErrorPanel({
  job,
  onRetry,
  retrying,
}: {
  job: Job;
  onRetry?: () => void;
  retrying?: boolean;
}) {
  const hint =
    job.error?.includes('diarization')
      ? '/settings/diarization'
      : job.error?.toLowerCase().includes('llm')
        ? '/settings/llm'
        : job.error?.toLowerCase().includes('mcp')
          ? '/settings/mcp'
          : null;

  return (
    <div className="rounded-md border border-danger/30 bg-danger-soft/40 p-4">
      <div className="flex items-start gap-3">
        <AlertCircle className="mt-0.5 size-5 shrink-0 text-danger" aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="font-medium text-danger-ink">
            Failed{job.error_stage ? ` during ${job.error_stage}` : ''}
          </p>
          <p className="mt-1 break-words text-sm text-fg-muted">{job.error}</p>

          {hint && (
            <p className="mt-2 text-sm">
              <Link to={hint} className="text-primary underline-offset-4 hover:underline">
                This looks like a configuration problem — open settings
              </Link>
            </p>
          )}

          <div className="mt-3 flex flex-wrap gap-2">
            {onRetry && (
              <Button size="sm" variant="secondary" onClick={onRetry} loading={retrying}>
                <RotateCcw />
                Retry
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                void navigator.clipboard.writeText(
                  JSON.stringify(
                    { id: job.id, stage: job.error_stage, error: job.error },
                    null,
                    2,
                  ),
                )
              }
            >
              Copy diagnostics
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function JobLogStream({ events }: { events: JobEvent[] }) {
  const [open, setOpen] = useState(false);
  if (!events.length) return null;

  return (
    <div className="mt-3">
      <button
        onClick={() => setOpen((v) => !v)}
        className="text-sm text-fg-subtle underline-offset-4 hover:text-fg hover:underline"
        aria-expanded={open}
      >
        {open ? 'Hide' : 'Show'} detail ({events.length})
      </button>
      {open && (
        <ul className="mt-2 max-h-56 space-y-0.5 overflow-y-auto rounded border border-border bg-surface-2 p-2 font-mono text-2xs">
          {events.map((e) => (
            <li
              key={e.id}
              className={cn(
                e.level === 'error' && 'text-danger-ink',
                e.level === 'warn' && 'text-warning-ink',
                e.level === 'info' && 'text-fg-muted',
              )}
            >
              <span className="text-fg-faint">{e.ts.slice(11, 19)}</span>{' '}
              {e.stage && <span className="text-fg-faint">[{e.stage}]</span>} {e.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
