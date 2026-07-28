import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CalendarDays, Layers, Mail, Mic, Plus, Search, X } from 'lucide-react';
import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { UpcomingPanel } from '@/components/calendar/UpcomingPanel';
import { Button } from '@/components/ui/Button';
import {
  Badge,
  Card,
  Input,
  Label,
  Select,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives';
import { EmptyState, ErrorState, Pagination } from '@/components/ui/states';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import { fmtRelative } from '@/lib/time';
import type { Paginated, Thread } from '@/types/api';

function StatPill({
  icon: Icon,
  count,
  label,
}: {
  icon: typeof Mic;
  count: number;
  label: string;
}) {
  return (
    <span
      className="inline-flex items-center gap-1 text-xs text-fg-subtle"
      aria-label={`${count} ${label}`}
    >
      <Icon className="size-3.5 text-fg-faint" aria-hidden />
      <span className="tabular">{count}</span>
    </span>
  );
}

function ThreadCard({ thread }: { thread: Thread }) {
  const unread = thread.unread_count > 0;
  return (
    <Card interactive className="relative overflow-hidden">
      {/* 3px identity rail: the app's own objects are indigo. */}
      <span className="absolute inset-y-0 left-0 w-[3px] bg-entity-meeting" aria-hidden />
      <Link to={`/threads/${thread.id}`} className="block p-5 pl-6">
        <div className="flex items-start justify-between gap-3">
          <h3 className="font-display text-lg font-semibold leading-snug">
            {unread && (
              // Inline rather than absolutely positioned so it pushes the title
              // instead of sitting over it at long titles.
              <span
                className="mr-2 inline-block size-2 shrink-0 rounded-full glow-dot align-middle"
                aria-hidden
              />
            )}
            {thread.title}
          </h3>
          {thread.archived && <Badge variant="neutral">Archived</Badge>}
        </div>

        {/* The dot is decoration; this is the part a screen reader gets. */}
        {unread && (
          <p className="mt-1 text-xs font-medium text-info-ink">
            {thread.unread_count} new item{thread.unread_count === 1 ? '' : 's'} found for you
          </p>
        )}

        {thread.description && (
          <p className="mt-1.5 line-clamp-2 text-sm text-fg-subtle">{thread.description}</p>
        )}

        <div className="mt-4 flex items-center gap-4">
          <StatPill icon={Mic} count={thread.meeting_count} label="meetings" />
          <StatPill icon={CalendarDays} count={thread.event_count} label="calendar events" />
          <StatPill icon={Mail} count={thread.email_count} label="emails" />
        </div>

        <div className="mt-4 flex items-center justify-between border-t border-border pt-3">
          <span className="text-xs text-fg-subtle">
            Updated <time dateTime={thread.updated_at}>{fmtRelative(thread.updated_at)}</time>
          </span>
        </div>
      </Link>
    </Card>
  );
}

function ThreadCardSkeleton() {
  return (
    <Card className="p-5">
      <Skeleton className="h-5 w-2/3" />
      <Skeleton className="mt-2 h-4 w-full" />
      <Skeleton className="mt-1 h-4 w-4/5" />
      <div className="mt-4 flex gap-4">
        <Skeleton className="h-4 w-10" />
        <Skeleton className="h-4 w-10" />
        <Skeleton className="h-4 w-10" />
      </div>
      <Skeleton className="mt-5 h-4 w-24" />
    </Card>
  );
}

function NewThreadDialog({ onDone }: { onDone: () => void }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');

  const create = useMutation({
    mutationFn: () => api.post<Thread>('/threads', { title, description: description || null }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['threads'] });
      setOpen(false);
      setTitle('');
      setDescription('');
      onDone();
    },
  });

  if (!open) {
    return (
      <Button variant="secondary" onClick={() => setOpen(true)}>
        <Plus />
        New thread
      </Button>
    );
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-overlay p-4 backdrop-blur-sm">
      <Card className="w-full max-w-md p-6">
        <h2 className="font-display text-xl font-semibold">New thread</h2>
        <p className="mt-1 text-sm text-fg-subtle">
          A thread groups the meetings, emails and invites for one ongoing piece of work.
        </p>

        <form
          className="mt-5 space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <div>
            <Label htmlFor="t-title">Title</Label>
            <Input
              id="t-title"
              className="mt-1.5"
              required
              autoFocus
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Atlas Migration"
            />
          </div>
          <div>
            <Label htmlFor="t-desc">Description</Label>
            <Textarea
              id="t-desc"
              className="mt-1.5"
              rows={3}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Move billing off the legacy Oracle stack before Q4"
            />
          </div>

          {create.error && (
            <p role="alert" className="text-sm text-danger-ink">
              {(create.error as Error).message}
            </p>
          )}

          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={create.isPending}>
              Create
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}

export function ThreadsPage() {
  const [params, setParams] = useSearchParams();

  const page = Number(params.get('page') || 1);
  const pageSize = Number(params.get('size') || 12);
  const q = params.get('q') || '';
  const sort = params.get('sort') || 'updated_at';
  const archived = params.get('archived') === '1';

  const [searchDraft, setSearchDraft] = useState(q);

  function update(next: Record<string, string | null>) {
    const merged = new URLSearchParams(params);
    for (const [key, value] of Object.entries(next)) {
      if (value === null || value === '') merged.delete(key);
      else merged.set(key, value);
    }
    // Any filter change invalidates the current page number.
    if (!('page' in next)) merged.delete('page');
    setParams(merged, { replace: true });
  }

  const query = useQuery({
    queryKey: ['threads', { page, pageSize, q, sort, archived }],
    queryFn: () =>
      api.get<Paginated<Thread>>('/threads', {
        page,
        page_size: pageSize,
        q: q || undefined,
        sort,
        archived: archived ? 'true' : 'false',
      }),
    // No layout jump when paging.
    placeholderData: keepPreviousData,
    // The sweep attaches things while this page is just sitting open, and a dot
    // that only appears on reload is not a notification. One list query a minute
    // is cheap; it is the same query the page already runs.
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-2xl font-semibold">Threads</h1>
          <p className="mt-1 text-sm text-fg-subtle">
            Ongoing work and the meetings that belong to it
          </p>
        </div>

        <div className="flex gap-2">
          <NewThreadDialog onDone={() => query.refetch()} />
          <Button variant="primary" asChild>
            <Link to="/meetings/new">
              <Plus />
              New meeting
            </Link>
          </Button>
        </div>
      </div>

      <Card className="p-3">
        <div className="flex flex-wrap items-center gap-3">
          <form
            className="relative min-w-[220px] flex-1"
            onSubmit={(e) => {
              e.preventDefault();
              update({ q: searchDraft });
            }}
          >
            <Search
              className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-fg-faint"
              aria-hidden
            />
            <Input
              className="pl-9 pr-9"
              placeholder="Search threads…"
              aria-label="Search threads"
              value={searchDraft}
              onChange={(e) => setSearchDraft(e.target.value)}
            />
            {searchDraft && (
              <button
                type="button"
                aria-label="Clear search"
                onClick={() => {
                  setSearchDraft('');
                  update({ q: null });
                }}
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-fg-faint hover:text-fg"
              >
                <X className="size-4" />
              </button>
            )}
          </form>

          <Select
            aria-label="Sort by"
            className="w-auto"
            value={sort}
            onChange={(e) => update({ sort: e.target.value })}
          >
            <option value="updated_at">Last activity</option>
            <option value="created_at">Recently created</option>
            <option value="title">Title</option>
          </Select>

          <label className="flex items-center gap-2 text-sm text-fg-muted">
            <input
              type="checkbox"
              checked={archived}
              onChange={(e) => update({ archived: e.target.checked ? '1' : null })}
              className="size-4 rounded border-border-strong"
            />
            Archived
          </label>
        </div>
      </Card>

      {query.isError && <ErrorState error={query.error} onRetry={() => query.refetch()} />}

      {query.isLoading && (
        <div className="grid gap-5 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <ThreadCardSkeleton key={i} />
          ))}
        </div>
      )}

      {query.data && query.data.items.length === 0 && (
        <Card>
          <EmptyState
            icon={Layers}
            title={q ? 'No threads match that search' : 'No threads yet'}
            description={
              q
                ? 'Try a different word, or clear the search.'
                : 'Upload a recording and we will create the first thread for you.'
            }
            action={
              q ? (
                <Button
                  variant="secondary"
                  onClick={() => {
                    setSearchDraft('');
                    update({ q: null });
                  }}
                >
                  Clear search
                </Button>
              ) : (
                <Button variant="primary" asChild>
                  <Link to="/meetings/new">
                    <Plus />
                    Upload a recording
                  </Link>
                </Button>
              )
            }
          />
        </Card>
      )}

      {query.data && query.data.items.length > 0 && (
        <>
          <div
            className={cn(
              'grid gap-5 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4',
              query.isPlaceholderData && 'opacity-60 transition-opacity',
            )}
          >
            {query.data.items.map((thread) => (
              <ThreadCard key={thread.id} thread={thread} />
            ))}
          </div>

          <Pagination
            page={query.data.page}
            pageSize={query.data.page_size}
            total={query.data.total}
            totalPages={query.data.total_pages}
            onPage={(p) => update({ page: String(p) })}
            onPageSize={(s) => update({ size: String(s), page: null })}
          />
        </>
      )}

      {/* Below the threads: this page is titled "Threads" and its search and
          paging belong to that list, so the calendar sits after it rather than
          between the heading and the thing the heading names. */}
      <UpcomingPanel />
    </div>
  );
}
