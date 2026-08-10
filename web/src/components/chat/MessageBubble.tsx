import { useQuery } from '@tanstack/react-query';
import { Check, Copy, NotebookPen, Plus, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Spinner } from '@/components/ui/primitives';
import { useNotes, type NoteScope } from '@/hooks/useNotes';
import { api } from '@/lib/api';
import { copyText } from '@/lib/clipboard';
import { cn } from '@/lib/cn';
import { renderMarkdown } from '@/lib/markdown';
import type { Paginated, Thread } from '@/types/api';

/** How long a "Copied"/"Saved" confirmation stays up before reverting. */
const FLASH_MS = 2000;

/**
 * One turn in either AI chat panel.
 *
 * Assistant replies are LLM-authored markdown; user turns are shown as the
 * literal text typed, not run through a renderer.
 *
 * Shared by ThreadChatPanel and TranscriptChatPanel, which are otherwise two
 * copies of the same component. The actions row is the reason to have factored
 * it out: "copy" and "save as a note" have to behave identically in both, and
 * the only thing that differs is which timeline the note lands on.
 */
export function MessageBubble({
  role,
  content,
  scope,
  pickThread,
  question,
  model,
  promptTokens,
  completionTokens,
}: {
  role: 'user' | 'assistant';
  content: string;
  /** Omitted while a reply is still streaming: there is nothing to save yet. */
  scope?: NoteScope;
  /** Set instead of `scope` when a chat has no single obvious thread (home
   * chat): "Add to note" asks which thread first, then behaves exactly like
   * `scope` from there. */
  pickThread?: boolean;
  /** The turn this reply answered. Makes for much better generated titles. */
  question?: string;
  model?: string | null;
  promptTokens?: number | null;
  completionTokens?: number | null;
}) {
  if (role === 'user') {
    return (
      <p className="max-w-[85%] whitespace-pre-wrap rounded-lg bg-primary-soft px-3 py-2 text-sm text-primary-soft-fg">
        {content}
      </p>
    );
  }

  return (
    <div className="max-w-[85%] min-w-0">
      <div
        className="prose prose-sm max-w-none rounded-lg bg-surface-2 px-3 py-2 dark:prose-invert prose-p:my-1 prose-ul:my-1 prose-ol:my-1"
        dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }}
      />
      {(scope || pickThread) && (
        <AssistantActions
          content={content}
          scope={scope}
          pickThread={pickThread}
          question={question}
          model={model}
          promptTokens={promptTokens}
          completionTokens={completionTokens}
        />
      )}
    </div>
  );
}

function AssistantActions({
  content,
  scope,
  pickThread,
  question,
  model,
  promptTokens,
  completionTokens,
}: {
  content: string;
  scope?: NoteScope;
  pickThread?: boolean;
  question?: string;
  model?: string | null;
  promptTokens?: number | null;
  completionTokens?: number | null;
}) {
  const [copied, setCopied] = useState<'ok' | 'failed' | null>(null);
  const [picking, setPicking] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(null), FLASH_MS);
    return () => window.clearTimeout(timer);
  }, [copied]);

  useEffect(() => {
    if (!saved) return;
    const timer = window.setTimeout(() => setSaved(null), FLASH_MS * 2);
    return () => window.clearTimeout(timer);
  }, [saved]);

  async function copy() {
    setCopied((await copyText(content)) ? 'ok' : 'failed');
  }

  return (
    <div className="mt-1">
      <div className="flex flex-wrap items-center gap-1">
        <ActionButton onClick={() => void copy()} icon={copied === 'ok' ? Check : Copy}>
          {copied === 'ok' ? 'Copied' : 'Copy'}
        </ActionButton>

        <ActionButton onClick={() => setPicking((v) => !v)} icon={NotebookPen} expanded={picking}>
          Add to note
        </ActionButton>

        {/* Copying can fail outright: on a plain-HTTP origin there is no
            Clipboard API, and execCommand can be refused too. Say so rather
            than flashing a tick that lied -- the text is still selectable. */}
        {copied === 'failed' && (
          <span role="status" className="text-2xs text-danger-ink">
            Clipboard blocked — select the text and copy
          </span>
        )}

        {saved && (
          <span role="status" className="min-w-0 truncate text-2xs text-success-ink">
            Saved to “{saved}”
          </span>
        )}

        {(model || (promptTokens != null && completionTokens != null)) && (
          <span className="ml-auto flex items-center gap-1.5 text-2xs text-fg-subtle">
            {model && <span className="truncate">{model}</span>}
            {promptTokens != null && completionTokens != null && (
              <span className="shrink-0">
                {promptTokens}→{completionTokens} tok
              </span>
            )}
          </span>
        )}
      </div>

      {/* In the flow rather than floating above it: this sits inside a
          scrolling history, where an absolutely positioned popover is clipped
          by the scroll container at whichever end it opens towards. */}
      {picking && pickThread && (
        <ThreadNotePicker
          content={content}
          question={question}
          model={model}
          onClose={() => setPicking(false)}
          onSaved={(title) => {
            setSaved(title);
            setPicking(false);
          }}
        />
      )}
      {picking && scope && (
        <NotePicker
          content={content}
          scope={scope}
          question={question}
          model={model}
          onClose={() => setPicking(false)}
          onSaved={(title) => {
            setSaved(title);
            setPicking(false);
          }}
        />
      )}
    </div>
  );
}

function ActionButton({
  onClick,
  icon: Icon,
  expanded,
  children,
}: {
  onClick: () => void;
  icon: typeof Copy;
  expanded?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-expanded={expanded}
      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-2xs font-medium text-fg-subtle hover:bg-surface-2 hover:text-fg"
    >
      <Icon className="size-3" aria-hidden />
      {children}
    </button>
  );
}

/**
 * "Which thread?" -- the step home chat's "Add to note" needs that thread and
 * meeting chat never do, since an answer there is already scoped to one. Once
 * a thread is picked this hands off to the exact same `NotePicker` those two
 * use, unmodified.
 */
function ThreadNotePicker({
  content,
  question,
  model,
  onClose,
  onSaved,
}: {
  content: string;
  question?: string;
  model?: string | null;
  onClose: () => void;
  onSaved: (title: string) => void;
}) {
  const [threadId, setThreadId] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  // Same query key MoveToThread.tsx uses for its own thread picker, so the
  // two share one cached fetch instead of each paying for their own.
  const threads = useQuery({
    queryKey: ['threads', 'picker'],
    queryFn: () => api.get<Paginated<Thread>>('/threads', { page_size: 200 }),
  });

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKeyDown);
    ref.current?.scrollIntoView({ block: 'nearest' });
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  if (threadId) {
    return (
      <NotePicker
        content={content}
        scope={{ kind: 'thread', threadId }}
        question={question}
        model={model}
        onClose={onClose}
        onSaved={onSaved}
      />
    );
  }

  return (
    <div
      ref={ref}
      role="group"
      aria-label="Choose a thread for this note"
      className="mt-1 overflow-hidden rounded-lg border border-border bg-surface shadow-sm"
    >
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <p className="flex-1 text-xs font-semibold">Save to which thread?</p>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="rounded p-0.5 text-fg-faint hover:bg-surface-2 hover:text-fg"
        >
          <X className="size-3.5" aria-hidden />
        </button>
      </div>

      {threads.isLoading && (
        <p className="px-3 py-2 text-xs text-fg-subtle">Loading threads…</p>
      )}

      {threads.data && threads.data.items.length === 0 && (
        <p className="px-3 py-2 text-xs text-fg-subtle">No threads yet.</p>
      )}

      {threads.data && threads.data.items.length > 0 && (
        <ul className="max-h-48 overflow-y-auto py-1">
          {threads.data.items.map((t) => (
            <li key={t.id}>
              <button
                type="button"
                onClick={() => setThreadId(String(t.id))}
                className="block w-full truncate px-3 py-1.5 text-left text-sm hover:bg-surface-2"
              >
                {t.title}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * "New note, or add to one of these."
 *
 * A new note is titled by the model from the answer's own text; appending to an
 * existing one deliberately leaves that note's title alone, since the user
 * picked it by name.
 */
function NotePicker({
  content,
  scope,
  question,
  model,
  onClose,
  onSaved,
}: {
  content: string;
  scope: NoteScope;
  question?: string;
  model?: string | null;
  onClose: () => void;
  onSaved: (title: string) => void;
}) {
  const { list, create, append } = useNotes(scope);
  const ref = useRef<HTMLDivElement>(null);
  const pending = create.isPending || append.isPending;
  const error = (create.error ?? append.error) as Error | null;

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKeyDown);
    // Opening near the bottom of the history leaves it half off-screen.
    ref.current?.scrollIntoView({ block: 'nearest' });
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  const notes = list.data ?? [];

  return (
    <div
      ref={ref}
      role="group"
      aria-label="Save this answer as a note"
      className="mt-1 overflow-hidden rounded-lg border border-border bg-surface shadow-sm"
    >
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <p className="flex-1 text-xs font-semibold">Save this answer</p>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="rounded p-0.5 text-fg-faint hover:bg-surface-2 hover:text-fg"
        >
          <X className="size-3.5" aria-hidden />
        </button>
      </div>

      <button
        type="button"
        disabled={pending}
        onClick={() =>
          create.mutate(
            { body: content, source: 'ai_chat', model, question },
            { onSuccess: (note) => onSaved(note.title) },
          )
        }
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-surface-2 disabled:opacity-50"
      >
        {create.isPending ? (
          <Spinner className="size-3.5 shrink-0" />
        ) : (
          <Plus className="size-3.5 shrink-0 text-primary" aria-hidden />
        )}
        <span className="min-w-0 flex-1">
          New note
          <span className="block text-2xs text-fg-subtle">
            {create.isPending ? 'Naming it…' : 'Titled for you from the answer'}
          </span>
        </span>
      </button>

      {notes.length > 0 && (
        <>
          <p className="border-t border-border px-3 pb-1 pt-2 text-2xs font-semibold uppercase tracking-wide text-fg-subtle">
            Or add to
          </p>
          <ul className="max-h-48 overflow-y-auto pb-1">
            {notes.map((note) => (
              <li key={note.id}>
                <button
                  type="button"
                  disabled={pending}
                  onClick={() =>
                    append.mutate(
                      { note, body: content },
                      { onSuccess: (updated) => onSaved(updated.title) },
                    )
                  }
                  className="block w-full truncate px-3 py-1.5 text-left text-sm hover:bg-surface-2 disabled:opacity-50"
                >
                  {note.title}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      {error && <p className="px-3 pb-2 text-2xs text-danger-ink">{error.message}</p>}
    </div>
  );
}

/** Kept here so both panels agree on what "waiting on the model" looks like. */
export function ThinkingBubble({ className }: { className?: string }) {
  return (
    <p
      className={cn(
        'max-w-[85%] rounded-lg bg-surface-2 px-3 py-2 text-sm text-fg-subtle',
        className,
      )}
    >
      <span className="inline-flex items-center gap-2">
        <Spinner className="size-3.5" />
        Thinking…
      </span>
    </p>
  );
}
