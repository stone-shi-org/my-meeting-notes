import { describe, expect, it } from 'vitest';
import { quotedLineCount, splitQuoted } from '@/lib/emailBody';

/** Padding, so a case is past the "too short to bother folding" threshold. */
const PAD = Array.from({ length: 12 }, (_, i) => `Line ${i} of my actual reply text.`).join('\n');

function parts(body: string) {
  return splitQuoted(body);
}

describe('splitQuoted — what must fold', () => {
  it('folds a single-line Gmail attribution', () => {
    const p = parts(`${PAD}\n\nOn Tue, 25 Aug 2026 at 09:14, Priya <p@x.com> wrote:\n> the original\n> continued`);
    expect(p.foldedBy).toBe('attribution');
    expect(p.reply).toContain('Line 0');
    expect(p.quoted).toContain('On Tue, 25 Aug 2026');
    expect(p.quoted).toContain('the original');
  });

  it('folds the wrapped two-line attribution Gmail also emits', () => {
    const p = parts(`${PAD}\n\nOn Tue, 25 Aug 2026 at 09:14, Priya Raman <priya@x.com>\nwrote:\n> the original`);
    expect(p.foldedBy).toBe('attribution');
    expect(p.reply).not.toContain('Priya Raman');
  });

  it('folds a run of quote-prefixed lines', () => {
    const p = parts(`${PAD}\n\n> first quoted line\n> second quoted line\n> third`);
    expect(p.foldedBy).toBe('quote-prefix');
    expect(p.quoted.split('\n').filter(Boolean)).toHaveLength(3);
  });

  it('folds an attribution together with the quote below it', () => {
    // The attribution must not dangle above the fold.
    const p = parts(`${PAD}\n\nOn Tuesday, Priya said something\n> quoted one\n> quoted two`);
    expect(p.quoted).toContain('On Tuesday');
  });

  it('folds an Original Message separator', () => {
    const p = parts(`${PAD}\n\n-----Original Message-----\nFrom: someone\nold content`);
    expect(p.foldedBy).toBe('original-message');
    expect(p.quoted).toContain('Original Message');
  });

  it('folds a Gmail forwarded-message banner whatever its dash count', () => {
    for (const dashes of ['--', '-----', '----------']) {
      const p = parts(`${PAD}\n\n${dashes} Forwarded message ${dashes}\nFrom: a@b`);
      expect(p.foldedBy).toBe('original-message');
    }
  });

  it('folds an Outlook header block', () => {
    const p = parts(
      `${PAD}\n\nFrom: Priya Raman\nSent: Tuesday, 25 August 2026 09:14\nTo: Me\nSubject: Atlas\n\nold content`,
    );
    expect(p.foldedBy).toBe('outlook-header');
    expect(p.quoted).toContain('Sent:');
  });

  it('folds at the earliest marker when several are present', () => {
    const p = parts(`${PAD}\n\n> an early quote\n> still quoting\n\n-----Original Message-----\nolder`);
    expect(p.foldedBy).toBe('quote-prefix');
    expect(p.quoted).toContain('Original Message');
  });
});

describe('splitQuoted — what must NOT fold', () => {
  it('leaves a lone rhetorical quote alone', () => {
    // One `>` line is a person quoting a phrase mid-sentence, not a reply chain.
    const p = parts(`${PAD}\n\n> just this one phrase\n\nand my point continues here at length.`);
    expect(p.foldedBy).toBeNull();
    expect(p.quoted).toBe('');
  });

  it('leaves a bare rule of dashes alone', () => {
    // People type these above their own sign-off.
    const p = parts(`${PAD}\n\n-----\n\nMore of my own writing follows here.`);
    expect(p.foldedBy).toBeNull();
  });

  it('leaves a sentence beginning "From:" alone', () => {
    // The companion-header requirement is what makes this safe.
    const p = parts(`${PAD}\n\nFrom: the design review, here is what actually changed.\n\nMore text.`);
    expect(p.foldedBy).toBeNull();
  });

  it('leaves quote characters inside a fenced code block alone', () => {
    // The case that will actually bite: GitHub and CI notification mail.
    const p = parts(
      `${PAD}\n\n\`\`\`\n> git log --oneline\n> git diff HEAD~1\n> make test\n\`\`\`\n\nThat is the command I ran.`,
    );
    expect(p.foldedBy).toBeNull();
    expect(p.reply).toContain('git diff');
  });

  it('leaves quote characters inside an indented code block alone', () => {
    const p = parts(`${PAD}\n\n    > indented output line\n    > another one\n\nEnd of my message.`);
    expect(p.foldedBy).toBeNull();
    expect(p.reply).toContain('indented output');
  });

  it('does not fold a short body even when it looks quotable', () => {
    const p = parts('Sounds good.\n\nOn Tue, Priya wrote:\n> the original');
    expect(p.foldedBy).toBeNull();
    expect(p.reply).toContain('the original');
  });

  it('returns the whole body when it is entirely quoted', () => {
    // Otherwise the UI would have nothing left to show.
    const body = `> everything here is quoted\n> line two\n> line three\n> line four\n> line five\n> six\n> seven\n> eight\n> nine`;
    const p = parts(body);
    expect(p.foldedBy).toBeNull();
    expect(p.reply).toBe(body);
  });

  it('handles empty and whitespace-only input', () => {
    for (const raw of ['', '   ', '\n\n', null, undefined]) {
      const p = splitQuoted(raw as string);
      expect(p.foldedBy).toBeNull();
      expect(p.quoted).toBe('');
    }
  });
});

describe('splitQuoted — signatures', () => {
  it('splits a "-- " delimited signature off but does not discard it', () => {
    const p = parts(`${PAD}\n\n-- \nPriya Raman\n+44 7700 900000`);
    expect(p.signature).toContain('+44 7700 900000');
    expect(p.reply).not.toContain('+44 7700 900000');
  });

  it('tolerates a trailing-space-stripped delimiter', () => {
    const p = parts(`${PAD}\n\n--\nPriya Raman`);
    expect(p.signature).toContain('Priya Raman');
  });

  it('takes the last delimiter, not the first', () => {
    const p = parts(`${PAD}\n\n--\nnot really\n\nmore text\n\n--\nPriya Raman`);
    expect(p.signature).toContain('Priya Raman');
    expect(p.reply).toContain('not really');
  });

  it('ignores a delimiter with too much text after it', () => {
    // That is a horizontal rule, not a signature.
    const tail = Array.from({ length: 12 }, (_, i) => `still writing ${i}`).join('\n');
    const p = parts(`${PAD}\n\n--\n${tail}`);
    expect(p.signature).toBe('');
  });
});

describe('splitQuoted — the safety property', () => {
  const CASES = [
    `${PAD}\n\nOn Tue, Priya <p@x.com> wrote:\n> quoted\n> more`,
    `${PAD}\n\n> a\n> b\n\n-- \nSig here`,
    `${PAD}\n\n-----Original Message-----\nFrom: a@b\nolder text`,
    `${PAD}\n\nFrom: X\nSent: Y\nTo: Z\nSubject: W\n\nbody`,
    `${PAD}\n\n\`\`\`\n> code\n> more code\n\`\`\`\nend`,
    'Sounds good.',
    '> all quoted\n> every line\n> of it\n> here\n> and\n> here\n> and\n> here\n> too',
    `${PAD}\n\n-- \nName\nPhone`,
    '',
  ];

  it.each(CASES)('reconstitutes the input exactly (case %#)', (body) => {
    // The single assertion that makes this file safe to keep tuning: whatever
    // the detectors do, no character is ever eaten.
    const p = splitQuoted(body);
    const rebuilt = [p.reply, p.signature].filter((s) => s !== '').join('\n') + p.quoted;
    const normalise = (s: string) => s.replace(/\r\n/g, '\n').replace(/\s+/g, ' ').trim();
    expect(normalise(rebuilt)).toBe(normalise(body));
  });
});

describe('quotedLineCount', () => {
  it('counts only non-blank folded lines, for the disclosure label', () => {
    // A screen-reader user cannot see what is behind a "…", so the button says
    // how much.
    const p = parts(`${PAD}\n\n> one\n\n> two\n\n> three`);
    expect(quotedLineCount(p)).toBe(3);
  });

  it('is zero when nothing folded', () => {
    expect(quotedLineCount(splitQuoted('Sounds good.'))).toBe(0);
  });
});
