import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Minimize2, Send, Sparkles, Square, Trash2 } from 'lucide-react';
import { Fragment, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { BestOfTwoAnswers, type BestOfTwoOutcome } from '@/components/chat/BestOfTwoAnswers';
import { ChatModelPicker } from '@/components/chat/ChatModelPicker';
import { ChatPanelResizeHandle } from '@/components/chat/ChatPanelResizeHandle';
import { FollowUpChips } from '@/components/chat/FollowUpChips';
import { MessageBubble, ThinkingBubble } from '@/components/chat/MessageBubble';
import { ToolCallBubble, type ToolCall } from '@/components/chat/ToolCallBubble';
import { Button } from '@/components/ui/Button';
import { ConfirmDialog } from '@/components/ui/ConfirmDialog';
import { Textarea } from '@/components/ui/primitives';
import { useAutoResizeTextarea } from '@/hooks/useAutoResizeTextarea';
import { useChatDraft } from '@/hooks/useChatDraft';
import { useChatModel } from '@/hooks/useChatModel';
import { useChatPanelWidth } from '@/hooks/useChatPanelWidth';
import type { NoteScope } from '@/hooks/useNotes';
import { api } from '@/lib/api';
import { streamChat } from '@/lib/chatStream';
import { cn } from '@/lib/cn';
import { ApiError, type ChatMessage } from '@/types/api';

const THREAD_STARTER_PROMPTS = [
  "What's next?",
  'Summarize the decisions made',
  'Who owns what?',
  "What's still outstanding?",
];

/**
 * Always-on assistant for asking questions about a thread's meetings,
 * calendar events, emails and notes. Rendered unconditionally by ThreadDetailPage as
 * a docked input pill in the bottom-right corner; focusing it expands the
 * same element in place into a full-height right side panel, and the
 * minimize button collapses it back to just the pill. There is no fully
 * hidden state -- the entry point is always reachable.
 */
export function ThreadChatPanel({ threadId }: { threadId: string }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useChatDraft(`mmn:draft:thread:${threadId}`);
  const [expanded, setExpanded] = useState(false);
  const [confirmClearOpen, setConfirmClearOpen] = useState(false);
  // null = idle, '' = waiting on the model, non-empty = tokens arriving.
  const [streamingText, setStreamingText] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<{ code: string; message: string } | null>(null);
  // Chips suggested from the turn that just finished. Ephemeral -- lost on
  // reload, and replaced (never merged) by the next round's own suggestions.
  const [followUps, setFollowUps] = useState<string[]>([]);
  // The tool hops of the round currently streaming, in order. Moved onto
  // `toolCallsByMessageId` once that round's answer is saved, so a hop stays
  // attached to the message it informed rather than to "whatever's live".
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([]);
  const [toolCallsByMessageId, setToolCallsByMessageId] = useState<Record<number, ToolCall[]>>({});
  // Mirrors `toolCalls` synchronously -- `onDone` needs the hops from *this*
  // round the instant it fires, and reading state from inside a callback
  // closed over an earlier render would risk missing ones added since.
  const toolCallsRef = useRef<ToolCall[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const chatModel = useChatModel();
  // Best of 2: the prompt currently shown as two side-by-side answers, if any.
  // `key` remounts the comparison for each send.
  const [compare, setCompare] = useState<{ message: string; models: [string, string]; key: number } | null>(null);
  const [compareBusy, setCompareBusy] = useState(false);
  const chatWidth = useChatPanelWidth();
  const noteScope: NoteScope = { kind: 'thread', threadId };
  useAutoResizeTextarea(textareaRef, draft);

  const handleScroll = () => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    setPinned(atBottom);
  };

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
      setFollowUps([]);
      setToolCallsByMessageId({});
    },
  });

  useEffect(() => {
    if (pinned) {
      bottomRef.current?.scrollIntoView({ block: 'end' });
    }
  }, [messages.length, streamingText, expanded, followUps, toolCalls, compare, pinned]);

  // Abandoning the read on unmount/collapse doesn't stop the answer being
  // generated and saved server-side -- it just stops watching it arrive.
  useEffect(() => () => abortRef.current?.abort(), []);

  // The pill and the panel are different subtrees (header/history are only
  // mounted once expanded), so the textarea remounts across that switch and
  // loses focus. Put it back once the expanded layout has committed.
  useEffect(() => {
    if (expanded) textareaRef.current?.focus();
  }, [expanded]);

  // Collapse on any click that lands outside the panel -- mousedown, not
  // click, so it fires before a focus change elsewhere steals the outcome.
  useEffect(() => {
    if (!expanded) return;
    function handlePointerDown(e: MouseEvent) {
      if (chatWidth.isDraggingRef.current) return;
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        minimize();
      }
    }
    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [expanded]);

  // Escape cancels a reply in flight -- this only aborts the client's read,
  // same as unmounting does above; the server keeps generating and persisting
  // the answer regardless of whether anyone's still listening.
  useEffect(() => {
    if (streamingText === null && !compareBusy) return;
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') cancelStream();
    }
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [streamingText, compareBusy]);

  function cancelStream() {
    abortRef.current?.abort();
    setCompare(null);
    setCompareBusy(false);
    toolCallsRef.current = [];
    setToolCalls([]);
    setStreamingText(null);
  }

  async function submit(overrideMessage?: string) {
    const message = (overrideMessage ?? draft).trim();
    if (!message || streamingText !== null || compare) return;
    setDraft('');
    setStreamError(null);
    setFollowUps([]);
    toolCallsRef.current = [];
    setToolCalls([]);

    const optimisticUser: ChatMessage = {
      id: -Date.now(),
      thread_id: Number(threadId),
      role: 'user',
      content: message,
      model: null,
      prompt_tokens: null,
      completion_tokens: null,
      created_at: new Date().toISOString(),
    };
    queryClient.setQueryData<ChatMessage[]>(['thread-chat', threadId], (prev) => [
      ...(prev ?? []),
      optimisticUser,
    ]);
    const pair = chatModel.pair;
    if (pair) {
      setCompare({ message, models: pair, key: Date.now() });
      return;
    }
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
            if (toolCallsRef.current.length) {
              setToolCallsByMessageId((prev) => ({
                ...prev,
                [assistantMessage.id]: toolCallsRef.current,
              }));
            }
            toolCallsRef.current = [];
            setToolCalls([]);
            setStreamingText(null);
          },
          onError: (err) => {
            setStreamError(err);
            setStreamingText(null);
          },
          onSuggestions: (suggestions) => setFollowUps(suggestions),
          onToolCall: (tool, arg) => {
            toolCallsRef.current = [...toolCallsRef.current, { tool, arg }];
            setToolCalls(toolCallsRef.current);
          },
          onToolResult: (_tool, _arg, result) => {
            const next = [...toolCallsRef.current];
            const last = next.length - 1;
            if (last >= 0) next[last] = { ...next[last], result };
            toolCallsRef.current = next;
            setToolCalls(next);
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

  // The user closed one of the two answers and the other has finished: it joins
  // the conversation like any single-model reply, and the panel goes back to normal.
  function settleCompare(outcome: BestOfTwoOutcome<ChatMessage>) {
    if (outcome.message) {
      const kept = outcome.message;
      queryClient.setQueryData<ChatMessage[]>(['thread-chat', threadId], (prev) => [...(prev ?? []), kept]);
    }
    if (outcome.toolCalls.length && outcome.message) {
      const kept = outcome.message;
      setToolCallsByMessageId((prev) => ({ ...prev, [kept.id]: outcome.toolCalls }));
    }
    if (outcome.error) setStreamError(outcome.error);
    setFollowUps(outcome.suggestions);
    setCompare(null);
    setCompareBusy(false);
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
      ref={panelRef}
      className={cn(
        'fixed z-30 flex flex-col overflow-hidden border-border bg-surface',
        chatWidth.isDragging
          ? 'transition-none select-none'
          : 'transition-all duration-slow ease-out motion-reduce:transition-none',
        expanded
          ? // Docked below the app header (h-14), not over it, so the nav stays reachable.
            'inset-x-0 top-14 bottom-0 border-l shadow-xl sm:inset-x-auto sm:right-0 sm:w-[var(--chat-panel-width,28rem)] sm:min-w-[28rem] sm:max-w-[calc(100vw-3rem)]'
          : 'bottom-4 right-4 w-64 rounded-full border shadow-lg motion-safe:animate-glow sm:w-80',
        // Two answers side by side need more than the single-answer minimum.
        expanded && compare && 'sm:min-w-[44rem]',
      )}
      style={{
        ...(!expanded ? { paddingBottom: 'env(safe-area-inset-bottom)' } : {}),
        ...(expanded ? ({ '--chat-panel-width': `${chatWidth.width}px` } as React.CSSProperties) : {}),
      }}
    >
      {expanded && <ChatPanelResizeHandle chatWidth={chatWidth} />}
      {expanded && (
        <div className="border-b border-border px-4 py-2.5">
          <div className="flex items-center gap-2">
            <Sparkles className="size-4 shrink-0 text-primary" aria-hidden />
            <h2 className="flex-1 text-sm font-semibold">Ask about this thread</h2>
            {messages.length > 0 && (
              <button
                type="button"
                onClick={() => setConfirmClearOpen(true)}
                disabled={streamingText !== null || compare !== null || clear.isPending}
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

          <ChatModelPicker chatModel={chatModel} disabled={compare !== null} />
        </div>
      )}

      {expanded && (
        <div
          ref={scrollContainerRef}
          onScroll={handleScroll}
          className="flex-1 space-y-3 overflow-y-auto p-4"
        >
          {messagesQuery.isLoading && (
            <p className="text-sm text-fg-subtle">Loading conversation…</p>
          )}

          {!messagesQuery.isLoading && messages.length === 0 && streamingText === null && !compare && (
            <>
              <p className="text-sm text-fg-subtle">
                Ask about the meetings, calendar events, emails and notes on this thread —
                decisions made, action items, who owns what, or a specific meeting's transcript.
                Any answer can be copied or kept as a note.
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                {THREAD_STARTER_PROMPTS.map((prompt) => (
                  <button
                    key={prompt}
                    type="button"
                    onClick={() => void submit(prompt)}
                    className="rounded-full border border-border bg-transparent px-3 py-1 text-sm text-fg-faint transition-colors duration-fast hover:border-border-strong hover:bg-surface hover:text-fg"
                  >
                    {prompt}
                  </button>
                ))}
              </div>
            </>
          )}

          {messages.map((message, i) => (
            <Fragment key={message.id}>
              {message.role === 'assistant' &&
                (toolCallsByMessageId[message.id] ?? message.tool_calls)?.map((call, j) => (
                  <div key={j} className="flex justify-start">
                    <ToolCallBubble call={call} />
                  </div>
                ))}
              <div className={cn('flex', message.role === 'user' ? 'justify-end' : 'justify-start')}>
                <MessageBubble
                  role={message.role}
                  content={message.content}
                  scope={noteScope}
                  // The turn this answered, for the generated note title.
                  question={messages[i - 1]?.role === 'user' ? messages[i - 1].content : undefined}
                  model={message.model}
                  promptTokens={message.prompt_tokens}
                  completionTokens={message.completion_tokens}
                />
              </div>
            </Fragment>
          ))}

          {streamingText === null && messages.at(-1)?.role === 'assistant' && (
            <FollowUpChips suggestions={followUps} onPick={(text) => void submit(text)} />
          )}

          {/* No scope while the reply is still arriving: there is nothing to
              copy or file until it is finished and saved. */}
          {streamingText !== null && (
            <>
              {toolCalls.map((call, i) => (
                <div key={i} className="flex justify-start">
                  <ToolCallBubble call={call} />
                </div>
              ))}
              <div className="flex justify-start">
                {streamingText === '' ? (
                  <ThinkingBubble />
                ) : (
                  <MessageBubble role="assistant" content={streamingText} isStreaming />
                )}
              </div>
            </>
          )}

          {compare && (
            <BestOfTwoAnswers<ChatMessage>
              key={compare.key}
              path={`/threads/${threadId}/chat`}
              message={compare.message}
              models={compare.models}
              modelName={(id) => chatModel.options.find((o) => o.id === id)?.name ?? id}
              discardPath={(id) => `/threads/${threadId}/chat/messages/${id}`}
              bubbleProps={{ scope: noteScope, question: compare.message }}
              onChoose={chatModel.setModel}
              onSettled={settleCompare}
              onBusyChange={setCompareBusy}
            />
          )}

          <div ref={bottomRef} />
          {!pinned && (
            <button
              type="button"
              className="jump-latest"
              onClick={() => {
                setPinned(true);
                bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
              }}
            >
              ↓ Latest
            </button>
          )}
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
              if (e.nativeEvent.isComposing || e.keyCode === 229) return;
              if (e.key === 'ArrowUp' && !draft) {
                const lastUser = [...messages].reverse().find((m) => m.role === 'user');
                if (lastUser) {
                  e.preventDefault();
                  setDraft(lastUser.content);
                  return;
                }
              }
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
            streamingText !== null || compareBusy ? (
              <Button
                size="icon"
                variant="secondary"
                aria-label="Stop generating"
                title="Stop generating"
                onClick={cancelStream}
              >
                <Square className="size-3.5 fill-current" />
              </Button>
            ) : (
              <Button
                size="icon"
                variant="primary"
                aria-label="Send"
                disabled={!draft.trim() || compare !== null}
                onClick={() => void submit()}
              >
                <Send />
              </Button>
            )
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

      <ConfirmDialog
        open={confirmClearOpen}
        onOpenChange={setConfirmClearOpen}
        title="Clear conversation?"
        description="Clear this conversation? This cannot be undone."
        confirmLabel="Clear"
        variant="danger"
        loading={clear.isPending}
        onConfirm={() => {
          clear.mutate(undefined, {
            onSettled: () => setConfirmClearOpen(false),
          });
        }}
      />
    </div>
  );
}
