import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import type {
  SearchKind,
  SearchMode,
  SearchRebuildResult,
  SearchResponse,
  SearchStatus,
} from '@/types/api';

export interface SearchOptions {
  kinds?: SearchKind[];
  since?: string;
  until?: string;
  threadId?: number;
  mode?: SearchMode;
  limit?: number;
  offset?: number;
}

export const SEARCH_STATUS_KEY = ['search-status'] as const;

/** `GET /api/search`. Disabled for a blank query -- the server would 422 it. */
export function useSearch(q: string, opts: SearchOptions = {}) {
  const query = q.trim();
  const params = {
    q: query,
    kinds: opts.kinds?.length ? opts.kinds.join(',') : undefined,
    since: opts.since,
    until: opts.until,
    thread_id: opts.threadId,
    mode: opts.mode,
    limit: opts.limit,
    offset: opts.offset,
  };
  return useQuery({
    queryKey: ['search', params],
    queryFn: () => api.get<SearchResponse>('/search', params),
    enabled: query.length > 0,
    // Typing a refinement should not blank the page between two result sets.
    placeholderData: keepPreviousData,
  });
}

/** Anything still waiting to be indexed, for this user or (admins) anyone. */
export function searchStatusPending(status: SearchStatus | undefined): boolean {
  if (!status) return false;
  return (
    status.pending_scopes > 0 ||
    status.embedding.pending_scopes > 0 ||
    (status.global?.pending_scopes ?? 0) > 0
  );
}

/**
 * `GET /api/search/status`. Polls every two seconds while there is work
 * outstanding (or while `poll` is forced on, e.g. straight after a rebuild,
 * before the server has reported the queue it just filled), and stops when
 * the queue drains.
 */
export function useSearchStatus(options: { poll?: boolean } = {}) {
  return useQuery({
    queryKey: SEARCH_STATUS_KEY,
    queryFn: () => api.get<SearchStatus>('/search/status'),
    refetchInterval: (query) =>
      options.poll || searchStatusPending(query.state.data) ? 2000 : false,
  });
}

/** `POST /api/search/rebuild`. Admin only; the work itself runs server-side. */
export function useRebuildSearch() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<SearchRebuildResult>('/search/rebuild'),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: SEARCH_STATUS_KEY });
    },
  });
}
