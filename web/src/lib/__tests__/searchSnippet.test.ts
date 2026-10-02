import { render } from '@testing-library/react';
import { createElement } from 'react';
import { describe, expect, it } from 'vitest';
import { parseSnippet, renderSnippet, stripSentinels } from '../searchSnippet';

const O = '\u0002';
const C = '\u0003';

describe('parseSnippet', () => {
  it('splits plain and highlighted runs', () => {
    expect(parseSnippet(`we cut the ${O}budget${C} for Q3`)).toEqual([
      { text: 'we cut the ', mark: false },
      { text: 'budget', mark: true },
      { text: ' for Q3', mark: false },
    ]);
  });

  it('handles several marks and a snippet with none', () => {
    expect(parseSnippet(`${O}a${C} b ${O}c${C}`)).toEqual([
      { text: 'a', mark: true },
      { text: ' b ', mark: false },
      { text: 'c', mark: true },
    ]);
    expect(parseSnippet('nothing to see')).toEqual([{ text: 'nothing to see', mark: false }]);
  });

  it('is empty for null, undefined and empty input', () => {
    expect(parseSnippet(null)).toEqual([]);
    expect(parseSnippet(undefined)).toEqual([]);
    expect(parseSnippet('')).toEqual([]);
  });

  it('drops a stray close and merges the text either side of it', () => {
    expect(parseSnippet(`before${C}after`)).toEqual([{ text: 'beforeafter', mark: false }]);
  });

  it('highlights to the end when an open is never closed', () => {
    // FTS5's window can cut a snippet between the open and the close.
    expect(parseSnippet(`we cut the ${O}budg`)).toEqual([
      { text: 'we cut the ', mark: false },
      { text: 'budg', mark: true },
    ]);
  });

  it('ignores a nested open', () => {
    expect(parseSnippet(`${O}a${O}b${C} c`)).toEqual([
      { text: 'ab', mark: true },
      { text: ' c', mark: false },
    ]);
  });

  it('never produces an empty mark', () => {
    expect(parseSnippet(`x ${O}${C} y`)).toEqual([{ text: 'x  y', mark: false }]);
  });
});

describe('stripSentinels', () => {
  it('removes every sentinel', () => {
    expect(stripSentinels(`${O}Priya${C} @ 12:34${C}`)).toBe('Priya @ 12:34');
    expect(stripSentinels(null)).toBe('');
  });
});

describe('renderSnippet', () => {
  it('renders marks as <mark> and keeps HTML as literal text', () => {
    const { container } = render(
      createElement('p', null, renderSnippet(`<b>x</b> and ${O}<i>y</i>${C}`)),
    );
    expect(container.querySelector('b')).toBeNull();
    expect(container.querySelector('i')).toBeNull();
    const mark = container.querySelector('mark');
    expect(mark).not.toBeNull();
    expect(mark!.textContent).toBe('<i>y</i>');
    expect(container.textContent).toBe('<b>x</b> and <i>y</i>');
  });

  it('colours the mark with an ink token', () => {
    const { container } = render(createElement('p', null, renderSnippet(`${O}a${C}`)));
    expect(container.querySelector('mark')!.className).toMatch(/text-[a-z]+-ink/);
  });
});
