import { describe, expect, it } from 'vitest';
import { fmtClock, fmtDurationHuman, fmtElapsed, fmtRelative, fmtVtt } from '../time';

describe('fmtClock', () => {
  it('formats under an hour without an hour component', () => {
    expect(fmtClock(0)).toBe('0:00');
    expect(fmtClock(41)).toBe('0:41');
    expect(fmtClock(1334)).toBe('22:14');
  });

  it('adds hours past 3600s', () => {
    expect(fmtClock(3600)).toBe('1:00:00');
    expect(fmtClock(3787)).toBe('1:03:07');
  });

  it('rounds down like a media player', () => {
    expect(fmtClock(41.9)).toBe('0:41');
  });

  it('clamps nonsense input', () => {
    expect(fmtClock(-5)).toBe('0:00');
    expect(fmtClock(NaN)).toBe('0:00');
  });
});

describe('fmtElapsed', () => {
  it('always pads minutes so the clock does not jitter', () => {
    expect(fmtElapsed(9)).toBe('00:09');
    expect(fmtElapsed(252)).toBe('04:12');
  });

  it('adds hours when needed', () => {
    expect(fmtElapsed(3787)).toBe('1:03:07');
  });
});

describe('fmtDurationHuman', () => {
  it('handles seconds, minutes and hours', () => {
    expect(fmtDurationHuman(45)).toBe('45 sec');
    expect(fmtDurationHuman(1334)).toBe('22 min');
    expect(fmtDurationHuman(3600)).toBe('1 hr');
    expect(fmtDurationHuman(3787)).toBe('1 hr 3 min');
  });

  it('handles zero and negatives', () => {
    expect(fmtDurationHuman(0)).toBe('0 sec');
    expect(fmtDurationHuman(-1)).toBe('0 sec');
  });
});

describe('fmtRelative', () => {
  const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

  it('counts up through minutes, hours and days', () => {
    expect(fmtRelative(ago(0))).toBe('just now');
    expect(fmtRelative(ago(12 * 60_000))).toBe('12m ago');
    expect(fmtRelative(ago(3 * 3_600_000))).toBe('3h ago');
    expect(fmtRelative(ago(4 * 86_400_000))).toBe('4d ago');
  });

  it('gives the date once "N days ago" stops being useful', () => {
    const old = new Date(Date.now() - 90 * 86_400_000).toISOString();
    expect(fmtRelative(old)).toBe(new Date(old).toLocaleDateString());
  });

  it('renders a missing or unparseable stamp as a dash', () => {
    expect(fmtRelative(null)).toBe('—');
    expect(fmtRelative(undefined)).toBe('—');
    expect(fmtRelative('not a date')).toBe('—');
  });
});

describe('fmtVtt', () => {
  it('always emits hours, per the VTT spec', () => {
    expect(fmtVtt(0)).toBe('00:00:00.000');
    expect(fmtVtt(1.659999966621399)).toBe('00:00:01.660');
    expect(fmtVtt(1352.489990234375)).toBe('00:22:32.490');
  });

  it('formats past an hour', () => {
    expect(fmtVtt(3787.5)).toBe('01:03:07.500');
  });
});
