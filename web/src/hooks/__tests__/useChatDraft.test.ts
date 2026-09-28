import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { useChatDraft } from '../useChatDraft';

describe('useChatDraft', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it('initializes with empty string if no saved draft', () => {
    const { result } = renderHook(() => useChatDraft('mmn:draft:test'));
    expect(result.current[0]).toBe('');
  });

  it('restores draft from sessionStorage if present', () => {
    window.sessionStorage.setItem('mmn:draft:test', 'hello draft');
    const { result } = renderHook(() => useChatDraft('mmn:draft:test'));
    expect(result.current[0]).toBe('hello draft');
  });

  it('persists changes to sessionStorage', () => {
    const { result } = renderHook(() => useChatDraft('mmn:draft:test'));
    act(() => {
      result.current[1]('typing something...');
    });
    expect(window.sessionStorage.getItem('mmn:draft:test')).toBe('typing something...');
  });

  it('removes item from sessionStorage when draft becomes empty', () => {
    window.sessionStorage.setItem('mmn:draft:test', 'initial text');
    const { result } = renderHook(() => useChatDraft('mmn:draft:test'));

    act(() => {
      result.current[1]('');
    });
    expect(window.sessionStorage.getItem('mmn:draft:test')).toBeNull();
  });
});
