import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Minimize2, Send, Sparkles, Trash2 } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { Select, Spinner, Textarea } from '@/components/ui/primitives';
import { useChatModel } from '@/hooks/useChatModel';
import { api } from '@/lib/api';
import { streamChat } from '@/lib/chatStream';
import { cn } from '@/lib/cn';
import { renderMarkdown } from '@/lib/markdown';
import { ApiError, type ChatMessage } from '@/types/api';

/** Assistant replies are LLM-authored markdown; user turns are shown as the
 * literal text typed, not run through a renderer. */
function MessageBubble({ role, content }: { role: 'user' | 'assistant'; content: string }) {
  if (role === 'user') {
    return (
      <p className="max-w-[85%] whitespace-pre-wrap rounded-lg bg-primary-soft px-3 py-2 text-sm text-primary-soft-fg">
        {content}
      </p>
    );
  }
  return (
    <div
      className="prose prose-sm max-w-[85%] rounded-lg bg-surface-2 px-3 py-2 dark:prose-invert prose-p:my-1 prose-ul:my-1 prose-ol:my-1"
      dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }}
    />
  );
}

/**
 * Always-on assistant for asking questions about a thread's meetings,
 * calendar events and emails. Rendered unconditionally by ThreadDetailPage as
 * a docked input pill in the bottom-right corner; focusing it expands the
 * same element in place into a full-height right side panel, and the
 * minimize button collapses it back to just the pill. There is no fully
 * hidden state -- the entry point is always reachable.
 */
export function ThreadChatPanel({ threadId }: { threadId: string }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const [expanded, setExpanded] = useState(false);
  // null = idle, '' = waiting on the model, non-empty = tokens arriving.
  const [streamingText, setStreamingText] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<{ code: string; message: string } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const chatModel = useChatModel();

  const messagesQuery = useQuery({
    queryKey: ['thread-chat', threadId],
    queryFn: () => api.get<ChatMessage[]>(`/threads/${threadId}/chat`),
    enabled: !!threadId,
  });

  const messages = messagesQuery.data ?? [];

  const clear = useMutation({
    mutationFn: () => api.del(`/threads/${threadId}/chat`),
    onSuccess: () => {
      queryClient.setQueryData<ChatMessage[]>(['thread-chat', threadId], []);
      setStreamError(null);
    },
  });

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [messages.length, streamingText, expanded]);

  // Abandoning the read on unmount/collapse doesn't stop the answer being
  // generated and saved server-side -- it just stops watching it arrive.
  useEffect(() => () => abortRef.current?.abort(), []);

  // The pill and the panel are different subtrees (header/history are only
  // mounted once expanded), so the textarea remounts across that switch and
  // loses focus. Put it back once the expanded layout has committed.
  useEffect(() => {
    if (expanded) textareaRef.current?.focus();
  }, [expanded]);

  async function submit() {
    const message = draft.trim();
    if (!message || streamingText !== null) return;
    setDraft('');
    setStreamError(null);

    const optimisticUser: ChatMessage = {
      id: -Date.now(),
      thread_id: Number(threadId),
      role: 'user',
      content: message,
      model: null,
      created_at: new Date().toISOString(),
    };
    queryClient.setQueryData<ChatMessage[]>(['thread-chat', threadId], (prev) => [
      ...(prev ?? []),
      optimisticUser,
    ]);
    setStreamingText('');

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await streamChat(
        `/threads/${threadId}/chat`,
        message,
        {
          onToken: (text) => setStreamingText((prev) => (prev ?? '') + text),
          onDone: (assistantMessage) => {
            queryClient.setQueryData<ChatMessage[]>(['thread-chat', threadId], (prev) => [
              ...(prev ?? []),
              assistantMessage,
            ]);
            setStreamingText(null);
          },
          onError: (err) => {
            setStreamError(err);
            setStreamingText(null);
          },
        },
        controller.signal,
        chatModel.selected,
      );
    } catch (err) {
      if (controller.signal.aborted) return;
      setStreamError({
        code: err instanceof ApiError ? err.code : 'network_error',
        message: err instanceof Error ? err.message : 'Something went wrong',
      });
      setStreamingText(null);
    }
  }

  function minimize() {
    setExpanded(false);
    textareaRef.current?.blur();
  }

  const settingsHint = streamError
    ? new ApiError(0, streamError.code, streamError.message).settingsHint
    : null;

  return (
    <div
      className={cn(
        'fixed z-30 flex flex-col overflow-hidden border-border bg-surface transition-all duration-slow ease-out motion-reduce:transition-none',
        expanded
          ? // Docked below the app header (h-14), not over it, so the nav stays reachable.
            'inset-x-0 top-14 bottom-0 border-l shadow-xl sm:inset-x-auto sm:right-0 sm:w-[28rem]'
          : 'bottom-4 right-4 w-64 rounded-full border shadow-lg motion-safe:animate-glow sm:w-80',
      )}
      style={!expanded ? { paddingBottom: 'env(safe-area-inset-bottom)' } : undefined}
    >
      {expanded && (
        <div className="border-b border-border px-4 py-2.5">
          <div className="flex items-center gap-2">
            <Sparkles className="size-4 shrink-0 text-primary" aria-hidden />
            <h2 className="flex-1 text-sm font-semibold">Ask about this thread</h2>
            {messages.length > 0 && (
              <button
                type="button"
                onClick={() => {
                  if (window.confirm('Clear this conversation? This cannot be undone.')) {
                    clear.mutate();
                  }
                }}
                disabled={streamingText !== null || clear.isPending}
                aria-label="Clear conversation"
                title="Clear conversation"
                className="rounded p-1 text-fg-faint hover:bg-surface-2 hover:text-danger-ink disabled:opacity-50"
              >
                <Trash2 className="size-4" aria-hidden />
              </button>
            )}
            <button
              type="button"
              onClick={minimize}
              aria-label="Minimize chat"
              title="Minimize"
              className="rounded p-1 text-fg-faint hover:bg-surface-2 hover:text-fg"
            >
              <Minimize2 className="size-4" aria-hidden />
            </button>
          </div>

          {chatModel.options.length > 1 && (
            <Select
              aria-label="Chat model"
              className="mt-2 h-7 text-xs"
              value={chatModel.selected ?? ''}
              onChange={(e) => chatModel.setModel(e.target.value)}
            >
              {chatModel.options.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </Select>
          )}
        </div>
      )}

      {expanded && (
        <div className="flex-1 space-y-3 overflow-y-auto p-4">
          {messagesQuery.isLoading && (
            <p className="text-sm text-fg-subtle">Loading conversation…</p>
          )}

          {!messagesQuery.isLoading && messages.length === 0 && streamingText === null && (
            <p className="text-sm text-fg-subtle">
              Ask about the meetings, calendar events and emails attached to this thread —
              decisions made, action items, who owns what, or a specific meeting's transcript.
            </p>
          )}

          {messages.map((message) => (
            <div
              key={message.id}
              className={cn('flex', message.role === 'user' ? 'justify-end' : 'justify-start')}
            >
              <MessageBubble role={message.role} content={message.content} />
            </div>
          ))}

          {streamingText !== null && (
            <div className="flex justify-start">
              {streamingText === '' ? (
                <p className="max-w-[85%] rounded-lg bg-surface-2 px-3 py-2 text-sm text-fg-subtle">
                  <span className="inline-flex items-center gap-2">
                    <Spinner className="size-3.5" />
                    Thinking…
                  </span>
                </p>
              ) : (
                <MessageBubble role="assistant" content={streamingText} />
              )}
            </div>
          )}

          <div ref={bottomRef} />
        </div>
      )}

      <div className={expanded ? 'border-t border-border p-3' : 'py-1.5 pl-4 pr-1.5'}>
        {expanded && streamError && (
          <p className="mb-2 text-xs text-danger-ink">
            {streamError.message}
            {settingsHint && (
              <>
                {' — '}
                <Link to={settingsHint} className="underline underline-offset-2">
                  open settings
                </Link>
              </>
            )}
          </p>
        )}
        <div className="flex items-end gap-2">
          <Textarea
            ref={textareaRef}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onFocus={() => setExpanded(true)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void submit();
              }
            }}
            placeholder="Ask about this thread…"
            rows={1}
            className={cn(
              'max-h-32 min-h-9 resize-none py-1.5',
              !expanded &&
                'border-0 bg-transparent shadow-none focus-visible:ring-0 focus-visible:ring-offset-0',
            )}
          />
          {expanded ? (
            <Button
              size="icon"
              variant="primary"
              aria-label="Send"
              loading={streamingText !== null}
              disabled={!draft.trim()}
              onClick={() => void submit()}
            >
              <Send />
            </Button>
          ) : (
            <button
              type="button"
              onClick={() => setExpanded(true)}
              aria-label="Open AI chat"
              title="Ask about this thread"
              className="flex size-8 shrink-0 items-center justify-center rounded-full bg-primary text-primary-fg transition-transform duration-fast hover:scale-105 active:scale-95 motion-reduce:transition-none motion-reduce:hover:scale-100"
            >
              <Sparkles className="size-4" aria-hidden />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
