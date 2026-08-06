import { Check, Copy, Pencil, Sparkles, Trash2, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { MoveToThread } from '@/components/thread/MoveToThread';
import { Button } from '@/components/ui/Button';
import { Badge, Input, Textarea } from '@/components/ui/primitives';
import { useNotes, type NoteScope } from '@/hooks/useNotes';
import { copyText } from '@/lib/clipboard';
import { cn } from '@/lib/cn';
import { renderMarkdown } from '@/lib/markdown';
import { fmtRelative } from '@/lib/time';
import type { Note } from '@/types/api';

/** Collapsed height of a long note body, in the timeline. */
const CLAMP = 'max-h-40 overflow-hidden';

/**
 * One note, read or edited in place.
 *
 * Editing is inline rather than on a page of its own: a note is a paragraph or
 * two next to the meeting it belongs to, and routing away to change a word
 * would lose the context that made it worth writing.
 */
export function NoteCard({
  note,
  scope,
  className,
}: {
  note: Note;
  scope: NoteScope;
  className?: string;
}) {
  const { update, remove, move } = useNotes(scope, { enabled: false });
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(note.title);
  const [body, setBody] = useState(note.body);
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  // Whether the clamped body is actually cut off. Measured rather than guessed
  // from length, so "Show more" never appears on a note that is fully visible.
  const [clipped, setClipped] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);

  const html = useMemo(() => renderMarkdown(note.body), [note.body]);
  const ai = note.source === 'ai_chat';
  const edited = note.updated_at !== note.created_at;

  // Another tab (or the append button in a chat panel) can move the note under
  // an open editor. Re-seed the drafts when the saved text actually changes.
  useEffect(() => {
    if (!editing) {
      setTitle(note.title);
      setBody(note.body);
    }
  }, [note.title, note.body, editing]);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  useEffect(() => {
    const el = bodyRef.current;
    if (!el || expanded) return;
    setClipped(el.scrollHeight > el.clientHeight + 1);
  }, [html, expanded]);

  function save() {
    const nextTitle = title.trim();
    const nextBody = body.trim();
    if (!nextTitle || !nextBody) return;
    update.mutate(
      { note, title: nextTitle, body: nextBody },
      { onSuccess: () => setEditing(false) },
    );
  }

  if (editing) {
    return (
      <div className={cn('rounded-md border-l-2 border-entity-note bg-surface-2/50 p-3', className)}>
        <Input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          aria-label="Note title"
          placeholder="Title"
          className="h-8 font-medium"
        />
        <Textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          aria-label="Note"
          rows={8}
          className="mt-2 text-sm"
        />
        <div className="mt-2 flex items-center gap-2">
          <Button size="sm" variant="primary" loading={update.isPending} onClick={save}>
            Save
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setEditing(false);
              setTitle(note.title);
              setBody(note.body);
            }}
          >
            Cancel
          </Button>
          {update.error && (
            <p className="text-xs text-danger-ink">{(update.error as Error).message}</p>
          )}
        </div>
      </div>
    );
  }

  return (
    <div
      className={cn(
        'group rounded-md border-l-2 border-entity-note bg-surface-2/50 py-2 pl-3 pr-3',
        className,
      )}
    >
      <div className="flex items-start gap-2">
        <p className="min-w-0 flex-1 text-sm font-medium">{note.title}</p>
        {ai && (
          <Badge variant="primary" size="sm" className="shrink-0">
            <Sparkles className="size-3" aria-hidden />
            AI
          </Badge>
        )}
        <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
          <IconAction
            label={copied ? 'Copied' : 'Copy note'}
            icon={copied ? Check : Copy}
            onClick={() => void copyText(note.body).then(setCopied)}
          />
          <IconAction label="Edit note" icon={Pencil} onClick={() => setEditing(true)} />
          <MoveToThread
            currentThreadId={String(note.thread_id)}
            pending={move.isPending}
            label="Move this note to another thread"
            onMove={(targetThreadId) => move.mutate({ note, targetThreadId })}
          />
          <IconAction
            label="Delete note"
            icon={Trash2}
            danger
            pending={remove.isPending}
            onClick={() => {
              // Unlike detaching an email, this is not recoverable by running
              // the match again -- the text only exists here.
              if (window.confirm(`Delete the note “${note.title}”? This cannot be undone.`)) {
                remove.mutate(note);
              }
            }}
          />
        </div>
      </div>

      <div
        ref={bodyRef}
        className={cn(
          'prose prose-sm mt-1 max-w-none dark:prose-invert prose-p:my-1 prose-ul:my-1 prose-ol:my-1',
          !expanded && CLAMP,
        )}
        dangerouslySetInnerHTML={{ __html: html }}
      />

      <div className="mt-1 flex flex-wrap items-center gap-x-3 text-xs text-fg-subtle">
        {(clipped || expanded) && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-primary hover:underline"
          >
            {expanded ? 'Show less' : 'Show more'}
          </button>
        )}
        <time dateTime={note.created_at}>{fmtRelative(note.created_at)}</time>
        {edited && <span>edited {fmtRelative(note.updated_at)}</span>}
        {remove.error && (
          <span className="text-danger-ink">{(remove.error as Error).message}</span>
        )}
        {move.error && (
          <span className="text-danger-ink">{(move.error as Error).message}</span>
        )}
      </div>
    </div>
  );
}

function IconAction({
  label,
  icon: Icon,
  onClick,
  danger,
  pending,
}: {
  label: string;
  icon: typeof Copy;
  onClick: () => void;
  danger?: boolean;
  pending?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={pending}
      aria-label={label}
      title={label}
      className={cn(
        'rounded p-1 text-fg-faint disabled:opacity-50',
        danger ? 'hover:text-danger-ink' : 'hover:text-fg',
      )}
    >
      <Icon className="size-3.5" aria-hidden />
    </button>
  );
}

/**
 * The "New note" form.
 *
 * The title field is optional and says so: leaving it blank hands the naming
 * to the model, which is the same thing "Add to note" does from a chat reply.
 */
export function NoteComposer({
  scope,
  onDone,
}: {
  scope: NoteScope;
  onDone?: () => void;
}) {
  const { create } = useNotes(scope, { enabled: false });
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');

  function submit() {
    const text = body.trim();
    if (!text) return;
    create.mutate(
      { body: text, title: title.trim() || undefined, source: 'manual' },
      {
        onSuccess: () => {
          setTitle('');
          setBody('');
          onDone?.();
        },
      },
    );
  }

  return (
    <div className="rounded-md border border-border-strong bg-surface-2/60 p-3">
      <Input
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        aria-label="Note title"
        placeholder="Title (optional — one is written for you)"
        className="h-8"
      />
      <Textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        aria-label="Note"
        placeholder="Markdown is rendered."
        rows={5}
        className="mt-2 text-sm"
        autoFocus
      />
      <div className="mt-2 flex items-center gap-2">
        <Button
          size="sm"
          variant="primary"
          loading={create.isPending}
          disabled={!body.trim()}
          onClick={submit}
        >
          {create.isPending && !title.trim() ? 'Naming it…' : 'Save note'}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            setTitle('');
            setBody('');
            onDone?.();
          }}
        >
          <X />
          Cancel
        </Button>
        {create.error && (
          <p className="text-xs text-danger-ink">{(create.error as Error).message}</p>
        )}
      </div>
    </div>
  );
}
