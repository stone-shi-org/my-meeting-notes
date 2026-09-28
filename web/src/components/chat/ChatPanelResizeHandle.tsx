import type { ChatPanelWidthResult } from '@/hooks/useChatPanelWidth';
import { cn } from '@/lib/cn';

interface ChatPanelResizeHandleProps {
  chatWidth: ChatPanelWidthResult;
  className?: string;
}

/**
 * Visual and interactive drag handle placed along the left edge of an expanded
 * right-docked chat panel. Enables dragging to resize the panel wider or narrower.
 */
export function ChatPanelResizeHandle({ chatWidth, className }: ChatPanelResizeHandleProps) {
  return (
    <div
      {...chatWidth.handleProps}
      title="Drag to resize side panel (double-click to reset)"
      className={cn(
        'group/resize absolute left-0 top-0 bottom-0 z-20 hidden w-2 sm:flex cursor-col-resize touch-none select-none items-center justify-center transition-colors',
        'hover:bg-primary/10 active:bg-primary/20',
        chatWidth.isDragging && 'bg-primary/20 cursor-col-resize',
        className,
      )}
    >
      <div
        className={cn(
          'h-10 w-0.5 rounded-full bg-border-strong transition-all duration-fast',
          'group-hover/resize:h-12 group-hover/resize:w-1 group-hover/resize:bg-primary',
          'group-focus-visible/resize:h-12 group-focus-visible/resize:w-1 group-focus-visible/resize:bg-primary group-focus-visible/resize:ring-2 group-focus-visible/resize:ring-primary',
          chatWidth.isDragging && 'h-14 w-1 bg-primary',
        )}
        aria-hidden
      />
    </div>
  );
}
