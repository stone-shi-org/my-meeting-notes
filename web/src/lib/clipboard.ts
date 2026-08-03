/**
 * Copy text to the clipboard, including where the modern API is not there.
 *
 * `navigator.clipboard` is gated on a secure context, exactly like
 * `navigator.mediaDevices` -- and this app is normally reached at
 * `http://192.168.x.x:4020`, which is not one. The whole object is *undefined*
 * there, so `navigator.clipboard.writeText(...)` throws "Cannot read properties
 * of undefined" rather than returning a rejected promise, and a naive
 * `.catch()` never runs.
 *
 * `document.execCommand('copy')` is deprecated but is not gated, and it is what
 * every browser this app supports still honours on a plain-HTTP origin. So:
 * try the real API, fall back to the old one, and report failure rather than
 * pretending. A copy button that silently does nothing is worse than one that
 * says it could not.
 */
export async function copyText(text: string): Promise<boolean> {
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Permission denied, or the document was not focused. Fall through --
      // execCommand is driven by the current selection and often still works.
    }
  }
  return legacyCopy(text);
}

function legacyCopy(text: string): boolean {
  if (typeof document === 'undefined') return false;

  const textarea = document.createElement('textarea');
  textarea.value = text;
  // Off-screen rather than hidden: `display: none` and `visibility: hidden`
  // elements cannot be selected, so the copy would silently take nothing.
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.top = '-1000px';
  textarea.style.opacity = '0';

  document.body.appendChild(textarea);
  const previous = document.activeElement as HTMLElement | null;

  try {
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    document.body.removeChild(textarea);
    // Focus went to the textarea; put it back so keyboard users are not
    // dumped at the top of the document by pressing a button.
    previous?.focus?.();
  }
}
