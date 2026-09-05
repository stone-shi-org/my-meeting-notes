/**
 * Detach / move / mark-read, shared by every attached item on a thread.
 *
 * Extracted from `ThreadDetailPage` when emails became conversations:
 * `EmailChainCard` needs all of these, and the page imports the card, so leaving
 * them on the page would be an import cycle.
 *
 * All three are keyed on the attachment's own row id. That is what lets them
 * survive the regrouping unchanged: a chain is a *view* over rows that each still
 * have one, so the per-message actions move down a level and nothing else about
 * them changes.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { X } from 'lucide-react';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';

export type Kind = 'emails' | 'calendar-events';

/** Detach an attached item from its thread.
 *
 * Only the copy on the thread goes; nothing is touched in the actual calendar or
 * mailbox, which is why this needs no confirmation dialog -- re-running the match
 * offers it straight back. */
export function useDetach(threadId: string, kind: Kind) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.del(`/threads/${threadId}/${kind}/${id}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['thread', threadId] });
    },
  });
}

/** Move an attached item onto another thread.
 *
 * Invalidates both ends: the source loses the item from its timeline and
 * counts, the destination gains it -- and `threads` too, since both cards'
 * counts on the home screen are affected. */
export function useMoveItem(threadId: string, kind: Kind) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, targetThreadId }: { id: number; targetThreadId: number }) =>
      api.post(`/threads/${threadId}/${kind}/${id}/move`, { target_thread_id: targetThreadId }),
    onSuccess: (_data, { targetThreadId }) => {
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['thread', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', String(targetThreadId)] });
      void queryClient.invalidateQueries({ queryKey: ['thread', String(targetThreadId)] });
      void queryClient.invalidateQueries({ queryKey: ['threads'] });
    },
  });
}

/** Clear the "arrived while you were away" mark on one item.
 *
 * Fired on the link the user actually clicks, which is the moment the mark stops
 * being true. Harmless to repeat: the backend only stamps a row that is still
 * unread, so a second click is a 200 with nothing changed -- which is also what
 * makes a chain-level "Mark N read" safe as N separate calls. */
export function useMarkRead(threadId: string, kind: Kind) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.post(`/threads/${threadId}/${kind}/${id}/read`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['thread', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['threads'] });
    },
  });
}

/** Dismiss the "Needs your reply" badge on an email conversation. */
export function useDismissReply(threadId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.post(`/threads/${threadId}/emails/${id}/dismiss-reply`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['thread', threadId] });
    },
  });
}

/** The unread mark on one row. Paired with bold text and a "New" label, never
 *  the only thing saying so. */
export function UnreadDot() {
  return (
    <span className="mt-1.5 size-2 shrink-0 rounded-full glow-dot" aria-hidden />
  );
}

export function MarkReadButton({
  onClick,
  pending,
  label = 'Mark read',
}: {
  onClick: () => void;
  pending: boolean;
  label?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={pending}
      className="shrink-0 rounded px-1.5 py-0.5 text-2xs font-medium text-info-ink hover:bg-info-soft disabled:opacity-50"
    >
      {label}
    </button>
  );
}

export function DetachButton({
  onClick,
  pending,
  label,
  className,
}: {
  onClick: () => void;
  pending: boolean;
  label: string;
  /**
   * Replaces the default hover pair rather than adding to it. A message row
   * inside a chain card scopes its reveal to `group/msg`, and appending a second
   * `group-hover:` variant would not dedupe -- both would apply, so hovering the
   * card would reveal the Detach button on all twelve rows at once.
   */
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={pending}
      aria-label={label}
      title={label}
      // Revealed on hover, but always reachable by keyboard -- hover-only
      // controls are invisible to anyone tabbing through.
      className={cn(
        'shrink-0 rounded p-1 text-fg-faint transition-opacity',
        className ?? 'opacity-0 group-hover:opacity-100 focus-visible:opacity-100',
        'hover:text-danger-ink disabled:opacity-50',
      )}
    >
      <X className="size-3.5" aria-hidden />
    </button>
  );
}
