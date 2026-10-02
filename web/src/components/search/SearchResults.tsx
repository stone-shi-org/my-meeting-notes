/**
 * "Search everything": one query across every kind of thing a user owns,
 * grouped by kind.
 *
 * One request at `limit=100`, grouped client-side. The server ranks across
 * kinds (RRF over keyword and semantic), but a reader scanning results wants
 * "what was said" separate from "what was emailed", so the groups come in a
 * fixed order and keep the server's ranking *within* each one. Five per group
 * with a per-group "Show more" keeps one chatty kind -- a long transcript
 * matches a common word dozens of times -- from pushing everything else off
 * the screen.
 */
import { Info, SearchX } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { Card, Skeleton } from '@/components/ui/primitives';
import { EmptyState, ErrorState } from '@/components/ui/states';
import { useSearch } from '@/hooks/useSearch';
import { SEARCH_KIND_LABELS, SEARCH_KIND_ORDER } from '@/lib/searchKinds';
import { renderSnippet, stripSentinels } from '@/lib/searchSnippet';
import { fmtElapsed } from '@/lib/time';
import type { SearchHit, SearchKind, SearchMode } from '@/types/api';

/** Hits shown per group before "Show more". */
export const GROUP_PREVIEW = 5;

/** One request, grouped client-side; the server's cap. */
const FETCH_LIMIT = 100;

function fmtDate(iso: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function HitRow({ hit }: { hit: SearchHit }) {
  const title = stripSentinels(hit.title) || SEARCH_KIND_LABELS[hit.kind];
  const date = fmtDate(hit.date);
  const meta: string[] = [];
  if (hit.thread_title && hit.kind !== 'thread') meta.push(hit.thread_title);
  if (hit.meeting_title && hit.kind !== 'meeting') meta.push(hit.meeting_title);
  if (date) meta.push(date);
  if (hit.kind === 'segment' && hit.start_sec != null) meta.push(fmtElapsed(hit.start_sec));

  return (
    <li className="py-3 first:pt-0 last:pb-0">
      <Link
        to={hit.url}
        className="font-medium text-fg underline-offset-4 hover:text-primary hover:underline"
      >
        {title}
      </Link>
      {hit.snippet ? (
        <p className="mt-1 line-clamp-3 break-words text-sm text-fg-muted">
          {renderSnippet(hit.snippet)}
        </p>
      ) : null}
      {meta.length > 0 ? (
        <p className="mt-1 text-xs text-fg-subtle">{meta.join(' · ')}</p>
      ) : null}
    </li>
  );
}

function HitGroup({ kind, hits }: { kind: SearchKind; hits: SearchHit[] }) {
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? hits : hits.slice(0, GROUP_PREVIEW);
  const hidden = hits.length - GROUP_PREVIEW;
  const headingId = `search-group-${kind}`;

  return (
    <Card className="p-4" role="region" aria-labelledby={headingId}>
      <h2 id={headingId} className="flex items-baseline gap-2 font-display text-base font-semibold">
        {SEARCH_KIND_LABELS[kind]}
        <span className="tabular text-xs font-normal text-fg-subtle">{hits.length}</span>
      </h2>
      <ul className="mt-3 divide-y divide-border">
        {shown.map((hit) => (
          <HitRow key={`${hit.kind}:${hit.ref_id}`} hit={hit} />
        ))}
      </ul>
      {hidden > 0 ? (
        <Button
          variant="ghost"
          size="sm"
          className="mt-2"
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? 'Show fewer' : `Show more (${hidden})`}
        </Button>
      ) : null}
    </Card>
  );
}

export function SearchResults({ q, mode }: { q: string; mode?: SearchMode }) {
  const search = useSearch(q, { limit: FETCH_LIMIT, mode });
  const query = q.trim();

  if (!query) return null;
  if (search.isLoading) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Searching">
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-28 w-full" />
      </div>
    );
  }
  if (search.isError) {
    return <ErrorState error={search.error} onRetry={() => void search.refetch()} />;
  }

  const data = search.data!;
  const byKind = new Map<SearchKind, SearchHit[]>();
  for (const hit of data.hits) {
    const list = byKind.get(hit.kind);
    if (list) list.push(hit);
    else byKind.set(hit.kind, [hit]);
  }
  const groups = SEARCH_KIND_ORDER.filter((k) => byKind.has(k));

  // Only worth saying when semantic was wanted: asking for keyword-only and
  // being told semantic is unavailable is noise.
  const keywordOnly = !data.semantic.available && (mode ?? data.mode) !== 'keyword';

  return (
    <div className="space-y-4">
      {keywordOnly ? (
        <p className="flex items-start gap-1.5 text-xs text-fg-subtle">
          <Info className="mt-0.5 size-3.5 shrink-0 text-fg-faint" aria-hidden />
          <span>
            Keyword matches only — semantic search is unavailable
            {data.semantic.reason ? `: ${data.semantic.reason}` : ''}
          </span>
        </p>
      ) : null}

      {groups.length === 0 ? (
        <Card>
          <EmptyState
            icon={SearchX}
            title={`No results for “${query}”`}
            description="Try fewer or different words. A word ending in * matches anything it starts."
          />
        </Card>
      ) : (
        // Keyed by query, so "Show more" does not stay open onto a new search.
        <div key={query} className="space-y-4">
          {groups.map((kind) => (
            <HitGroup key={kind} kind={kind} hits={byKind.get(kind)!} />
          ))}
          {data.has_more ? (
            <p className="text-xs text-fg-subtle">
              Showing the best {data.hits.length} matches. Add a word to narrow the search.
            </p>
          ) : null}
        </div>
      )}
    </div>
  );
}
