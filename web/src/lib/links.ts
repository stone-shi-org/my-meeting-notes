/** Gmail has no per-message web URL in the MCP search result, but the RFC 2822
 * Message-ID header it does return resolves to the right thread via Gmail's
 * own search operator -- no server-side change needed. */
export function gmailLink(messageId: string | null | undefined): string | null {
  if (!messageId) return null;
  const bare = messageId.replace(/^<|>$/g, '');
  return `https://mail.google.com/mail/u/0/#search/rfc822msgid:${encodeURIComponent(bare)}`;
}

/** Providers that have a web UI supply their own `url`. Only fall back to the
 * Gmail search link for accounts actually backed by Gmail -- pointing an iCloud
 * or Zoho message at mail.google.com would open a search that finds nothing. */
export function emailLink(email: {
  url?: string | null;
  provider?: string | null;
  rfc_message_id?: string | null;
  message_id?: string | null;
}): string | null {
  if (email.url) return email.url;
  const gmailBacked = !email.provider || ['google', 'mcp_email'].includes(email.provider);
  if (!gmailBacked) return null;
  return gmailLink(email.rfc_message_id || email.message_id);
}
