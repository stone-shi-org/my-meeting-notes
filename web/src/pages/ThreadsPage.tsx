import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Layers, Plus, Search, X } from 'lucide-react';
import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { UpcomingPanel } from '@/components/calendar/UpcomingPanel';
import { HomeChatPanel } from '@/components/home/HomeChatPanel';
import { SearchResults } from '@/components/search/SearchResults';
import { GroupedThreadList, NewGroupButton } from '@/components/thread/ThreadGroups';
import { Button } from '@/components/ui/Button';
import { Card, Input, Label, Select, Textarea } from '@/components/ui/primitives';
import { EmptyState } from '@/components/ui/states';
import { api } from '@/lib/api';
import type { Thread } from '@/types/api';

function NewThreadDialog() {
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

  const q = params.get('q') || '';
  const sort = params.get('sort') || 'updated_at';
  const archived = params.get('archived') === '1';
  // "Search everything" is a URL param too, so a results page can be shared.
  const search = (params.get('search') || '').trim();

  const [searchDraft, setSearchDraft] = useState(q || search);
  const draft = searchDraft.trim();

  // The filters stay in the URL because they are what a shared link means.
  // Paging does not: each group pages on its own now, and one `?page=` cannot
  // say which of five sections it belongs to.
  function update(next: Record<string, string | null>, opts: { push?: boolean } = {}) {
    const merged = new URLSearchParams(params);
    for (const [key, value] of Object.entries(next)) {
      if (value === null || value === '') merged.delete(key);
      else merged.set(key, value);
    }
    setParams(merged, { replace: !opts.push });
  }

  // Opening results is a navigation (Back returns to the list); refining or
  // filtering is not. Enter still applies the thread filter exactly as it
  // always did, so "Back to threads" lands on the list that query narrows.
  function searchEverything() {
    if (!draft) return;
    update({ q: draft, search: draft }, { push: !search });
  }

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
          <NewGroupButton />
          <NewThreadDialog />
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
              if (draft) searchEverything();
              else update({ q: null, search: null });
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
                  update({ q: null, search: null });
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
            {/* The filter swaps the list rather than widening it, so "Archived"
                alone reads as "include archived", which is not what it does. */}
            Archived only
          </label>
        </div>

        {draft && draft !== search ? (
          <button
            type="button"
            onClick={searchEverything}
            className="mt-2 flex w-full items-center gap-2 rounded px-3 py-2 text-left text-sm text-fg-muted transition-colors duration-fast hover:bg-surface-2 hover:text-fg"
          >
            <Search className="size-4 shrink-0 text-fg-faint" aria-hidden />
            <span className="min-w-0 truncate">
              Search everything for <span className="font-medium text-fg">“{draft}”</span>
            </span>
          </button>
        ) : null}
      </Card>

      {search ? (
        <section aria-label="Search results" className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="font-display text-lg font-semibold">
              Everything matching “{search}”
            </h2>
            <Button variant="ghost" size="sm" onClick={() => update({ search: null })}>
              <ArrowLeft />
              Back to threads
            </Button>
          </div>
          <SearchResults q={search} />
        </section>
      ) : (
        <GroupedThreadList
          filters={{ q, sort, archived }}
          emptyState={
            <EmptyState
              icon={Layers}
              title={
                q
                  ? 'No threads match that search'
                  : archived
                    ? 'Nothing is archived'
                    : 'No threads yet'
              }
              description={
                q
                  ? 'Try a different word, or clear the search.'
                  : archived
                    ? 'Archiving a thread from its own page keeps everything and stops it being checked for follow-ups.'
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
                ) : archived ? (
                  <Button variant="secondary" onClick={() => update({ archived: null })}>
                    Show active threads
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
          }
        />
      )}

      {/* Below the threads: this page is titled "Threads" and its search and
          paging belong to that list, so the calendar sits after it rather than
          between the heading and the thing the heading names. */}
      <UpcomingPanel />

      <HomeChatPanel />
    </div>
  );
}
