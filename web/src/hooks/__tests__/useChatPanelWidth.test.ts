import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import {
  CHAT_PANEL_WIDTH_KEY,
  DEFAULT_CHAT_PANEL_WIDTH,
  MAX_CHAT_PANEL_WIDTH,
  MIN_CHAT_PANEL_WIDTH,
  useChatPanelWidth,
} from '../useChatPanelWidth';

describe('useChatPanelWidth', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('defaults to 448px (28rem) when no localStorage value exists', () => {
    const { result } = renderHook(() => useChatPanelWidth());
    expect(result.current.width).toBe(DEFAULT_CHAT_PANEL_WIDTH);
  });

  it('restores stored width from localStorage', () => {
    window.localStorage.setItem(CHAT_PANEL_WIDTH_KEY, '640');
    const { result } = renderHook(() => useChatPanelWidth());
    expect(result.current.width).toBe(640);
  });

  it('clamps stored width if below minimum or above maximum', () => {
    window.localStorage.setItem(CHAT_PANEL_WIDTH_KEY, '200');
    const { result: lowResult } = renderHook(() => useChatPanelWidth());
    expect(lowResult.current.width).toBe(DEFAULT_CHAT_PANEL_WIDTH);

    window.localStorage.setItem(CHAT_PANEL_WIDTH_KEY, '99999');
    const { result: highResult } = renderHook(() => useChatPanelWidth());
    expect(highResult.current.width).toBe(MAX_CHAT_PANEL_WIDTH);
  });

  it('allows manual setWidth and persists to localStorage', () => {
    const { result } = renderHook(() => useChatPanelWidth());

    act(() => {
      result.current.setWidth(600);
    });

    expect(result.current.width).toBe(600);
    expect(window.localStorage.getItem(CHAT_PANEL_WIDTH_KEY)).toBe('600');
  });

  it('resets width to default on resetWidth', () => {
    window.localStorage.setItem(CHAT_PANEL_WIDTH_KEY, '700');
    const { result } = renderHook(() => useChatPanelWidth());

    act(() => {
      result.current.resetWidth();
    });

    expect(result.current.width).toBe(DEFAULT_CHAT_PANEL_WIDTH);
    expect(window.localStorage.getItem(CHAT_PANEL_WIDTH_KEY)).toBe(String(DEFAULT_CHAT_PANEL_WIDTH));
  });

  it('supports keyboard navigation via handleProps.onKeyDown', () => {
    const { result } = renderHook(() => useChatPanelWidth());

    // ArrowLeft widens the right-docked panel
    act(() => {
      result.current.handleProps.onKeyDown({
        key: 'ArrowLeft',
        preventDefault: () => {},
        shiftKey: false,
      } as React.KeyboardEvent);
    });
    expect(result.current.width).toBe(DEFAULT_CHAT_PANEL_WIDTH + 24);

    // ArrowRight narrows it
    act(() => {
      result.current.handleProps.onKeyDown({
        key: 'ArrowRight',
        preventDefault: () => {},
        shiftKey: false,
      } as React.KeyboardEvent);
    });
    expect(result.current.width).toBe(DEFAULT_CHAT_PANEL_WIDTH);

    // Cannot narrow past MIN_CHAT_PANEL_WIDTH
    act(() => {
      result.current.handleProps.onKeyDown({
        key: 'ArrowRight',
        preventDefault: () => {},
        shiftKey: false,
      } as React.KeyboardEvent);
    });
    expect(result.current.width).toBe(MIN_CHAT_PANEL_WIDTH);

    // Enter toggles wide mode
    act(() => {
      result.current.handleProps.onKeyDown({
        key: 'Enter',
        preventDefault: () => {},
        shiftKey: false,
      } as React.KeyboardEvent);
    });
    expect(result.current.width).toBe(720);

    // Home resets to default
    act(() => {
      result.current.handleProps.onKeyDown({
        key: 'Home',
        preventDefault: () => {},
        shiftKey: false,
      } as React.KeyboardEvent);
    });
    expect(result.current.width).toBe(DEFAULT_CHAT_PANEL_WIDTH);
  });

  it('resets on double click', () => {
    window.localStorage.setItem(CHAT_PANEL_WIDTH_KEY, '800');
    const { result } = renderHook(() => useChatPanelWidth());
    expect(result.current.width).toBe(800);

    act(() => {
      result.current.handleProps.onDoubleClick();
    });

    expect(result.current.width).toBe(DEFAULT_CHAT_PANEL_WIDTH);
  });
});
