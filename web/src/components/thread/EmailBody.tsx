/**
 * One email's body, with the quoted history folded away.
 *
 * Rendered as **plain text**, deliberately:
 *
 * - It is not markdown. Routing it through `renderMarkdown` would mangle any
 *   message containing `*`, `#` or an `_underscored_file_name_` -- and unlike a
 *   note, this text was never authored as markdown.
 * - No `dangerouslySetInnerHTML` anywhere on this path. HTML was converted to
 *   text server-side precisely so this component needs no sanitizer, and adding
 *   one here would re-open the surface that conversion closed.
 */
import { AlertCircle, ExternalLink, Loader2, Sparkles } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Badge, Skeleton } from '@/components/ui/primitives';
import { cn } from '@/lib/cn';
import { quotedLineCount, splitQuoted } from '@/lib/emailBody';

export function AiSummary({
  summary,
  model,
  className,
}: {
  summary: string;
  model?: string | null;
  className?: string;
}) {
  return (
    <div className={cn('flex items-start gap-1.5', className)}>
      {/* Attributed, always. AI-written text has to say whose words it is --
          the same rule the notes feature follows for a saved chat reply. */}
      <Badge variant="primary" size="sm" className="shrink-0 gap-1" title={model ?? undefined}>
        <Sparkles className="size-3" aria-hidden />
        Summary
      </Badge>
      <p className="text-xs text-fg-muted">{summary}</p>
    </div>
  );
}

/** The body text itself, with a "show quoted text" disclosure. */
function BodyText({ body }: { body: string }) {
  const parts = useMemo(() => splitQuoted(body), [body]);
  const [showQuoted, setShowQuoted] = useState(false);
  const folded = quotedLineCount(parts);

  return (
    <div className="mt-1.5 space-y-2">
      {/* `leading-relaxed` is doing real work here: it is what makes this read
          as prose against the compact uppercase label above it. */}
      <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-fg">
        {parts.reply}
      </p>

      {parts.signature ? (
        <p className="whitespace-pre-wrap break-words text-xs text-fg-subtle">
          {parts.signature}
        </p>
      ) : null}

      {parts.quoted ? (
        <div>
          <button
            type="button"
            onClick={() => setShowQuoted((v) => !v)}
            aria-expanded={showQuoted}
            className="text-xs text-primary hover:underline"
          >
            {/* Says how much, not just "...": a screen-reader user cannot see
                what is behind an ellipsis. */}
            {showQuoted ? 'Hide quoted text' : `Show ${folded} quoted line${folded === 1 ? '' : 's'}`}
          </button>
          {showQuoted ? (
            <p className="mt-1 whitespace-pre-wrap break-words border-l-2 border-border pl-2 text-xs text-fg-subtle">
              {parts.quoted}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function EmailBody({
  body,
  loading,
  error,
  hasBody,
  fetchedAt,
  snippet,
  aiSummary,
  aiSummaryModel,
  externalUrl,
  onLoad,
  onRetry,
  loadPending,
}: {
  body: string | null | undefined;
  loading: boolean;
  error: boolean;
  hasBody: boolean;
  /** When a fetch was last attempted. With `hasBody` false, this is what makes
   *  the state terminal rather than "not tried yet". */
  fetchedAt: string | null | undefined;
  snippet: string | null;
  aiSummary?: string | null;
  aiSummaryModel?: string | null;
  externalUrl: string | null;
  onLoad: () => void;
  onRetry: () => void;
  loadPending: boolean;
}) {
  const summary = aiSummary ? (
    <AiSummary summary={aiSummary} model={aiSummaryModel} className="mt-1" />
  ) : null;

  // The states, in the order they have to be checked.

  if (hasBody && loading) {
    // Height is known, so a skeleton rather than a spinner.
    return (
      <div className="mt-2 space-y-1.5">
        {summary}
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-2/3" />
      </div>
    );
  }

  if (hasBody && error) {
    return (
      <div className="mt-2">
        {summary}
        <p className="text-xs text-fg-subtle">
          Could not load the message.{' '}
          <button type="button" onClick={onRetry} className="text-primary hover:underline">
            Try again
          </button>
        </p>
      </div>
    );
  }

  if (hasBody && body) {
    return (
      <div>
        {summary}
        <BodyText body={body} />
      </div>
    );
  }

  // Asked, and this account cannot supply one. Terminal: **no retry button**,
  // because a retry that cannot succeed is a lie. The snippet stays visible so
  // the row is never blank, and "Open in mail" is the actual recourse.
  if (fetchedAt) {
    return (
      <div className="mt-1 space-y-1">
        {snippet ? <p className="text-sm text-fg-muted">{snippet}</p> : null}
        {summary}
        <p className="flex items-center gap-1 text-xs text-fg-subtle">
          <AlertCircle className="size-3 shrink-0" aria-hidden />
          This account can&apos;t supply the full message.
          {externalUrl ? (
            <a
              href={externalUrl}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-0.5 text-primary hover:underline"
            >
              Open in mail
              <ExternalLink className="size-3" aria-hidden />
            </a>
          ) : null}
        </p>
      </div>
    );
  }

  // Not fetched yet.
  return (
    <div className="mt-1 space-y-1">
      {snippet ? <p className="text-sm text-fg-muted">{snippet}</p> : null}
      {summary}
      <button
        type="button"
        onClick={onLoad}
        disabled={loadPending}
        className="inline-flex items-center gap-1 text-xs text-primary hover:underline disabled:opacity-50"
      >
        {loadPending ? <Loader2 className="size-3 animate-spin" aria-hidden /> : null}
        {loadPending ? 'Fetching the full message…' : 'Load full message'}
      </button>
    </div>
  );
}
