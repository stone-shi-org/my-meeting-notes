import { useCallback, useEffect, useRef, useState } from 'react';

export const CHAT_PANEL_WIDTH_KEY = 'mmn.chatPanelWidth';
export const DEFAULT_CHAT_PANEL_WIDTH = 448; // 28rem
export const MIN_CHAT_PANEL_WIDTH = 448; // 28rem
export const MAX_CHAT_PANEL_WIDTH = 1120; // 70rem

function getStoredWidth(): number {
  if (typeof window === 'undefined') return DEFAULT_CHAT_PANEL_WIDTH;
  try {
    const raw = window.localStorage.getItem(CHAT_PANEL_WIDTH_KEY);
    if (!raw) return DEFAULT_CHAT_PANEL_WIDTH;
    const parsed = parseInt(raw, 10);
    if (Number.isFinite(parsed) && parsed >= MIN_CHAT_PANEL_WIDTH) {
      return Math.min(parsed, MAX_CHAT_PANEL_WIDTH);
    }
  } catch {
    // localStorage can fail in sandboxed iframes or private modes
  }
  return DEFAULT_CHAT_PANEL_WIDTH;
}

function persistWidth(val: number) {
  try {
    window.localStorage.setItem(CHAT_PANEL_WIDTH_KEY, String(val));
  } catch {
    // Ignore storage write failures
  }
}

export interface ChatPanelWidthResult {
  width: number;
  isDragging: boolean;
  minWidth: number;
  maxWidth: number;
  isDraggingRef: React.MutableRefObject<boolean>;
  resetWidth: () => void;
  setWidth: (width: number) => void;
  handleProps: {
    onPointerDown: (e: React.PointerEvent) => void;
    onDoubleClick: () => void;
    onKeyDown: (e: React.KeyboardEvent) => void;
    role: 'separator';
    'aria-orientation': 'vertical';
    'aria-label': string;
    'aria-valuenow': number;
    'aria-valuemin': number;
    'aria-valuemax': number;
    tabIndex: number;
  };
}

/**
 * Manages resizable width for side chat panels with persistence across sessions
 * and keyboard/pointer support.
 */
export function useChatPanelWidth(): ChatPanelWidthResult {
  const [width, setWidthState] = useState<number>(getStoredWidth);
  const [isDragging, setIsDragging] = useState(false);
  const isDraggingRef = useRef(false);
  const currentWidthRef = useRef(width);
  currentWidthRef.current = width;

  const clampWidth = useCallback((targetWidth: number): number => {
    const maxAllowed =
      typeof window !== 'undefined'
        ? Math.max(MIN_CHAT_PANEL_WIDTH, Math.min(window.innerWidth - 64, MAX_CHAT_PANEL_WIDTH))
        : MAX_CHAT_PANEL_WIDTH;
    return Math.min(Math.max(targetWidth, MIN_CHAT_PANEL_WIDTH), maxAllowed);
  }, []);

  const setWidth = useCallback(
    (newVal: number) => {
      const clamped = clampWidth(newVal);
      setWidthState(clamped);
      currentWidthRef.current = clamped;
      persistWidth(clamped);
    },
    [clampWidth],
  );

  const resetWidth = useCallback(() => {
    setWidth(DEFAULT_CHAT_PANEL_WIDTH);
  }, [setWidth]);

  // Ensure width remains clamped if window is resized smaller than current width
  useEffect(() => {
    function handleWindowResize() {
      const clamped = clampWidth(currentWidthRef.current);
      if (clamped !== currentWidthRef.current) {
        setWidthState(clamped);
        currentWidthRef.current = clamped;
      }
    }
    window.addEventListener('resize', handleWindowResize);
    return () => window.removeEventListener('resize', handleWindowResize);
  }, [clampWidth]);

  const handlePointerDown = useCallback(
    (e: React.PointerEvent) => {
      if (e.button !== 0) return; // Only primary button
      e.preventDefault();

      isDraggingRef.current = true;
      setIsDragging(true);

      const startX = e.clientX;
      const startWidth = currentWidthRef.current;

      function onPointerMove(moveEvent: PointerEvent) {
        if (!isDraggingRef.current) return;
        // Right-docked panel: moving pointer left increases panel width
        const delta = startX - moveEvent.clientX;
        const target = startWidth + delta;
        const clamped = clampWidth(target);
        setWidthState(clamped);
        currentWidthRef.current = clamped;
      }

      function onPointerUp() {
        if (!isDraggingRef.current) return;
        isDraggingRef.current = false;
        setIsDragging(false);
        persistWidth(currentWidthRef.current);
        window.removeEventListener('pointermove', onPointerMove);
        window.removeEventListener('pointerup', onPointerUp);
        window.removeEventListener('pointercancel', onPointerUp);
      }

      window.addEventListener('pointermove', onPointerMove);
      window.addEventListener('pointerup', onPointerUp);
      window.addEventListener('pointercancel', onPointerUp);
    },
    [clampWidth],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      const step = e.shiftKey ? 48 : 24;
      if (e.key === 'ArrowLeft') {
        // Dragging left widens the right-docked panel
        e.preventDefault();
        setWidth(currentWidthRef.current + step);
      } else if (e.key === 'ArrowRight') {
        // Dragging right narrows the right-docked panel
        e.preventDefault();
        setWidth(currentWidthRef.current - step);
      } else if (e.key === 'Home') {
        e.preventDefault();
        resetWidth();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        // Toggle between default (448px) and wide (720px)
        if (currentWidthRef.current > DEFAULT_CHAT_PANEL_WIDTH + 50) {
          resetWidth();
        } else {
          setWidth(720);
        }
      }
    },
    [resetWidth, setWidth],
  );

  const maxAllowed =
    typeof window !== 'undefined'
      ? Math.max(MIN_CHAT_PANEL_WIDTH, Math.min(window.innerWidth - 64, MAX_CHAT_PANEL_WIDTH))
      : MAX_CHAT_PANEL_WIDTH;

  return {
    width,
    isDragging,
    minWidth: MIN_CHAT_PANEL_WIDTH,
    maxWidth: maxAllowed,
    isDraggingRef,
    resetWidth,
    setWidth,
    handleProps: {
      onPointerDown: handlePointerDown,
      onDoubleClick: resetWidth,
      onKeyDown: handleKeyDown,
      role: 'separator',
      'aria-orientation': 'vertical',
      'aria-label': 'Resize side panel',
      'aria-valuenow': width,
      'aria-valuemin': MIN_CHAT_PANEL_WIDTH,
      'aria-valuemax': maxAllowed,
      tabIndex: 0,
    },
  };
}
