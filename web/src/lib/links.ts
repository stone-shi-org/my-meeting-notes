/** Gmail has no per-message web URL in the MCP search result, but the RFC 2822
 * Message-ID header it does return resolves to the right thread via Gmail's
 * own search operator -- no server-side change needed. */
export function gmailLink(messageId: string | null | undefined): string | null {
  if (!messageId) return null;
  const bare = messageId.replace(/^<|>$/g, '');
  return `https://mail.google.com/mail/u/0/#search/rfc822msgid:${encodeURIComponent(bare)}`;
}
