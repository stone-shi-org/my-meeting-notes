/**
 * Fetch email bodies for the thread being viewed.
 *
 * Called from the **page**, not the card. N cards each deciding to hydrate would
 * mean N bulk requests against something the server deliberately bounds to one
 * screenful, and they would race.
 *
 * The server returns at most `HYDRATE_MAX_PER_CALL` bodies per call and reports
 * how many are still pending, so this keeps going until the thread is done
 * rather than stopping at the first batch -- a 40-email thread used to need four
 * separate visits to fill in, because the guard was "have I hydrated this
 * thread" rather than "is there anything left".
 *
 * Bodies only: summarising is a separate, explicit action, so nothing here
 * spends LLM budget.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import type { EmailChain, EmailHydrateResult, TimelineItem } from '@/types/api';

/**
 * A backstop on the round-trip loop. Each round fetches up to a screenful, so
 * this is a very large thread's worth -- it exists so a server that kept
 * reporting work left could never spin the client forever.
 */
const MAX_ROUNDS = 20;

/** Every attached message that has never been asked for a body. */
export function unhydratedIds(items: TimelineItem[] | undefined): number[] {
  if (!items) return [];
  const out: number[] = [];
  for (const item of items) {
    if (item.kind !== 'email_chain') continue;
    for (const message of (item.payload as EmailChain).messages) {
      if (!message.has_body && !message.body_fetched_at && typeof message.id === 'number') {
        out.push(message.id);
      }
    }
  }
  return out;
}

export function useEmailHydration(
  threadId: string | undefined,
  timeline: TimelineItem[] | undefined,
) {
  const queryClient = useQueryClient();
  /** Which thread the loop is running for, so switching threads restarts it. */
  const runningFor = useRef<string | null>(null);
  const rounds = useRef(0);
  const [pending, setPending] = useState(false);

  const hydrate = useMutation({
    mutationFn: () => api.post<EmailHydrateResult>(`/threads/${threadId}/emails/hydrate`),
  });

  useEffect(() => {
    if (!threadId || !timeline) return;
    if (runningFor.current === threadId) return;
    if (unhydratedIds(timeline).length === 0) return;

    // Claimed before the first await, so a re-render mid-flight cannot start a
    // second loop -- the same guard shape the page uses for its next-step
    // auto-refresh.
    runningFor.current = threadId;
    rounds.current = 0;
    let cancelled = false;
    setPending(true);

    void (async () => {
      try {
        for (;;) {
          rounds.current += 1;
          const result = await hydrate.mutateAsync();
          if (cancelled) return;
          // Nothing fetched and nothing left means every remaining row is one
          // this account cannot supply -- stop rather than asking again.
          if (result.remaining <= 0 || result.requested === 0) break;
          if (rounds.current >= MAX_ROUNDS) break;
        }
      } catch {
        // A failed round ends the loop. `runningFor` stays claimed, so it does
        // not retry on every render; navigating back re-arms it.
      } finally {
        if (!cancelled) {
          setPending(false);
          // Invalidate once, at the end. Doing it per round would re-render the
          // whole timeline mid-loop, and patching emails nested inside
          // `payload.messages` is the cache surgery that goes wrong.
          void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
    // `hydrate` is a stable mutation object and `timeline` changes on every
    // refetch; the ref is what makes this run once per thread.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, timeline]);

  return { pending };
}
