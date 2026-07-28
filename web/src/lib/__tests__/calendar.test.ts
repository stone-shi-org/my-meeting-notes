import { describe, expect, it } from 'vitest';
import {
  dayKey,
  dayLabel,
  eventDate,
  eventTimeLabel,
  groupByDay,
  isAllDay,
  localDatetimeValue,
} from '../calendar';
import type { UpcomingEvent } from '@/types/api';

function event(partial: Partial<UpcomingEvent>): UpcomingEvent {
  return {
    uid: 'u1',
    summary: 'Standup',
    description: null,
    location: null,
    start: null,
    end: null,
    attendees: [],
    calendar_name: null,
    account: null,
    type: null,
    url: null,
    source_uid: null,
    provider: null,
    integration_id: null,
    attached: null,
    ...partial,
  };
}

describe('isAllDay', () => {
  it('recognises a bare VALUE=DATE stamp', () => {
    expect(isAllDay('2026-07-30')).toBe(true);
  });

  it('does not match a timed stamp', () => {
    expect(isAllDay('2026-07-30T09:00:00+00:00')).toBe(false);
    expect(isAllDay(null)).toBe(false);
  });
});

describe('eventDate', () => {
  it('keeps an all-day event on its own day', () => {
    // The bug this exists to prevent: `new Date('2026-07-30')` is UTC midnight,
    // which renders as 29 July anywhere west of Greenwich.
    const date = eventDate('2026-07-30')!;
    expect(dayKey(date)).toBe('2026-07-30');
    expect(date.getHours()).toBe(0);
  });

  it('parses a timed stamp normally', () => {
    expect(eventDate('2026-07-30T09:00:00Z')!.toISOString()).toBe('2026-07-30T09:00:00.000Z');
  });

  it('is null for missing or unparseable input', () => {
    expect(eventDate(null)).toBeNull();
    expect(eventDate('sometime Tuesday')).toBeNull();
  });
});

describe('dayLabel', () => {
  const now = new Date(2026, 6, 28, 14, 30); // local Tue 28 Jul 2026

  it('names today and tomorrow', () => {
    expect(dayLabel('2026-07-28', now)).toBe('Today');
    expect(dayLabel('2026-07-29', now)).toBe('Tomorrow');
  });

  it('falls back to a weekday and date further out', () => {
    const label = dayLabel('2026-08-04', now);
    expect(label).not.toBe('Today');
    expect(label).toContain('4');
  });

  it('crosses a month boundary without confusing tomorrow', () => {
    expect(dayLabel('2026-08-01', new Date(2026, 6, 31, 23, 30))).toBe('Tomorrow');
  });
});

describe('eventTimeLabel', () => {
  it('says All day rather than a misleading midnight', () => {
    expect(eventTimeLabel({ start: '2026-07-30', end: '2026-07-31' })).toBe('All day');
  });

  it('renders a range when both ends are on the same day', () => {
    const label = eventTimeLabel({
      start: '2026-07-30T09:00:00',
      end: '2026-07-30T09:30:00',
    });
    expect(label).toContain('–');
  });

  it('shows only the start when the event runs past midnight', () => {
    const label = eventTimeLabel({
      start: '2026-07-30T23:00:00',
      end: '2026-07-31T01:00:00',
    });
    expect(label).not.toContain('–');
  });

  it('is empty without a start', () => {
    expect(eventTimeLabel({ start: null })).toBe('');
  });
});

describe('groupByDay', () => {
  const now = new Date(2026, 6, 28, 9, 0);

  it('buckets consecutive events of the same day together', () => {
    const days = groupByDay(
      [
        event({ uid: 'a', start: '2026-07-28T09:00:00' }),
        event({ uid: 'b', start: '2026-07-28T14:00:00' }),
        event({ uid: 'c', start: '2026-07-29T10:00:00' }),
      ],
      now,
    );

    expect(days.map((d) => d.label)).toEqual(['Today', 'Tomorrow']);
    expect(days[0].events.map((e) => e.uid)).toEqual(['a', 'b']);
  });

  it('keeps the order it was given, which is the server sort', () => {
    const days = groupByDay(
      [
        event({ uid: 'later', start: '2026-08-02T09:00:00' }),
        event({ uid: 'sooner', start: '2026-07-28T09:00:00' }),
      ],
      now,
    );
    expect(days.map((d) => d.events[0].uid)).toEqual(['later', 'sooner']);
  });

  it('gives undated events their own trailing bucket', () => {
    const days = groupByDay(
      [event({ uid: 'a', start: '2026-07-28T09:00:00' }), event({ uid: 'x', start: null })],
      now,
    );
    expect(days[days.length - 1].label).toBe('No date');
  });

  it('is empty for no events', () => {
    expect(groupByDay([], now)).toEqual([]);
  });
});

describe('localDatetimeValue', () => {
  it('produces the wall-clock value a datetime-local input expects', () => {
    expect(localDatetimeValue(new Date(2026, 6, 28, 9, 5))).toBe('2026-07-28T09:05');
  });
});
