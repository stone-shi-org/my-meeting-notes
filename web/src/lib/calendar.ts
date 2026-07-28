/**
 * Calendar-event dates for display.
 *
 * Providers hand back two shapes: a full ISO stamp for a timed event, and a bare
 * `2026-07-30` for an all-day one. The bare form is the trap -- `new Date()`
 * parses a date-only string as *UTC* midnight, so anywhere west of Greenwich an
 * all-day event renders on the previous day. Everything here goes through
 * `eventDate`, which builds those in local time instead.
 */

import type { UpcomingEvent } from '@/types/api';

/** A `VALUE=DATE` all-day stamp: a bare date, no time part. */
export function isAllDay(stamp: string | null | undefined): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(stamp ?? '');
}

export function eventDate(stamp: string | null | undefined): Date | null {
  if (!stamp) return null;
  if (isAllDay(stamp)) {
    const [year, month, day] = stamp.split('-').map(Number);
    return new Date(year, month - 1, day);
  }
  const parsed = new Date(stamp);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/** Local `YYYY-MM-DD`, the key events are grouped under. */
export function dayKey(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

export function dayLabel(key: string, now = new Date()): string {
  if (key === dayKey(now)) return 'Today';

  // Calendar arithmetic, not +24h: on the two DST changeover days adding a
  // fixed number of milliseconds can skip or repeat a local date.
  const tomorrow = new Date(now);
  tomorrow.setDate(tomorrow.getDate() + 1);
  if (key === dayKey(tomorrow)) return 'Tomorrow';

  const [year, month, day] = key.split('-').map(Number);
  return new Date(year, month - 1, day).toLocaleDateString(undefined, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
  });
}

function clock(date: Date): string {
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/** `09:00 – 09:30`, `09:00` when the end is missing, `All day` for a bare date. */
export function eventTimeLabel(event: { start?: string | null; end?: string | null }): string {
  if (isAllDay(event.start)) return 'All day';

  const start = eventDate(event.start);
  if (!start) return '';

  const end = eventDate(event.end);
  // An end on a different day would read as a wrong time rather than a range.
  if (!end || dayKey(end) !== dayKey(start)) return clock(start);
  return `${clock(start)} – ${clock(end)}`;
}

export interface EventDay {
  key: string;
  label: string;
  events: UpcomingEvent[];
}

/** Group an already-sorted listing into day buckets, order preserved. */
export function groupByDay(events: UpcomingEvent[], now = new Date()): EventDay[] {
  const days: EventDay[] = [];

  for (const event of events) {
    const date = eventDate(event.start);
    // Undated events sort last server-side and get their own trailing bucket.
    const key = date ? dayKey(date) : 'undated';
    const last = days[days.length - 1];

    if (last?.key === key) last.events.push(event);
    else days.push({ key, label: key === 'undated' ? 'No date' : dayLabel(key, now), events: [event] });
  }

  return days;
}

/** `<input type="datetime-local">` wants a local wall-clock string, not an ISO stamp. */
export function localDatetimeValue(date = new Date()): string {
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}
