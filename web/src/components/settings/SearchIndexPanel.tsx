/**
 * What the search index holds, and (for admins) a way to rebuild it.
 *
 * Indexing happens off the request path -- writes mark things dirty and a
 * background task catches up -- so "I just renamed that speaker and search
 * still shows the old name" has an answer here: it is N items behind. The
 * same goes for embeddings, which can be off, failing, or simply still
 * filling in; each of those reads differently on this page.
 *
 * Rebuild is fire-and-forget on the server. The panel polls status while
 * anything is pending and stops once it drains, the same "the server's state
 * *is* the progress" posture as Email backfill.
 */
import { AlertCircle, Check, RefreshCw, Sparkles } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { Badge, Card, Meter, Skeleton } from '@/components/ui/primitives';
import { ErrorState } from '@/components/ui/states';
import { useAuth } from '@/hooks/useAuth';
import { searchStatusPending, useRebuildSearch, useSearchStatus } from '@/hooks/useSearch';
import { SEARCH_KIND_LABELS, SEARCH_KIND_ORDER } from '@/lib/searchKinds';
import { fmtRelative } from '@/lib/time';
import type { SearchStatus } from '@/types/api';

function pct(part: number, whole: number): number {
  return whole > 0 ? part / whole : 0;
}

function plural(n: number, one: string, many: string): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

function EmbeddingSection({ embedding }: { embedding: SearchStatus['embedding'] }) {
  if (!embedding.enabled) {
    return (
      <div className="border-t border-border pt-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h3 className="flex items-center gap-1.5 text-sm font-semibold">
              <Sparkles className="size-4 text-fg-subtle" aria-hidden />
              Semantic search
            </h3>
            <p className="mt-0.5 text-xs text-fg-subtle">
              Search is matching keywords only. Semantic search uses the embedding
              model configured under{' '}
              <Link
                to="/settings/matching"
                className="text-primary underline-offset-4 hover:underline"
              >
                Matching → Semantic pre-filter (embeddings)
              </Link>
              ; turn that on to find things by meaning as well as by word.
            </p>
          </div>
          <Badge className="shrink-0">Off</Badge>
        </div>
      </div>
    );
  }

  const complete = embedding.chunks > 0 && embedding.embedded >= embedding.chunks;

  return (
    <div className="border-t border-border pt-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className="flex items-center gap-1.5 text-sm font-semibold">
            <Sparkles className="size-4 text-fg-subtle" aria-hidden />
            Semantic search
          </h3>
          <p className="mt-0.5 text-xs text-fg-subtle">
            {embedding.model ? (
              <>
                Embedded with <span className="font-mono">{embedding.model}</span>.{' '}
              </>
            ) : null}
            Passages are embedded in the background after they are indexed.
            {embedding.pending_scopes > 0
              ? ` ${plural(embedding.pending_scopes, 'item', 'items')} still to embed.`
              : ''}
          </p>
        </div>
        {complete ? (
          <Badge variant="success" className="shrink-0 gap-1">
            <Check className="size-3" aria-hidden />
            Up to date
          </Badge>
        ) : null}
      </div>

      <div className="mt-3 flex items-center gap-3">
        <Meter
          value={pct(embedding.embedded, embedding.chunks)}
          label={`${embedding.embedded.toLocaleString()} of ${embedding.chunks.toLocaleString()} passages embedded`}
        />
        {/* The bar is a single hue, so the number carries the value too. */}
        <span className="tabular shrink-0 text-xs text-fg-muted">
          {embedding.embedded.toLocaleString()} / {embedding.chunks.toLocaleString()}
        </span>
      </div>

      {embedding.last_error ? (
        <p role="alert" className="mt-2 flex items-start gap-1 text-xs text-danger-ink">
          <AlertCircle className="mt-0.5 size-3 shrink-0" aria-hidden />
          Last embedding attempt failed: {embedding.last_error}
        </p>
      ) : null}
      {embedding.scale_warning ? (
        <p className="mt-2 text-xs text-warning-ink">
          Semantic search compares every passage one by one, and slows down past{' '}
          {embedding.scale_limit.toLocaleString()} passages — this account has{' '}
          {embedding.chunks.toLocaleString()}. Keyword matches are unaffected.
        </p>
      ) : null}
    </div>
  );
}

function RebuildSection({
  status,
  onStarted,
}: {
  status: SearchStatus;
  /** Called once the server has accepted the rebuild, to start polling. */
  onStarted: () => void;
}) {
  const rebuild = useRebuildSearch();
  const pending = searchStatusPending(status);

  return (
    <div className="border-t border-border pt-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className="flex items-center gap-1.5 text-sm font-semibold">
            <RefreshCw className="size-4 text-fg-subtle" aria-hidden />
            Rebuild
          </h3>
          <p className="mt-0.5 text-xs text-fg-subtle">
            Re-reads every thread and meeting, for every user, and indexes it from
            scratch. Results may be incomplete until it finishes. Nothing outside the
            index is changed.
          </p>
          {status.global ? (
            <p className="mt-1 text-xs text-fg-muted">
              Across all users: {plural(status.global.docs, 'item', 'items')} indexed from{' '}
              {plural(status.global.scopes, 'thread or meeting', 'threads and meetings')},{' '}
              {plural(status.global.chunks, 'passage', 'passages')} embedded,{' '}
              {status.global.pending_scopes.toLocaleString()} waiting.
            </p>
          ) : null}
        </div>
        <Button
          variant="secondary"
          size="sm"
          className="shrink-0"
          loading={rebuild.isPending}
          disabled={rebuild.isPending}
          onClick={() => rebuild.mutate(undefined, { onSuccess: onStarted })}
        >
          Rebuild index
        </Button>
      </div>
      {rebuild.isSuccess ? (
        <p className="mt-2 text-xs text-fg-muted">
          {pending
            ? `Rebuilding — ${plural(rebuild.data.queued_scopes, 'thread or meeting', 'threads and meetings')} queued.`
            : 'Rebuild finished.'}
        </p>
      ) : null}
      {rebuild.isError ? (
        <p role="alert" className="mt-2 flex items-start gap-1 text-xs text-danger-ink">
          <AlertCircle className="mt-0.5 size-3 shrink-0" aria-hidden />
          {rebuild.error instanceof Error ? rebuild.error.message : 'The rebuild failed.'}
        </p>
      ) : null}
    </div>
  );
}

export function SearchIndexPanel() {
  const { isAdmin } = useAuth();
  // Straight after a rebuild the server may not yet report the queue it is
  // filling; keep polling until a status read shows it drained.
  const [awaitingRebuild, setAwaitingRebuild] = useState(false);
  const status = useSearchStatus({ poll: awaitingRebuild });
  const pending = searchStatusPending(status.data);

  useEffect(() => {
    if (awaitingRebuild && status.data && !pending && !status.isFetching) {
      setAwaitingRebuild(false);
    }
  }, [awaitingRebuild, pending, status.data, status.isFetching]);

  if (status.isLoading) return <Skeleton className="h-96 w-full" />;
  if (status.isError) {
    return <ErrorState error={status.error} onRetry={() => void status.refetch()} />;
  }

  const s = status.data!;
  const counts = new Map(s.kinds.map((k) => [k.kind, k.indexed]));
  const total = s.kinds.reduce((sum, k) => sum + k.indexed, 0);

  return (
    <div className="space-y-4">
      <Card className="p-5">
        <h2 className="font-display text-lg font-semibold">Search</h2>
        <p className="mt-1 text-sm text-fg-subtle">
          Everything “Search everything” on the home page can find. Changes are
          indexed in the background a moment after they are saved, so a fresh edit
          can take a few seconds to show up in results.
        </p>

        <div className="mt-5 grid gap-6 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <table className="w-full text-sm">
            <caption className="sr-only">Indexed items by kind</caption>
            <thead>
              <tr className="text-left text-xs text-fg-subtle">
                <th scope="col" className="pb-2 font-medium">
                  Kind
                </th>
                <th scope="col" className="pb-2 text-right font-medium">
                  Indexed
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {SEARCH_KIND_ORDER.map((kind) => (
                <tr key={kind}>
                  <th scope="row" className="py-1.5 text-left font-normal text-fg-muted">
                    {SEARCH_KIND_LABELS[kind]}
                  </th>
                  <td className="tabular py-1.5 text-right text-fg">
                    {(counts.get(kind) ?? 0).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t border-border-strong">
                <th scope="row" className="pt-2 text-left font-medium text-fg">
                  Total
                </th>
                <td className="tabular pt-2 text-right font-semibold text-fg">
                  {total.toLocaleString()}
                </td>
              </tr>
            </tfoot>
          </table>

          <dl className="space-y-4">
            <div>
              <dt className="text-xs font-medium text-fg-muted">Waiting to be indexed</dt>
              <dd className="mt-0.5 flex items-center gap-2 text-sm text-fg">
                {s.pending_scopes === 0 ? (
                  <Badge variant="success" className="gap-1">
                    <Check className="size-3" aria-hidden />
                    Up to date
                  </Badge>
                ) : (
                  plural(s.pending_scopes, 'thread or meeting', 'threads and meetings')
                )}
              </dd>
            </div>
            <div>
              <dt className="text-xs font-medium text-fg-muted">Last indexed</dt>
              <dd className="mt-0.5 text-sm text-fg">
                {s.last_indexed_at ? (
                  <time dateTime={s.last_indexed_at} title={s.last_indexed_at}>
                    {fmtRelative(s.last_indexed_at)}
                  </time>
                ) : (
                  'Never'
                )}
              </dd>
            </div>
          </dl>
        </div>

        <div className="mt-5 space-y-4">
          <EmbeddingSection embedding={s.embedding} />
          {isAdmin ? (
            <RebuildSection status={s} onStarted={() => setAwaitingRebuild(true)} />
          ) : null}
        </div>
      </Card>
    </div>
  );
}

