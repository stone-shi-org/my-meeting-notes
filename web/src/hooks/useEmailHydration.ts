/**
 * Fire the bulk body fetch once per thread visit.
 *
 * Called from the **page**, not the card. N cards each deciding to hydrate would
 * mean N bulk requests against something the server deliberately bounds to one
 * screenful, and they would race.
 *
 * The ref is set *before* mutate rather than in `onSuccess`, so a failed attempt
 * does not retry on every render -- the same guard the page already uses for its
 * next-step auto-refresh.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';
import { api } from '@/lib/api';
import type { EmailChain, EmailHydrateResult, TimelineItem } from '@/types/api';

/** Whether anything on this timeline has never been asked for a body. */
export function needsHydration(items: TimelineItem[] | undefined): boolean {
  if (!items) return false;
  return items.some(
    (item) =>
      item.kind === 'email_chain' &&
      (item.payload as EmailChain).messages.some(
        (m) => !m.has_body && !m.body_fetched_at,
      ),
  );
}

export function useEmailHydration(
  threadId: string | undefined,
  timeline: TimelineItem[] | undefined,
) {
  const queryClient = useQueryClient();
  const hydratedFor = useRef<string | null>(null);

  const hydrate = useMutation({
    mutationFn: () => api.post<EmailHydrateResult>(`/threads/${threadId}/emails/hydrate`),
    onSuccess: () => {
      // Invalidate rather than setQueryData. Patching emails nested inside
      // `payload.messages` is exactly the cache surgery that goes wrong, and the
      // rest of this page invalidates everywhere for the same reason. The cost
      // is one extra timeline GET, which the page already does routinely.
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
    },
  });

  useEffect(() => {
    if (!threadId || !timeline) return;
    if (hydratedFor.current === threadId) return;
    if (!needsHydration(timeline)) return;
    hydratedFor.current = threadId;
    hydrate.mutate();
    // `hydrate` is a stable mutation object; including it would re-run this on
    // every status change, which is the thing the ref exists to prevent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, timeline]);

  return { pending: hydrate.isPending };
}
