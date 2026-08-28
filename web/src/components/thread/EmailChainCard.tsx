/**
 * One email conversation on the thread timeline.
 *
 * Collapsed it is roughly the old flat row; expanded it is the conversation, read
 * top to bottom. A lone email arrives here too as a chain of one -- one renderer
 * rather than two that could drift apart -- and drops all the chain chrome so the
 * common case does not gain a click.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronRight, ExternalLink } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { EmailBody } from '@/components/thread/EmailBody';
import { MoveToThread } from '@/components/thread/MoveToThread';
import {
  DetachButton,
  MarkReadButton,
  UnreadDot,
  useDetach,
  useMarkRead,
  useMoveItem,
} from '@/components/thread/threadItemActions';
import { Badge } from '@/components/ui/primitives';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import { emailLink } from '@/lib/links';
import { fmtRelative } from '@/lib/time';
import type { Email, EmailBody as EmailBodyPayload, EmailChain } from '@/types/api';

/** Providers whose search cannot see this account's own sent mail. */
const INBOX_ONLY_PROVIDERS = new Set(['apple']);

function shortDate(iso: string | null): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** "priya@acme.com" -> "priya", for a participant chip. */
function shortName(address: string): string {
  const local = address.split('@')[0] ?? address;
  return local.replace(/[._-]+/g, ' ');
}

function participantLabel(participants: string[]): string {
  if (participants.length === 0) return 'No other participants';
  const names = participants.slice(0, 2).map(shortName);
  const rest = participants.length - names.length;
  return rest > 0 ? `${names.join(', ')} +${rest}` : names.join(', ');
}

/**
 * A conversation whose every message is inbound, on an account whose search
 * cannot see sent mail. Said out loud rather than left to look complete: an
 * incomplete chain presented as whole is what makes a suggestion engine tell you
 * to send a message you already sent.
 */
function missingOutbound(chain: EmailChain): boolean {
  if (chain.messages.length === 0) return false;
  if (!chain.messages.every((m) => m.direction === 'inbound')) return false;
  return chain.messages.some((m) => INBOX_ONLY_PROVIDERS.has(m.provider ?? ''));
}

/**
 * Who sent one message.
 *
 * Weight and size are uniform across all three branches, and set by the header
 * row rather than here: the *content* is what distinguishes the directions
 * ("You" vs the sender), so varying the weight as well made an unknown-direction
 * header indistinguishable from the body text below it.
 */
function DirectionLabel({ email }: { email: Email }) {
  if (email.direction === 'outbound') return <span>You</span>;
  if (email.direction === 'inbound') {
    return <span>{email.sender ?? 'Unknown sender'}</span>;
  }
  // Null is genuinely unknown. No label and no direction colour -- the raw
  // sender and nothing else. The coloured gutter is decoration; the text is what
  // carries the claim, so a colour with no accessible equivalent would be
  // asserting something the UI cannot back up.
  return (
    <>
      <span>{email.sender ?? 'Unknown sender'}</span>
      <span className="sr-only"> (sender side unknown)</span>
    </>
  );
}

function gutterClass(direction: Email['direction']): string {
  if (direction === 'outbound') return 'border-primary';
  if (direction === 'inbound') return 'border-entity-email';
  // The neutral border, not a third direction colour and not transparent. It
  // gives the row the same structure as its siblings without claiming a side --
  // transparent left an unknown-direction message with no anchor at all.
  return 'border-border';
}

function EmailMessageRow({
  email,
  threadId,
  defaultOpen,
}: {
  email: Email;
  threadId: string;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const bodyId = useId();
  const queryClient = useQueryClient();
  const detach = useDetach(threadId, 'emails');
  const move = useMoveItem(threadId, 'emails');
  const markRead = useMarkRead(threadId, 'emails');

  const rowId = typeof email.id === 'number' ? email.id : null;
  const link = emailLink(email);
  const unread = Boolean(email.unread);

  const body = useQuery({
    queryKey: ['email-body', threadId, rowId],
    queryFn: () => api.get<EmailBodyPayload>(`/threads/${threadId}/emails/${rowId}/body`),
    // A delivered email body never changes, so one fetch per opened message ever
    // -- the query cache is what makes this no-request-per-render.
    staleTime: Infinity,
    enabled: open && rowId !== null && email.has_body === true,
  });

  const loadBody = useMutation({
    mutationFn: () =>
      api.post(`/threads/${threadId}/emails/${rowId}/hydrate`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
      void queryClient.invalidateQueries({ queryKey: ['email-body', threadId, rowId] });
    },
  });

  // Marked read when the *body becomes visible*, which is the moment the mark
  // stops being true. An effect rather than the click handler because the newest
  // message opens with the chain and is just as read as one opened by hand --
  // leaving its dot lit while the reader is looking at it is the worse failure.
  // The other messages stay collapsed and unmarked, so expanding a chain still
  // does not blanket-clear it.
  const marked = useRef(false);
  useEffect(() => {
    if (!open || !unread || rowId === null || marked.current) return;
    marked.current = true;
    markRead.mutate(rowId);
    // `markRead` is a stable mutation object; the ref is what makes this once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, unread, rowId]);

  return (
    <li
      className={cn(
        // py-1 as well as pl-3: each message needs to read as its own block, not
        // as one more line in a run of text.
        'group/msg border-l-2 py-1 pl-3',
        gutterClass(email.direction),
      )}
    >
      <div className="flex items-start gap-2">
        {unread ? <UnreadDot /> : null}
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-controls={bodyId}
          className="flex min-w-0 flex-1 items-baseline gap-2 text-left"
        >
          {/* A compact bold label, deliberately one size *below* the body text
              underneath it. Both used to be `text-sm text-fg`, which made a
              message's header and its own first line look identical.
              Deliberately not uppercased: this is usually an address, and
              "PRIYA@ACME.COM" is harder to read than it is label-like. */}
          <span
            className={cn(
              'truncate text-xs text-fg',
              unread ? 'font-bold' : 'font-semibold',
            )}
          >
            <DirectionLabel email={email} />
          </span>
          {/* Each row carries its own absolute date: a conversation spans weeks
              and sits under its newest message's day header. Mono, matching the
              timeline's own times. */}
          <time
            dateTime={email.date ?? undefined}
            className="shrink-0 font-mono text-2xs font-normal text-fg-subtle"
          >
            {shortDate(email.date)}
          </time>
        </button>

        {link ? (
          <a
            href={link}
            target="_blank"
            rel="noreferrer"
            aria-label="Open in mail"
            title="Open in mail"
            className="shrink-0 rounded p-1 text-fg-faint opacity-0 transition-opacity hover:text-primary focus-visible:opacity-100 group-hover/msg:opacity-100"
          >
            <ExternalLink className="size-3.5" aria-hidden />
          </a>
        ) : null}

        {rowId !== null ? (
          <>
            <MoveToThread
              currentThreadId={threadId}
              onMove={(targetThreadId) => move.mutate({ id: rowId, targetThreadId })}
              pending={move.isPending}
              label="Move this email to another thread"
              className="shrink-0 opacity-0 transition-opacity focus-visible:opacity-100 group-hover/msg:opacity-100"
            />
            <DetachButton
              onClick={() => detach.mutate(rowId)}
              pending={detach.isPending}
              label="Detach this email"
              className="opacity-0 focus-visible:opacity-100 group-hover/msg:opacity-100"
            />
          </>
        ) : null}
      </div>

      {unread ? (
        <p className="text-2xs font-medium text-info-ink">New · added for you</p>
      ) : null}

      {(email.to_recipients || email.cc_recipients) && open ? (
        <p className="mt-0.5 truncate text-2xs text-fg-subtle">
          to {email.to_recipients ?? '—'}
          {email.cc_recipients ? ` · cc ${email.cc_recipients}` : ''}
        </p>
      ) : null}

      <div id={bodyId} hidden={!open}>
        {open ? (
          <EmailBody
            body={body.data?.body}
            loading={body.isLoading}
            error={body.isError}
            hasBody={email.has_body === true}
            fetchedAt={email.body_fetched_at}
            snippet={email.snippet}
            aiSummary={email.ai_summary}
            aiSummaryModel={email.ai_summary_model}
            externalUrl={link}
            onLoad={() => loadBody.mutate()}
            onRetry={() => void body.refetch()}
            loadPending={loadBody.isPending}
          />
        ) : null}
      </div>
    </li>
  );
}

export function EmailChainCard({
  chain,
  threadId,
}: {
  chain: EmailChain;
  threadId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const listId = useId();
  const markRead = useMarkRead(threadId, 'emails');
  const detach = useDetach(threadId, 'emails');
  const move = useMoveItem(threadId, 'emails');

  const single = chain.message_count === 1;
  const newest = chain.messages[chain.messages.length - 1];
  const unread = chain.unread_count > 0;
  const preview = newest?.ai_summary || newest?.snippet || null;
  const soleId = single && typeof newest?.id === 'number' ? newest.id : null;

  function markChainRead() {
    // N mutations invalidating the same keys, which react-query dedupes within a
    // tick; the backend treats a repeat as a no-op 200.
    for (const message of chain.messages) {
      if (message.unread && typeof message.id === 'number') markRead.mutate(message.id);
    }
  }

  return (
    <div
      className={cn(
        'group border-l-2 py-2 pl-3 pr-3',
        unread ? 'border-info bg-info-soft/30' : 'border-entity-email bg-surface-2/50',
      )}
    >
      <div className="flex items-start gap-2">
        {unread ? <UnreadDot /> : null}
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          aria-controls={listId}
          className="flex min-w-0 flex-1 items-start gap-1.5 text-left"
        >
          <ChevronRight
            className={cn(
              'mt-0.5 size-3.5 shrink-0 text-fg-faint transition-transform',
              expanded && 'rotate-90',
            )}
            aria-hidden
          />
          <span className={cn('min-w-0 flex-1 text-sm', unread && 'font-semibold')}>
            {chain.subject || '(no subject)'}
          </span>
        </button>

        {unread ? (
          <MarkReadButton
            onClick={markChainRead}
            pending={markRead.isPending}
            label={single ? 'Mark read' : `Mark ${chain.unread_count} read`}
          />
        ) : null}

        {/* A chain of one has nowhere to hide its own actions, so they come up
            to the header and the row below carries none. */}
        {soleId !== null ? (
          <>
            <MoveToThread
              currentThreadId={threadId}
              onMove={(targetThreadId) => move.mutate({ id: soleId, targetThreadId })}
              pending={move.isPending}
              label="Move this email to another thread"
              className="shrink-0 opacity-0 transition-opacity focus-visible:opacity-100 group-hover:opacity-100"
            />
            <DetachButton
              onClick={() => detach.mutate(soleId)}
              pending={detach.isPending}
              label="Detach this email"
            />
          </>
        ) : null}
      </div>

      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 pl-5 text-xs text-fg-subtle">
        <span className="truncate">{participantLabel(chain.participants)}</span>
        {!single ? <span>· {chain.message_count} messages</span> : null}
        <span>· {fmtRelative(chain.last_message_at)}</span>
        {/* Nothing at all when `awaiting` is null: the UI does not guess a side. */}
        {chain.awaiting === 'you' ? (
          <Badge variant="warning" size="sm">
            Your reply
          </Badge>
        ) : null}
        {chain.awaiting === 'them' ? (
          <Badge variant="neutral" size="sm">
            Waiting on them
          </Badge>
        ) : null}
      </div>

      {missingOutbound(chain) ? (
        <p className="mt-1 pl-5 text-2xs text-fg-subtle">
          Outbound messages from this account may be missing.
        </p>
      ) : null}

      {!expanded && preview ? (
        <p className={cn('mt-1 line-clamp-1 pl-5 text-xs', 'text-fg-muted')}>{preview}</p>
      ) : null}

      <ol id={listId} hidden={!expanded} className="mt-2 space-y-4 pl-5">
        {expanded
          ? chain.messages.map((message, index) => (
              <EmailMessageRow
                key={message.message_id}
                email={message}
                threadId={threadId}
                // The newest message opens with the chain: one click gets you
                // what you actually wanted.
                defaultOpen={index === chain.messages.length - 1}
              />
            ))
          : null}
      </ol>
    </div>
  );
}
