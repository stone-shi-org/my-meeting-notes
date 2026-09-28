import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ChatPanelResizeHandle } from '../ChatPanelResizeHandle';
import type { ChatPanelWidthResult } from '@/hooks/useChatPanelWidth';

describe('ChatPanelResizeHandle', () => {
  const dummyChatWidth: ChatPanelWidthResult = {
    width: 500,
    isDragging: false,
    minWidth: 448,
    maxWidth: 1120,
    isDraggingRef: { current: false },
    resetWidth: vi.fn(),
    setWidth: vi.fn(),
    handleProps: {
      onPointerDown: vi.fn(),
      onDoubleClick: vi.fn(),
      onKeyDown: vi.fn(),
      role: 'separator',
      'aria-orientation': 'vertical',
      'aria-label': 'Resize side panel',
      'aria-valuenow': 500,
      'aria-valuemin': 448,
      'aria-valuemax': 1120,
      tabIndex: 0,
    },
  };

  it('renders a separator with appropriate ARIA attributes and title', () => {
    render(<ChatPanelResizeHandle chatWidth={dummyChatWidth} />);
    const handle = screen.getByRole('separator', { name: /resize side panel/i });
    expect(handle).toBeInTheDocument();
    expect(handle).toHaveAttribute('aria-orientation', 'vertical');
    expect(handle).toHaveAttribute('aria-valuenow', '500');
    expect(handle).toHaveAttribute('title', expect.stringContaining('Drag to resize'));
  });

  it('reflects dragging state styles when isDragging is true', () => {
    const draggingState: ChatPanelWidthResult = {
      ...dummyChatWidth,
      isDragging: true,
    };
    const { container } = render(<ChatPanelResizeHandle chatWidth={draggingState} />);
    const handle = container.firstElementChild as HTMLElement;
    expect(handle.className).toContain('cursor-col-resize');
  });
});
