import { X } from 'lucide-react';
import { useEffect, useRef, useState, type ComponentProps } from 'react';
import { MessageBubble, ThinkingBubble } from '@/components/chat/MessageBubble';
import { ToolCallBubble, type ToolCall } from '@/components/chat/ToolCallBubble';
import { api } from '@/lib/api';
import { streamChat } from '@/lib/chatStream';
import { cn } from '@/lib/cn';
import { ApiError } from '@/types/api';

interface Slot<T> {
  model: string;
  text: string;
  status: 'streaming' | 'done' | 'error';
  /** The response stream has closed -- follow-up chips arrive after `done`. */
  finished: boolean;
  message: T | null;
  toolCalls: ToolCall[];
  suggestions: string[];
  error: { code: string; message: string } | null;
}

/** What the panel gets back once the surviving answer has finished. */
export interface BestOfTwoOutcome<T> {
  /** The kept, already-saved answer; null when it failed. */
  message: T | null;
  model: string;
  toolCalls: ToolCall[];
  suggestions: string[];
  error: { code: string; message: string } | null;
}

/**
 * One prompt, two models, side by side.
 *
 * Each side is an ordinary chat request (`streamChat`), so each persists its
 * own question and answer. Closing a side keeps the other: the closed answer
 * is deleted server-side (`discardPath`) if it had already been saved, or
 * simply abandoned if it had not -- an aborted request never persists. The
 * surviving model becomes the panel's single selected model, and once its
 * answer has finished the panel is handed it via `onSettled` and takes this
 * component down, returning to the normal single-model view.
 */
export function BestOfTwoAnswers<T extends { id: number; content: string }>({
  path,
  message,
  models,
  modelName,
  discardPath,
  bubbleProps,
  onChoose,
  onSettled,
  onBusyChange,
}: {
  path: string;
  message: string;
  models: [string, string];
  modelName: (id: string) => string;
  /** Endpoint that deletes a saved answer (and the question it answered). */
  discardPath: (messageId: number) => string;
  /** Note-saving props for a finished answer (`scope` / `pickThread` / `question`). */
  bubbleProps?: Partial<ComponentProps<typeof MessageBubble>>;
  /** The user closed the other side: this model is now the single selection. */
  onChoose: (model: string) => void;
  onSettled: (outcome: BestOfTwoOutcome<T>) => void;
  /** True while either side is still generating, for the panel's Stop button. */
  onBusyChange?: (busy: boolean) => void;
}) {
  const [slots, setSlots] = useState<Slot<T>[]>(() =>
    models.map((model) => ({
      model,
      text: '',
      status: 'streaming',
      finished: false,
      message: null,
      toolCalls: [],
      suggestions: [],
      error: null,
    })),
  );
  // Index of the side the user closed; the other one is the keeper.
  const [closed, setClosed] = useState<0 | 1 | null>(null);
  const controllers = useRef<AbortController[]>([]);
  const settled = useRef(false);
  // Tool hops per side, mirrored synchronously for the same reason the panels do.
  const toolRefs = useRef<ToolCall[][]>([[], []]);

  function patch(i: number, change: (s: Slot<T>) => Partial<Slot<T>>) {
    setSlots((prev) => prev.map((s, j) => (j === i ? { ...s, ...change(s) } : s)));
  }

  useEffect(() => {
    const ctrls = models.map(() => new AbortController());
    controllers.current = ctrls;
    models.forEach((model, i) => {
      streamChat<T>(
        path,
        message,
        {
          onToken: (text) => patch(i, (s) => ({ text: s.text + text })),
          onDone: (saved) => patch(i, () => ({ status: 'done', message: saved })),
          onError: (error) => patch(i, () => ({ status: 'error', error })),
          onSuggestions: (suggestions) => patch(i, () => ({ suggestions })),
          onToolCall: (tool, arg) => {
            toolRefs.current[i] = [...toolRefs.current[i], { tool, arg }];
            patch(i, () => ({ toolCalls: toolRefs.current[i] }));
          },
          onToolResult: (_tool, _arg, result) => {
            const next = [...toolRefs.current[i]];
            const last = next.length - 1;
            if (last >= 0) next[last] = { ...next[last], result };
            toolRefs.current[i] = next;
            patch(i, () => ({ toolCalls: next }));
          },
        },
        ctrls[i].signal,
        model,
      )
        .then(() => patch(i, () => ({ finished: true })))
        .catch((err) => {
        if (ctrls[i].signal.aborted) return;
        patch(i, () => ({
          finished: true,
          status: 'error',
          error: {
            code: err instanceof ApiError ? err.code : 'network_error',
            message: err instanceof Error ? err.message : 'Something went wrong',
          },
        }));
      });
    });
    // Unmounting abandons the reads; a side that never finished is never saved.
    return () => ctrls.forEach((c) => c.abort());
    // One compare per mount: the panel remounts this with a new key per send.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const busy = slots.some((s) => s.status === 'streaming');
  useEffect(() => {
    onBusyChange?.(busy);
  }, [busy, onBusyChange]);

  // Closed side chosen, and the keeper has finished (or failed): hand it back.
  useEffect(() => {
    if (closed === null || settled.current) return;
    const keeper = slots[closed === 0 ? 1 : 0];
    // Wait for the stream to close, not just for `done`: the follow-up chips
    // arrive after it, and settling now would abandon them mid-flight.
    if (!keeper.finished) return;
    settled.current = true;
    onSettled({
      message: keeper.message,
      model: keeper.model,
      toolCalls: keeper.toolCalls,
      suggestions: keeper.suggestions,
      error: keeper.error,
    });
  }, [closed, slots, onSettled]);

  function close(i: 0 | 1) {
    if (closed !== null) return;
    const loser = slots[i];
    // An unfinished answer is never saved once its request is aborted; one
    // that already arrived is, so take it (and its question) back out.
    controllers.current[i]?.abort();
    if (loser.message) void api.del(discardPath(loser.message.id)).catch(() => undefined);
    setClosed(i);
    onChoose(slots[i === 0 ? 1 : 0].model);
  }

  const visible = slots.map((slot, i) => ({ slot, i: i as 0 | 1 })).filter(({ i }) => closed !== i);

  return (
    <div
      className={cn(
        'grid min-w-0 gap-2',
        visible.length === 2 ? 'grid-cols-1 sm:grid-cols-2' : 'grid-cols-1',
      )}
      role="group"
      aria-label="Best of 2 answers"
    >
      {visible.map(({ slot, i }) => (
        <section
          key={i}
          aria-label={`Answer from ${modelName(slot.model)}`}
          className="min-w-0 space-y-2 rounded-lg border border-border p-2"
        >
          <header className="flex items-center gap-1">
            <h3 className="min-w-0 flex-1 truncate text-xs font-semibold text-fg-muted">
              {modelName(slot.model)}
            </h3>
            {closed === null && (
              <button
                type="button"
                onClick={() => close(i)}
                aria-label={`Close ${modelName(slot.model)} answer`}
                title="Close this answer and keep the other"
                className="rounded p-1 text-fg-faint hover:bg-surface-2 hover:text-fg"
              >
                <X className="size-3.5" aria-hidden />
              </button>
            )}
          </header>

          {slot.toolCalls.map((call, j) => (
            <div key={j} className="[&>*]:max-w-full">
              <ToolCallBubble call={call} />
            </div>
          ))}

          <div className="[&>div]:max-w-full">
            {slot.status === 'error' ? (
              <p className="text-xs text-danger-ink">{slot.error?.message}</p>
            ) : slot.status === 'streaming' && slot.text === '' ? (
              <ThinkingBubble />
            ) : slot.status === 'streaming' ? (
              <MessageBubble role="assistant" content={slot.text} isStreaming />
            ) : (
              <MessageBubble
                role="assistant"
                content={slot.message?.content ?? slot.text}
                model={slot.model}
                {...bubbleProps}
              />
            )}
          </div>
        </section>
      ))}
    </div>
  );
}
