import { afterEach, describe, expect, it, vi } from 'vitest';
import { copyText } from '../clipboard';

/** Replace `navigator.clipboard`, including deleting it outright — which is
 * what a plain-HTTP origin actually looks like. */
function setClipboard(value: unknown) {
  Object.defineProperty(navigator, 'clipboard', {
    value,
    configurable: true,
    writable: true,
  });
}

function setExecCommand(fn: ((command: string) => boolean) | undefined) {
  Object.defineProperty(document, 'execCommand', {
    value: fn,
    configurable: true,
    writable: true,
  });
}

afterEach(() => {
  setClipboard(undefined);
  setExecCommand(undefined);
  vi.restoreAllMocks();
});

describe('copyText', () => {
  it('uses the Clipboard API when it is available', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard({ writeText });

    expect(await copyText('hello')).toBe(true);
    expect(writeText).toHaveBeenCalledWith('hello');
  });

  it('falls back to execCommand when navigator.clipboard is undefined', async () => {
    // The plain-HTTP LAN case: the whole object is missing, so reading
    // .writeText off it would throw rather than reject.
    setClipboard(undefined);
    const exec = vi.fn().mockReturnValue(true);
    setExecCommand(exec);

    expect(await copyText('over http')).toBe(true);
    expect(exec).toHaveBeenCalledWith('copy');
  });

  it('falls back to execCommand when writeText rejects', async () => {
    setClipboard({ writeText: vi.fn().mockRejectedValue(new Error('denied')) });
    const exec = vi.fn().mockReturnValue(true);
    setExecCommand(exec);

    expect(await copyText('denied then ok')).toBe(true);
    expect(exec).toHaveBeenCalledWith('copy');
  });

  it('reports failure rather than pretending, when neither route works', async () => {
    setClipboard(undefined);
    setExecCommand(() => false);

    expect(await copyText('nope')).toBe(false);
  });

  it('leaves no scratch element behind', async () => {
    setClipboard(undefined);
    setExecCommand(() => true);

    const before = document.body.childElementCount;
    await copyText('tidy');
    expect(document.body.childElementCount).toBe(before);
  });

  it('puts focus back where it was', async () => {
    setClipboard(undefined);
    setExecCommand(() => true);

    const button = document.createElement('button');
    document.body.appendChild(button);
    button.focus();

    await copyText('focus');
    expect(document.activeElement).toBe(button);

    document.body.removeChild(button);
  });
});
