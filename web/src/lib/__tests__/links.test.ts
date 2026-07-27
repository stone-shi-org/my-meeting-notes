import { describe, expect, it } from 'vitest';
import { emailLink, gmailLink } from '../links';

describe('gmailLink', () => {
  it('strips the angle brackets from an RFC 2822 id', () => {
    expect(gmailLink('<abc@mail.gmail.com>')).toBe(
      'https://mail.google.com/mail/u/0/#search/rfc822msgid:abc%40mail.gmail.com',
    );
  });

  it('is null without an id', () => {
    expect(gmailLink(null)).toBeNull();
    expect(gmailLink('')).toBeNull();
  });
});

describe('emailLink', () => {
  it('prefers the provider-supplied url', () => {
    expect(
      emailLink({ url: 'https://mail.zoho.com/z#/mail/1', provider: 'zoho', message_id: 'x' }),
    ).toBe('https://mail.zoho.com/z#/mail/1');
  });

  it('falls back to a Gmail search for Gmail-backed accounts', () => {
    expect(emailLink({ provider: 'mcp_email', rfc_message_id: '<a@b>' })).toContain(
      'rfc822msgid:a%40b',
    );
    expect(emailLink({ provider: 'google', rfc_message_id: '<a@b>' })).toContain(
      'mail.google.com',
    );
  });

  it('does NOT send a non-Gmail message to a Gmail search', () => {
    // The bug this function exists to prevent: an iCloud or Zoho message linked
    // into mail.google.com opens a search that finds nothing.
    expect(emailLink({ provider: 'apple', rfc_message_id: '<a@icloud.com>' })).toBeNull();
    expect(emailLink({ provider: 'zoho', message_id: '<a@zoho.com>' })).toBeNull();
  });

  it('treats an unknown provider as Gmail-backed, matching pre-refactor rows', () => {
    // Rows attached before providers existed carry no `provider`, and every one
    // of them came from the Gmail-backed MCP server.
    expect(emailLink({ message_id: '<legacy@x>' })).toContain('mail.google.com');
  });

  it('is null when there is nothing to link to', () => {
    expect(emailLink({ provider: 'apple' })).toBeNull();
    expect(emailLink({})).toBeNull();
  });
});
