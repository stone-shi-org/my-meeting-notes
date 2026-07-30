import DOMPurify from 'dompurify';
import { marked } from 'marked';

/** LLM-authored markdown (meeting summaries, chat replies) rendered as sanitized HTML. */
export function renderMarkdown(text: string): string {
  if (!text) return '';
  return DOMPurify.sanitize(marked.parse(text, { async: false }) as string);
}
