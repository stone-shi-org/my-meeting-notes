import { describe, expect, it, vi } from 'vitest';
import * as clipboard from '@/lib/clipboard';
import { enhanceCodeBlocks, handleCodeBlockClick } from '../codeBlocks';

describe('codeBlocks helper', () => {
  it('safely handles null container', () => {
    expect(() => enhanceCodeBlocks(null)).not.toThrow();
  });

  it('safely handles click with non-matching target', () => {
    const fakeEvent = {
      target: document.createElement('div'),
    } as unknown as MouseEvent;
    expect(() => handleCodeBlockClick(fakeEvent)).not.toThrow();
  });

  it('enhances pre elements with language badge and copy button', () => {
    const container = document.createElement('div');
    container.innerHTML = '<pre><code class="language-python">print("hello")</code></pre>';
    document.body.appendChild(container);

    enhanceCodeBlocks(container);

    const wrapper = container.querySelector('.code-block-container');
    expect(wrapper).not.toBeNull();
    const lang = container.querySelector('.code-block-lang');
    expect(lang?.textContent).toBe('python');
    const btn = container.querySelector('.code-copy-btn');
    expect(btn).not.toBeNull();

    // Idempotent: re-running does not double-wrap
    enhanceCodeBlocks(container);
    expect(container.querySelectorAll('.code-block-container')).toHaveLength(1);

    document.body.removeChild(container);
  });

  it('defaults language to code when no class present', () => {
    const container = document.createElement('div');
    container.innerHTML = '<pre><code>plain text</code></pre>';
    document.body.appendChild(container);

    enhanceCodeBlocks(container);

    const lang = container.querySelector('.code-block-lang');
    expect(lang?.textContent).toBe('code');

    document.body.removeChild(container);
  });

  it('handles click on copy button and calls copyText', async () => {
    const copySpy = vi.spyOn(clipboard, 'copyText').mockResolvedValue(true);
    const container = document.createElement('div');
    container.innerHTML = '<pre><code class="language-typescript">const x = 42;</code></pre>';
    document.body.appendChild(container);

    enhanceCodeBlocks(container);

    const btn = container.querySelector<HTMLButtonElement>('.code-copy-btn')!;
    expect(btn).not.toBeNull();

    handleCodeBlockClick({ target: btn } as unknown as MouseEvent);

    expect(copySpy).toHaveBeenCalledWith('const x = 42;');
    copySpy.mockRestore();
    document.body.removeChild(container);
  });
});
