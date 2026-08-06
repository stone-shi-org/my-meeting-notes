import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import type { Note } from '@/types/api';

/**
 * Which timeline a note is being written onto.
 *
 * Two shapes because the two callers hold two different ids: the thread page
 * knows its thread, the transcript page knows its meeting and should not have
 * to fetch the thread first. Only creating and listing branch on this --
 * editing, appending and deleting all go through the thread routes using the
 * `thread_id` the server put on the note itself.
 */
export type NoteScope =
  | { kind: 'thread'; threadId: string }
  | { kind: 'meeting'; meetingId: string };

function scopePath(scope: NoteScope): string {
  return scope.kind === 'thread'
    ? `/threads/${scope.threadId}/notes`
    : `/meetings/${scope.meetingId}/notes`;
}

export function notesKey(scope: NoteScope): string[] {
  return scope.kind === 'thread'
    ? ['notes', 'thread', scope.threadId]
    : ['notes', 'meeting', scope.meetingId];
}

export interface NewNote {
  body: string;
  /** Omit to have the title generated from the body. */
  title?: string;
  source?: Note['source'];
  model?: string | null;
  /** The question the saved answer replied to. Only ever shown to the title
   * prompt -- it is not stored on the note. */
  question?: string;
}

/**
 * A note list plus every write against it.
 *
 * All five mutations share one invalidation sweep: a note changes the thread's
 * timeline, its counts and the staleness of its cached next step, so anything
 * less would leave one of those showing yesterday's answer.
 */
export function useNotes(scope: NoteScope, options: { enabled?: boolean } = {}) {
  const queryClient = useQueryClient();
  const key = notesKey(scope);

  const list = useQuery({
    queryKey: key,
    queryFn: () => api.get<Note[]>(scopePath(scope)),
    enabled: options.enabled ?? true,
  });

  function settle(note?: Note) {
    void queryClient.invalidateQueries({ queryKey: key });
    if (!note) return;
    const threadId = String(note.thread_id);
    void queryClient.invalidateQueries({ queryKey: ['notes', 'thread', threadId] });
    if (note.meeting_id != null) {
      void queryClient.invalidateQueries({
        queryKey: ['notes', 'meeting', String(note.meeting_id)],
      });
    }
    void queryClient.invalidateQueries({ queryKey: ['thread-timeline', threadId] });
    void queryClient.invalidateQueries({ queryKey: ['thread', threadId] });
    void queryClient.invalidateQueries({ queryKey: ['threads'] });
  }

  const create = useMutation({
    mutationFn: (input: NewNote) =>
      api.post<Note>(scopePath(scope), { source: 'manual', ...input }),
    onSuccess: settle,
  });

  const append = useMutation({
    mutationFn: ({ note, body }: { note: Note; body: string }) =>
      api.post<Note>(`/threads/${note.thread_id}/notes/${note.id}/append`, { body }),
    onSuccess: settle,
  });

  const update = useMutation({
    mutationFn: ({ note, title, body }: { note: Note; title?: string; body?: string }) =>
      api.patch<Note>(`/threads/${note.thread_id}/notes/${note.id}`, { title, body }),
    onSuccess: settle,
  });

  const remove = useMutation({
    mutationFn: (note: Note) => api.del(`/threads/${note.thread_id}/notes/${note.id}`),
    // The note is gone, so there is no row to derive the thread from -- take it
    // from what was passed in.
    onSuccess: (_data, note) => settle(note),
  });

  const move = useMutation({
    mutationFn: ({ note, targetThreadId }: { note: Note; targetThreadId: number }) =>
      api.post<Note>(`/threads/${note.thread_id}/notes/${note.id}/move`, {
        target_thread_id: targetThreadId,
      }),
    // settle(data) covers the destination; the source thread is not in the
    // response any more, so it needs its own invalidation from the argument.
    onSuccess: (data, { note }) => {
      settle(data);
      const sourceThreadId = String(note.thread_id);
      void queryClient.invalidateQueries({ queryKey: ['notes', 'thread', sourceThreadId] });
      void queryClient.invalidateQueries({ queryKey: ['thread-timeline', sourceThreadId] });
      void queryClient.invalidateQueries({ queryKey: ['thread', sourceThreadId] });
    },
  });

  return { list, create, append, update, remove, move };
}
