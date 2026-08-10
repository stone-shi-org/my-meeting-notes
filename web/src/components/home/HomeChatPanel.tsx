import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Minimize2, Send, Sparkles, Trash2 } from 'lucide-react';
import { Fragment, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { FollowUpChips } from '@/components/chat/FollowUpChips';
import { MessageBubble, ThinkingBubble } from '@/components/chat/MessageBubble';
import { ToolCallBubble, type ToolCall } from '@/components/chat/ToolCallBubble';
import { Button } from '@/components/ui/Button';
import { Select, Textarea } from '@/components/ui/primitives';
import { useChatModel } from '@/hooks/useChatModel';
import { api } from '@/lib/api';
import { streamChat } from '@/lib/chatStream';
import { cn } from '@/lib/cn';
import { ApiError, type HomeChatMessage } from '@/types/api';

const HOME_STARTER_PROMPTS = [
  'What needs my attention?',
  "What's on my calendar this week?",
  "Which threads haven't moved in a while?",
  'Summarize my open threads',
];

/**
 * Always-on assistant for asking questions across every thread on the home
 * screen, not just one -- same docked-pill-expands-to-panel shape as
 * ThreadChatPanel, adapted for a cross-thread digest instead of a single
 * thread's own.
 */
export function HomeChatPanel() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const [expanded, setExpanded] = useState(false);
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
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const chatModel = useChatModel();

  const messagesQuery = useQuery({
    queryKey: ['home-chat'],
    queryFn: () => api.get<HomeChatMessage[]>('/home/chat'),
  });

  const messages = messagesQuery.data ?? [];

  const clear = useMutation({
    mutationFn: () => api.del('/home/chat'),
    onSuccess: () => {
      queryClient.setQueryData<HomeChatMessage[]>(['home-chat'], []);
      setStreamError(null);
      setFollowUps([]);
      setToolCallsByMessageId({});
    },
  });

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [messages.length, streamingText, expanded, followUps, toolCalls]);

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
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        minimize();
      }
    }
    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [expanded]);

  async function submit(overrideMessage?: string) {
    const message = (overrideMessage ?? draft).trim();
    if (!message || streamingText !== null) return;
    setDraft('');
    setStreamError(null);
    setFollowUps([]);
    toolCallsRef.current = [];
    setToolCalls([]);

    const optimisticUser: HomeChatMessage = {
      id: -Date.now(),
      role: 'user',
      content: message,
      model: null,
      prompt_tokens: null,
      completion_tokens: null,
      created_at: new Date().toISOString(),
    };
    queryClient.setQueryData<HomeChatMessage[]>(['home-chat'], (prev) => [
      ...(prev ?? []),
      optimisticUser,
    ]);
    setStreamingText('');

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await streamChat<HomeChatMessage>(
        '/home/chat',
        message,
        {
          onToken: (text) => setStreamingText((prev) => (prev ?? '') + text),
          onDone: (assistantMessage) => {
            queryClient.setQueryData<HomeChatMessage[]>(['home-chat'], (prev) => [
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
            <h2 className="flex-1 text-sm font-semibold">Ask about your threads</h2>
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
            <>
              <p className="text-sm text-fg-subtle">
                Ask about anything across your threads — what needs attention, what's coming up on
                your calendar, or a specific thread's own meetings and decisions. Any answer can be
                copied or kept as a note.
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                {HOME_STARTER_PROMPTS.map((prompt) => (
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
                toolCallsByMessageId[message.id]?.map((call, j) => (
                  <div key={j} className="flex justify-start">
                    <ToolCallBubble call={call} />
                  </div>
                ))}
              <div className={cn('flex', message.role === 'user' ? 'justify-end' : 'justify-start')}>
                <MessageBubble
                  role={message.role}
                  content={message.content}
                  pickThread
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

          {/* No note/copy actions while the reply is still arriving: there is
              nothing to save yet. */}
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
                  <MessageBubble role="assistant" content={streamingText} />
                )}
              </div>
            </>
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
            placeholder="Ask about your threads…"
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
              title="Ask about your threads"
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
