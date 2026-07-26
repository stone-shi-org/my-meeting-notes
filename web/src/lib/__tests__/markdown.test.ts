import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { describe, expect, it } from 'vitest';

/** The summary body is LLM-authored and rendered as HTML, so it must be sanitized. */
function renderSummary(markdown: string): string {
  return DOMPurify.sanitize(marked.parse(markdown, { async: false }) as string);
}

describe('summary markdown rendering', () => {
  it('renders ordinary markdown', () => {
    const html = renderSummary('### Decisions\n\n- Ship on Friday\n- Rollback ready');
    expect(html).toContain('<h3>Decisions</h3>');
    expect(html).toContain('<li>Ship on Friday</li>');
  });

  it('strips script tags', () => {
    const html = renderSummary('Hello\n\n<script>alert("xss")</script>');
    expect(html).not.toContain('<script');
    expect(html).not.toContain('alert(');
  });

  it('strips inline event handlers', () => {
    const html = renderSummary('<img src="x" onerror="alert(1)">');
    expect(html).not.toContain('onerror');
  });

  it('strips javascript: URLs', () => {
    const html = renderSummary('[click me](javascript:alert(1))');
    expect(html).not.toContain('javascript:');
  });

  it('keeps safe links', () => {
    const html = renderSummary('[docs](https://example.com/spec)');
    expect(html).toContain('https://example.com/spec');
  });

  it('handles an empty summary', () => {
    expect(renderSummary('')).toBe('');
  });
});
