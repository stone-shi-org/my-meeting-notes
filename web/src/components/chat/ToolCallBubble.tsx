import { ChevronDown, ChevronRight } from 'lucide-react';
import { useState } from 'react';
import { Spinner } from '@/components/ui/primitives';

export interface ToolCall {
  tool: string;
  arg: string;
  /** Undefined while the tool is still running. */
  result?: string;
}

/**
 * One tool hop inside a thread chat turn -- ThreadChatPanel's tool-hop loop
 * is the only chat that has these (meeting chat answers from a transcript
 * alone, with no on-demand tool). Shown as a "Calling x..." spinner while
 * the hop is in flight, then collapses into a row a user can expand to see
 * what was asked and what came back.
 */
export function ToolCallBubble({ call }: { call: ToolCall }) {
  const [expanded, setExpanded] = useState(false);

  if (call.result === undefined) {
    return (
      <p className="max-w-[85%] rounded-lg bg-surface-2 px-3 py-2 text-sm text-fg-subtle">
        <span className="inline-flex items-center gap-2">
          <Spinner className="size-3.5" />
          Calling {call.tool}…
        </span>
      </p>
    );
  }

  return (
    <div className="max-w-[85%] min-w-0 rounded-lg bg-surface-2 px-3 py-2 text-sm">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-1.5 text-left text-fg-subtle hover:text-fg"
      >
        {expanded ? (
          <ChevronDown className="size-3.5 shrink-0" aria-hidden />
        ) : (
          <ChevronRight className="size-3.5 shrink-0" aria-hidden />
        )}
        Called {call.tool}
      </button>

      {expanded && (
        <div className="mt-2 space-y-1.5 border-t border-border pt-2 text-2xs text-fg-subtle">
          <p>
            <span className="font-semibold">Request: </span>
            {call.tool} {call.arg}
          </p>
          <div>
            <span className="font-semibold">Result:</span>
            <pre className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap break-words rounded bg-surface px-2 py-1.5">
              {call.result}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}
