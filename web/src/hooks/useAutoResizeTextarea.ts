import { useLayoutEffect } from 'react';

/**
 * Grows a textarea to fit pasted or shift+enter'd multi-line text, the same
 * feel as ChatGPT's composer -- a plain `<textarea>` does not do this on its
 * own. Resets to `'auto'` before measuring so deleting text shrinks the box
 * back down instead of it only ever growing. The visible cap is left to the
 * caller's own `max-h-*` class: it clips the rendered box regardless of this
 * inline height, and a textarea scrolls past it by default.
 */
export function useAutoResizeTextarea(
  ref: React.RefObject<HTMLTextAreaElement | null>,
  value: string,
) {
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  }, [ref, value]);
}
