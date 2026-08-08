/**
 * Groups on the home screen: collapsible folders you drag thread cards into.
 *
 * "Ungrouped" is rendered as a section like any other but is not a row in the
 * database — it is `group_id IS NULL`, which is where every thread starts and
 * where the threads of a deleted group land. It is therefore always present and
 * cannot be renamed or removed.
 *
 * Each section pages independently (its own `page` state, its own query), so a
 * group is never split across a page boundary the way one shared pager would
 * split it.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  CalendarDays,
  ChevronDown,
  FolderPlus,
  Mail,
  Mic,
  NotebookPen,
  Pencil,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { Badge, Card, Input, Skeleton } from '@/components/ui/primitives';
import { ErrorState, Pagination } from '@/components/ui/states';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import { groupRailColor } from '@/lib/groupColors';
import { fmtRelative } from '@/lib/time';
import type { Paginated, Thread, ThreadGroup } from '@/types/api';

/**
 * The drag payload is a custom MIME type, not `text/plain`: the card wraps an
 * `<a>`, so the browser has already put the thread's URL on the drag before our
 * handler runs. Checking for our own type is what tells "a card from this page"
 * apart from a link, a file, or a selection dragged in from somewhere else —
 * and `dataTransfer.types` is the only part readable during dragover.
 */
const THREAD_MIME = 'application/x-mmn-thread-id';

/** The section key for threads in no group. Also the API's `group` value. */
const UNGROUPED = 'none';

const PAGE_SIZE = 6;

const COLLAPSED_KEY = 'mmn.threadGroups.collapsed';

export interface ThreadFilters {
  q: string;
  sort: string;
  archived: boolean;
}

/* -------------------------------------------------------------------------- */
/* Collapse state                                                             */
/* -------------------------------------------------------------------------- */

/**
 * Which sections are collapsed, remembered across reloads.
 *
 * Stored as the collapsed set rather than the expanded one so a brand new group
 * — which is not in the set — starts open. Kept in localStorage rather than the
 * URL because it is a per-person habit, not part of what a shared link means.
 */
function readCollapsed(): Set<string> {
  try {
    const raw = window.localStorage.getItem(COLLAPSED_KEY);
    return new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    return new Set();
  }
}

/**
 * Held once for the whole list, not once per section: the set is stored whole,
 * so two sections each writing their own copy would have the second overwrite
 * the first and lose it.
 */
function useCollapsed() {
  const [collapsed, setCollapsed] = useState<Set<string>>(readCollapsed);

  function toggle(key: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      try {
        window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...next]));
      } catch {
        /* private mode, or a full quota. The panel still opens and closes. */
      }
      return next;
    });
  }

  return { collapsed, toggle };
}

/**
 * Filing a thread, from either route into it — the drop and the picker are the
 * same write, and both invalidate the same two lists: the thread leaves one
 * section and joins another, and every group's thread_count is now off by one.
 */
function useMoveThread() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ threadId, groupId }: { threadId: number; groupId: number | null }) =>
      api.put<Thread>(`/threads/${threadId}/group`, { group_id: groupId }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['threads'] });
      void queryClient.invalidateQueries({ queryKey: ['thread-groups'] });
    },
  });
}

/* -------------------------------------------------------------------------- */
/* Cards                                                                      */
/* -------------------------------------------------------------------------- */

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

/**
 * The keyboard route into a group, and the reason this feature is not
 * mouse-only: HTML5 drag and drop emits nothing a keyboard can trigger, so
 * every card carries the same move as a plain `<select>`.
 */
function GroupPicker({
  thread,
  groups,
  onMove,
}: {
  thread: Thread;
  groups: ThreadGroup[];
  onMove: (groupId: number | null) => void;
}) {
  return (
    <select
      aria-label={`Group for ${thread.title}`}
      value={thread.group_id ?? UNGROUPED}
      onChange={(e) => onMove(e.target.value === UNGROUPED ? null : Number(e.target.value))}
      className="h-7 max-w-[10rem] rounded border border-border bg-surface px-1.5 text-xs
                 text-fg-muted hover:border-border-strong focus:border-border-strong"
    >
      <option value={UNGROUPED}>Ungrouped</option>
      {groups.map((g) => (
        <option key={g.id} value={g.id}>
          {g.name}
        </option>
      ))}
    </select>
  );
}

function ThreadCard({
  thread,
  groups,
  onMove,
}: {
  thread: Thread;
  groups: ThreadGroup[];
  onMove: (groupId: number | null) => void;
}) {
  const unread = thread.unread_count > 0;
  const [dragging, setDragging] = useState(false);

  return (
    <Card
      interactive
      className={cn('relative overflow-hidden', dragging && 'opacity-50')}
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData(THREAD_MIME, String(thread.id));
        e.dataTransfer.effectAllowed = 'move';
        setDragging(true);
      }}
      onDragEnd={() => setDragging(false)}
    >
      {/* 3px identity rail, in the colour of the group the card is filed under.
          Decoration only -- the section heading above it says the same thing in
          words. Ungrouped keeps the app's default indigo. */}
      <span
        className="absolute inset-y-0 left-0 w-[3px]"
        style={{ background: groupRailColor(thread.group_id) }}
        aria-hidden
      />
      <Link to={`/threads/${thread.id}`} className="block p-5 pb-3 pl-6">
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

        {/* Same icon as the thread page's own Next step box -- one visual
            vocabulary for "this is the AI's suggestion," not the thread's own
            words like the description above it. */}
        {thread.next_step && (
          <p className="mt-2 flex items-start gap-1.5 text-sm text-fg-subtle">
            <Sparkles className="mt-0.5 size-3.5 shrink-0 text-primary" aria-hidden />
            <span className="line-clamp-2">{thread.next_step}</span>
          </p>
        )}

        <div className="mt-4 flex items-center gap-4">
          <StatPill icon={Mic} count={thread.meeting_count} label="meetings" />
          <StatPill icon={CalendarDays} count={thread.event_count} label="calendar events" />
          <StatPill icon={Mail} count={thread.email_count} label="emails" />
          <StatPill icon={NotebookPen} count={thread.note_count} label="notes" />
        </div>
      </Link>

      {/* Outside the link: a <select> nested in an <a> is a trap for both the
          mouse and the keyboard. */}
      <div className="flex items-center justify-between gap-2 border-t border-border py-2.5 pl-6 pr-4">
        <span className="text-xs text-fg-subtle">
          Updated <time dateTime={thread.updated_at}>{fmtRelative(thread.updated_at)}</time>
        </span>
        <GroupPicker thread={thread} groups={groups} onMove={onMove} />
      </div>
    </Card>
  );
}

export function ThreadCardSkeleton() {
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

/* -------------------------------------------------------------------------- */
/* One section                                                                */
/* -------------------------------------------------------------------------- */

function GroupHeading({
  group,
  count,
  collapsed,
  onToggle,
}: {
  group: ThreadGroup | null;
  count: number | null;
  collapsed: boolean;
  onToggle: () => void;
}) {
  const queryClient = useQueryClient();
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(group?.name ?? '');

  const rename = useMutation({
    mutationFn: (name: string) =>
      api.patch<ThreadGroup>(`/thread-groups/${group!.id}`, { name }),
    onSuccess: () => {
      setRenaming(false);
      void queryClient.invalidateQueries({ queryKey: ['thread-groups'] });
    },
  });

  const remove = useMutation({
    mutationFn: () => api.del(`/thread-groups/${group!.id}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['thread-groups'] });
      void queryClient.invalidateQueries({ queryKey: ['threads'] });
    },
  });

  function commit() {
    const name = draft.trim();
    if (!name || name === group!.name) {
      setRenaming(false);
      setDraft(group!.name);
      return;
    }
    rename.mutate(name);
  }

  if (renaming && group) {
    return (
      <div className="flex flex-1 items-center gap-2">
        <Input
          className="h-8 max-w-xs"
          autoFocus
          value={draft}
          aria-label={`Rename ${group.name}`}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === 'Enter') e.currentTarget.blur();
            else if (e.key === 'Escape') {
              setDraft(group.name);
              setRenaming(false);
            }
          }}
        />
        {rename.error && (
          <span role="alert" className="text-xs text-danger-ink">
            {(rename.error as Error).message}
          </span>
        )}
      </div>
    );
  }

  return (
    <>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!collapsed}
        className="flex flex-1 items-center gap-2 rounded py-1 text-left hover:text-fg"
      >
        <ChevronDown
          className={cn(
            'size-4 shrink-0 text-fg-faint transition-transform duration-fast',
            collapsed && '-rotate-90',
          )}
          aria-hidden
        />
        {/* The same colour as the rail on this section's cards, so the two read
            as one system rather than as decoration that happens to differ. */}
        <span
          className="size-2 shrink-0 rounded-full"
          style={{ background: groupRailColor(group?.id ?? null) }}
          aria-hidden
        />
        <span className="font-display font-semibold">{group?.name ?? 'Ungrouped'}</span>
        {count !== null && <span className="tabular text-xs text-fg-subtle">{count}</span>}
      </button>

      {group && (
        <>
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={`Rename ${group.name}`}
            onClick={() => {
              setDraft(group.name);
              setRenaming(true);
            }}
          >
            <Pencil className="size-3.5" />
          </Button>
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={`Delete ${group.name}`}
            loading={remove.isPending}
            onClick={() => {
              // Names what actually goes: the folder, not the work in it.
              const detail = group.thread_count
                ? ` Its ${group.thread_count} thread${group.thread_count === 1 ? '' : 's'} will move to Ungrouped.`
                : '';
              if (window.confirm(`Delete the group “${group.name}”?${detail}`)) remove.mutate();
            }}
          >
            <Trash2 className="size-3.5" />
          </Button>
        </>
      )}
    </>
  );
}

function GroupSection({
  group,
  groups,
  filters,
  collapsed,
  onToggle,
  emptyState,
  hideWhenEmpty = false,
}: {
  group: ThreadGroup | null;
  groups: ThreadGroup[];
  filters: ThreadFilters;
  collapsed: boolean;
  onToggle: () => void;
  /** Rendered instead of the section's own hint when this is the only section
   * and it is empty — i.e. the page has nothing on it at all. */
  emptyState?: React.ReactNode;
  /** Drop the whole section from the page when it holds nothing. Used for
   * Ungrouped, which is a heading over nothing once everything is filed. */
  hideWhenEmpty?: boolean;
}) {
  const sectionKey = group ? String(group.id) : UNGROUPED;
  const move = useMoveThread();

  const [page, setPage] = useState(1);
  const [over, setOver] = useState(false);

  // A narrower filter can leave the section on a page that no longer exists.
  useEffect(() => setPage(1), [filters.q, filters.sort, filters.archived]);

  const query = useQuery({
    queryKey: ['threads', { group: sectionKey, page, ...filters }],
    queryFn: () =>
      api.get<Paginated<Thread>>('/threads', {
        group: sectionKey,
        page,
        page_size: PAGE_SIZE,
        q: filters.q || undefined,
        sort: filters.sort,
        archived: filters.archived ? 'true' : 'false',
      }),
    // No layout jump when paging.
    placeholderData: keepPreviousData,
    // The sweep attaches things while this page is just sitting open, and a dot
    // that only appears on reload is not a notification.
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });

  const accepts = (e: React.DragEvent) => e.dataTransfer.types.includes(THREAD_MIME);

  // Before the section's own query lands, the group's own count stands in so the
  // heading does not flash empty. Ungrouped has no such count, hence the null.
  const count = query.data?.total ?? group?.thread_count ?? null;
  const items = query.data?.items ?? [];

  // Only once the query has actually answered: hiding on `undefined` would make
  // every section flicker out of the page on the first render.
  if (hideWhenEmpty && query.data?.total === 0) return null;

  return (
    <section
      // Named, so each group is a landmark a screen reader can jump between --
      // and so "the Clients section" is a thing the drop tests can address.
      aria-label={group?.name ?? 'Ungrouped'}
      className={cn(
        'rounded-lg border border-border bg-surface-2/40 transition-colors',
        over && 'border-primary bg-primary-soft/40',
      )}
      onDragOver={(e) => {
        if (!accepts(e)) return;
        // Without preventDefault the browser refuses the drop entirely.
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        setOver(true);
      }}
      onDragLeave={(e) => {
        // Ignore the leaves fired while crossing this section's own children.
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOver(false);
      }}
      onDrop={(e) => {
        if (!accepts(e)) return;
        e.preventDefault();
        setOver(false);
        const threadId = Number(e.dataTransfer.getData(THREAD_MIME));
        if (!threadId) return;
        // Dropping a card back where it came from is a no-op, not a write.
        if (!items.some((t) => t.id === threadId)) {
          move.mutate({ threadId, groupId: group?.id ?? null });
        }
      }}
    >
      <div className="flex items-center gap-1 px-3 py-2">
        <GroupHeading group={group} count={count} collapsed={collapsed} onToggle={onToggle} />
      </div>

      {!collapsed && (
        <div className="px-3 pb-3">
          {query.isError && <ErrorState error={query.error} onRetry={() => query.refetch()} />}

          {query.isLoading && (
            <div className="grid gap-5 sm:grid-cols-2 xl:grid-cols-3">
              {Array.from({ length: 3 }).map((_, i) => (
                <ThreadCardSkeleton key={i} />
              ))}
            </div>
          )}

          {query.data && items.length === 0 && (
            <>
              {emptyState ?? (
                <p className="px-2 py-6 text-center text-sm text-fg-subtle">
                  {filters.q
                    ? 'No threads here match that search.'
                    : group
                      ? 'Drag a thread card here to file it in this group.'
                      : 'Drop a thread card here to take it out of its group.'}
                </p>
              )}
            </>
          )}

          {items.length > 0 && (
            <>
              <div
                className={cn(
                  'grid gap-5 sm:grid-cols-2 xl:grid-cols-3',
                  query.isPlaceholderData && 'opacity-60 transition-opacity',
                )}
              >
                {items.map((thread) => (
                  <ThreadCard
                    key={thread.id}
                    thread={thread}
                    groups={groups}
                    onMove={(groupId) => move.mutate({ threadId: thread.id, groupId })}
                  />
                ))}
              </div>

              {query.data && query.data.total_pages > 1 && (
                <Pagination
                  page={query.data.page}
                  pageSize={query.data.page_size}
                  total={query.data.total}
                  totalPages={query.data.total_pages}
                  onPage={setPage}
                />
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Public pieces                                                              */
/* -------------------------------------------------------------------------- */

export function NewGroupButton() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');

  const create = useMutation({
    mutationFn: () => api.post<ThreadGroup>('/thread-groups', { name: name.trim() }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['thread-groups'] });
      setOpen(false);
      setName('');
    },
  });

  if (!open) {
    return (
      <Button variant="secondary" onClick={() => setOpen(true)}>
        <FolderPlus />
        New group
      </Button>
    );
  }

  return (
    <form
      className="flex items-start gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (name.trim()) create.mutate();
      }}
    >
      <div>
        <Input
          autoFocus
          className="h-9 w-44"
          value={name}
          aria-label="New group name"
          placeholder="Clients"
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              setOpen(false);
              setName('');
            }
          }}
        />
        {create.error && (
          <p role="alert" className="mt-1 max-w-44 text-xs text-danger-ink">
            {(create.error as Error).message}
          </p>
        )}
      </div>
      <Button type="submit" variant="secondary" loading={create.isPending}>
        Add
      </Button>
    </form>
  );
}

/**
 * The home screen's thread list, laid out as one section per group with
 * Ungrouped last.
 *
 * Ungrouped hides itself once it is empty -- it is a heading over nothing when
 * everything has been filed. It comes back for the duration of a drag, because
 * otherwise dragging the last thread *out* of a group would have nowhere to
 * land, and it stays put when no group exists at all, since that is the whole
 * page and `emptyState` belongs in it.
 */
export function GroupedThreadList({
  filters,
  emptyState,
}: {
  filters: ThreadFilters;
  emptyState: React.ReactNode;
}) {
  const groups = useQuery({
    queryKey: ['thread-groups'],
    queryFn: () => api.get<ThreadGroup[]>('/thread-groups'),
  });
  const { collapsed, toggle } = useCollapsed();
  // A card is mid-drag. dragstart/dragend both bubble, so one pair of handlers
  // on the container sees every card without threading callbacks through.
  const [dragging, setDragging] = useState(false);

  if (groups.isError) {
    return <ErrorState error={groups.error} onRetry={() => groups.refetch()} />;
  }

  const list = groups.data ?? [];

  return (
    <div
      className="space-y-4"
      onDragStart={(e) => {
        if (e.dataTransfer.types.includes(THREAD_MIME)) setDragging(true);
      }}
      onDragEnd={() => setDragging(false)}
      onDrop={() => setDragging(false)}
    >
      {list.map((group) => (
        <GroupSection
          key={group.id}
          group={group}
          groups={list}
          filters={filters}
          collapsed={collapsed.has(String(group.id))}
          onToggle={() => toggle(String(group.id))}
        />
      ))}
      {/* Ungrouped last: it is where threads start, and where the threads of a
          deleted group reappear. */}
      <GroupSection
        group={null}
        groups={list}
        filters={filters}
        collapsed={collapsed.has(UNGROUPED)}
        onToggle={() => toggle(UNGROUPED)}
        emptyState={list.length === 0 ? emptyState : undefined}
        hideWhenEmpty={list.length > 0 && !dragging}
      />
    </div>
  );
}
