import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { MessageCircle, Send, Trash2, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { Spinner, Textarea } from '@/components/ui/primitives';
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
 * Floating chat panel for asking questions about a thread's meetings,
 * calendar events and emails. Visibility is controlled by the parent (a
 * toggle button in ThreadDetailPage's header), matching how the existing
 * Meetings/Events/Emails filters are local state rather than a routed tab.
 */
export function ThreadChatPanel({
  threadId,
  onClose,
}: {
  threadId: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  // null = idle, '' = waiting on the model, non-empty = tokens arriving.
  const [streamingText, setStreamingText] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<{ code: string; message: string } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

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
  }, [messages.length, streamingText]);

  // Abandoning the read on unmount/close doesn't stop the answer being
  // generated and saved server-side -- it just stops watching it arrive.
  useEffect(() => () => abortRef.current?.abort(), []);

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
        threadId,
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

  const settingsHint = streamError
    ? new ApiError(0, streamError.code, streamError.message).settingsHint
    : null;

  return (
    <div
      className="fixed inset-x-3 bottom-3 z-40 flex flex-col sm:inset-x-auto sm:right-4 sm:bottom-4 sm:w-96"
      style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}
    >
      <div className="flex h-[30rem] max-h-[70vh] flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-lg">
        <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
          <MessageCircle className="size-4 shrink-0 text-primary" aria-hidden />
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
            onClick={onClose}
            aria-label="Close chat"
            className="rounded p-1 text-fg-faint hover:bg-surface-2 hover:text-fg"
          >
            <X className="size-4" aria-hidden />
          </button>
        </div>

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

        <div className="border-t border-border p-3">
          {streamError && (
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
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  void submit();
                }
              }}
              placeholder="Ask a question…"
              rows={1}
              className="max-h-32 min-h-9 resize-none py-1.5"
            />
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
          </div>
        </div>
      </div>
    </div>
  );
}
