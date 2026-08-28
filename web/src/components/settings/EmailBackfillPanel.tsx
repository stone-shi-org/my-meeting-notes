/**
 * How far along the email backfill is, and a way to finish it.
 *
 * Bodies and summaries are fetched lazily, per thread, as you open it. That is
 * the right default but it is invisible: an account with two hundred attached
 * emails across threads nobody has revisited stays mostly un-backfilled and
 * nothing says so. This panel says so.
 *
 * Each run button drives the same bounded server call in a loop, the way
 * `useEmailHydration` does for one thread. Nothing is queued: the server's
 * predicates *are* the resume state, so navigating away mid-run loses nothing
 * and pressing the button again continues from where it stopped.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, Check, Download, Sparkles } from 'lucide-react';
import { useRef, useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Badge, Card, Meter, Skeleton } from '@/components/ui/primitives';
import { ErrorState } from '@/components/ui/states';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import type { BackfillRunResult, EmailBackfillStats } from '@/types/api';

/**
 * A backstop on each loop. Bodies move a screenful per round and summaries
 * eight, so this is a very large account's worth -- it exists so a server that
 * kept reporting work left could never spin the browser forever.
 */
const MAX_ROUNDS = 200;

function pct(part: number, whole: number): number {
  return whole > 0 ? part / whole : 0;
}

/** One number with its label, in the counts grid. */
function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: number;
  hint?: string;
  tone?: 'muted';
}) {
  return (
    <div>
      <p
        className={cn(
          'tabular text-2xl font-semibold',
          tone === 'muted' ? 'text-fg-muted' : 'text-fg',
        )}
      >
        {value.toLocaleString()}
      </p>
      <p className="text-xs font-medium text-fg-muted">{label}</p>
      {hint ? <p className="mt-0.5 text-2xs text-fg-subtle">{hint}</p> : null}
    </div>
  );
}

/**
 * One backfill pass: a progress line, a button, and whatever the last round
 * said. Bodies and summaries differ only in their endpoint and their copy, so
 * they share this rather than being written twice.
 */
function RunSection({
  title,
  description,
  endpoint,
  done,
  outstanding,
  total,
  actionLabel,
  icon: Icon,
  costWarning,
}: {
  title: string;
  description: string;
  endpoint: string;
  done: number;
  outstanding: number;
  total: number;
  actionLabel: string;
  icon: typeof Download;
  /** Shown next to the button when the run costs money per item. */
  costWarning?: string;
}) {
  const queryClient = useQueryClient();
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const cancelled = useRef(false);

  const run = useMutation({
    mutationFn: () => api.post<BackfillRunResult>(endpoint),
  });

  async function start() {
    cancelled.current = false;
    setRunning(true);
    setFailed(null);
    setStatus(null);
    try {
      for (let round = 0; round < MAX_ROUNDS; round += 1) {
        const result = await run.mutateAsync();
        if (cancelled.current) return;
        if (result.done) {
          setStatus('All done.');
          break;
        }
        // A batch that asked for work and completed none means the provider or
        // the model is failing. Those rows stay eligible on purpose, so without
        // this the loop would run until MAX_ROUNDS achieving nothing.
        if (result.stalled) {
          setFailed(
            'Stopped: the model returned nothing for this batch. Check the LLM settings and try again.',
          );
          break;
        }
        if ((result.fetched ?? 0) === 0 && (result.summarised ?? 0) === 0) {
          setFailed(
            `Stopped on "${result.thread_title}": none of that batch could be fetched.`,
          );
          break;
        }
        setStatus(`${result.thread_title} — ${result.remaining ?? 0} left on that thread`);
        // Refreshed each round so the numbers above move while it runs.
        await queryClient.invalidateQueries({ queryKey: ['email-backfill-stats'] });
      }
    } catch (err) {
      setFailed(err instanceof Error ? err.message : 'The backfill failed.');
    } finally {
      if (!cancelled.current) {
        setRunning(false);
        void queryClient.invalidateQueries({ queryKey: ['email-backfill-stats'] });
      }
    }
  }

  const complete = outstanding === 0;

  return (
    <div className="border-t border-border pt-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className="flex items-center gap-1.5 text-sm font-semibold">
            <Icon className="size-4 text-fg-subtle" aria-hidden />
            {title}
          </h3>
          <p className="mt-0.5 text-xs text-fg-subtle">{description}</p>
        </div>
        {complete ? (
          <Badge variant="success" className="shrink-0 gap-1">
            <Check className="size-3" aria-hidden />
            Up to date
          </Badge>
        ) : (
          <Button
            variant="secondary"
            size="sm"
            className="shrink-0"
            loading={running}
            onClick={() => void start()}
          >
            {running ? 'Running…' : `${actionLabel} (${outstanding.toLocaleString()})`}
          </Button>
        )}
      </div>

      <div className="mt-3 flex items-center gap-3">
        <Meter
          value={pct(done, total)}
          label={`${done.toLocaleString()} of ${total.toLocaleString()} done`}
        />
        {/* The bar is a single hue, so the number carries the value too. */}
        <span className="tabular shrink-0 text-xs text-fg-muted">
          {done.toLocaleString()} / {total.toLocaleString()}
        </span>
      </div>

      {costWarning && !complete ? (
        <p className="mt-2 text-2xs text-fg-subtle">{costWarning}</p>
      ) : null}
      {status ? <p className="mt-2 text-xs text-fg-muted">{status}</p> : null}
      {failed ? (
        <p role="alert" className="mt-2 flex items-start gap-1 text-xs text-danger-ink">
          <AlertCircle className="mt-0.5 size-3 shrink-0" aria-hidden />
          {failed}
        </p>
      ) : null}
    </div>
  );
}

export function EmailBackfillPanel() {
  const stats = useQuery({
    queryKey: ['email-backfill-stats'],
    queryFn: () => api.get<EmailBackfillStats>('/email-backfill/stats'),
  });

  if (stats.isLoading) return <Skeleton className="h-96 w-full" />;
  if (stats.isError) return <ErrorState error={stats.error} onRetry={() => void stats.refetch()} />;

  const s = stats.data!;

  if (s.total === 0) {
    return (
      <Card className="p-5">
        <h2 className="font-display text-lg font-semibold">Email backfill</h2>
        <p className="mt-1 text-sm text-fg-subtle">
          No emails are attached to any of your threads yet. Once a match attaches
          some, their full text is fetched as you open each thread — and this page
          will show how far along that is.
        </p>
      </Card>
    );
  }

  // "Asked, and this account cannot supply one" is a third state, not pending.
  // Counting it as outstanding would leave the bar permanently short of 100%.
  const bodiesSettled = s.bodies + s.unavailable;

  // Summaries are measured against the messages that *have* text, not against
  // every attached email: a message with no body yet cannot be summarised, so
  // counting it here would show "Up to date" next to a two-thirds-full bar the
  // moment the outstanding count hit zero. The denominator grows as bodies
  // arrive, which is the honest reading.
  const summariesSettled = s.summaries + s.summary_not_needed;
  const summarisable = summariesSettled + s.summary_pending;

  return (
    <div className="space-y-4">
      <Card className="p-5">
        <h2 className="font-display text-lg font-semibold">Email backfill</h2>
        <p className="mt-1 text-sm text-fg-subtle">
          Attaching an email stores only a short preview — no provider returns the
          full text at search time. The rest is fetched the first time you open
          each thread, so threads you have not revisited stay behind. This is where
          you can see that, and catch them up.
        </p>

        <dl className="mt-5 grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat label="Attached emails" value={s.total} />
          <Stat
            label="Full text stored"
            value={s.bodies}
            hint={s.unavailable > 0 ? `${s.unavailable} unavailable` : undefined}
          />
          <Stat
            label="Messages summarised"
            value={s.summaries}
            hint={
              s.summary_not_needed > 0 ? `${s.summary_not_needed} too short to need one` : undefined
            }
          />
          <Stat
            label="Threads waiting"
            value={s.threads_pending}
            tone={s.threads_pending === 0 ? 'muted' : undefined}
          />
        </dl>

        <RunSection
          title="Message text"
          description="Fetched from each connected account. No AI, and nothing is charged for it."
          endpoint="/email-backfill/bodies"
          done={bodiesSettled}
          outstanding={s.body_pending}
          total={s.total}
          actionLabel="Fetch remaining"
          icon={Download}
        />

        <RunSection
          title="AI summaries"
          description="One sentence per message, for the thread view and for what the assistant reads."
          endpoint="/email-backfill/summaries"
          done={summariesSettled}
          outstanding={s.summary_pending}
          total={summarisable}
          actionLabel="Summarise remaining"
          icon={Sparkles}
          costWarning={`One model call per message — ${s.summary_pending.toLocaleString()} calls if you run it now. Messages shorter than a screen are skipped.`}
        />
      </Card>

      <Card className="p-5">
        <h2 className="font-display text-lg font-semibold">What the app knows</h2>
        <p className="mt-1 text-sm text-fg-subtle">
          How much of the conversation grouping rests on facts from your mail
          provider, rather than on matching subjects and participants.
        </p>

        <dl className="mt-5 grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Stat
            label="Grouped by provider id"
            value={s.with_conversation_id}
            hint="Exact"
          />
          <Stat
            label="Grouped by reply headers"
            value={s.with_rfc_headers}
            hint="Exact"
          />
          <Stat
            label="Grouped by subject"
            value={s.subject_only}
            hint="A guess, hedged"
            tone="muted"
          />
        </dl>

        <div className="mt-5 border-t border-border pt-4">
          <h3 className="text-sm font-semibold">Who sent what</h3>
          <p className="mt-0.5 text-xs text-fg-subtle">
            Messages whose direction is unknown are never guessed at — the
            assistant is told it cannot tell, rather than assuming you were the
            one waiting.
          </p>
          <dl className="mt-3 grid grid-cols-3 gap-4">
            <Stat label="You sent" value={s.outbound} />
            <Stat label="Received" value={s.inbound} />
            <Stat label="Unknown" value={s.direction_unknown} tone="muted" />
          </dl>
        </div>
      </Card>
    </div>
  );
}
