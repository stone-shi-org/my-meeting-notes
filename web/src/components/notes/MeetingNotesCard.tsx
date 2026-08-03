import { NotebookPen } from 'lucide-react';
import { useState } from 'react';
import { NoteCard, NoteComposer } from '@/components/notes/NoteCard';
import { Button } from '@/components/ui/Button';
import { Card, Skeleton } from '@/components/ui/primitives';
import { ErrorState } from '@/components/ui/states';
import { useNotes, type NoteScope } from '@/hooks/useNotes';

/**
 * The notes filed on one meeting, on its transcript page.
 *
 * Only this meeting's -- notes written on the thread as a whole belong to the
 * thread's timeline, and pulling them in here would make it look as though
 * they were about this recording.
 */
export function MeetingNotesCard({ meetingId }: { meetingId: number }) {
  const scope: NoteScope = { kind: 'meeting', meetingId: String(meetingId) };
  const { list } = useNotes(scope);
  const [composing, setComposing] = useState(false);

  const notes = list.data ?? [];

  return (
    <Card className="p-5">
      <div className="mb-3 flex items-center gap-2">
        <h2 className="flex-1 font-display text-lg font-semibold">
          Notes
          {notes.length > 0 && (
            <span className="ml-2 text-sm font-normal text-fg-subtle tabular">{notes.length}</span>
          )}
        </h2>
        <Button size="sm" variant="ghost" onClick={() => setComposing((v) => !v)}>
          <NotebookPen />
          New note
        </Button>
      </div>

      {composing && (
        <div className="mb-3">
          <NoteComposer scope={scope} onDone={() => setComposing(false)} />
        </div>
      )}

      {list.isLoading && <Skeleton className="h-20 w-full" />}
      {list.isError && <ErrorState error={list.error} onRetry={() => list.refetch()} />}

      {list.data && notes.length === 0 && !composing && (
        <p className="text-sm text-fg-subtle">
          Nothing yet. Write one here, or save an answer out of the AI chat with “Add to note”.
        </p>
      )}

      {notes.length > 0 && (
        <ul className="space-y-2">
          {notes.map((note) => (
            <li key={note.id}>
              <NoteCard note={note} scope={scope} />
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
