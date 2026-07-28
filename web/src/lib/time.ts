/** Time formatting. Durations take seconds; :func:`fmtRelative` takes an ISO stamp. */

function pad(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/** Player clock: `0:41`, `22:14`, `1:03:07`. Rounds down, like every media player. */
export function fmtClock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/** Elapsed clock: always at least MM:SS, zero-padded so it doesn't jitter. */
export function fmtElapsed(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/** Human duration for prose: `22 min`, `1 hr 3 min`, `45 sec`. */
export function fmtDurationHuman(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '0 sec';
  const total = Math.round(seconds);
  if (total < 60) return `${total} sec`;
  const h = Math.floor(total / 3600);
  const m = Math.round((total % 3600) / 60);
  if (h === 0) return `${m} min`;
  return m === 0 ? `${h} hr` : `${h} hr ${m} min`;
}

/**
 * How long ago, for a timestamp the backend produced: `just now`, `12m ago`.
 *
 * Past a month it gives the date instead -- "63d ago" is arithmetic the reader
 * has to do, and by then the exact day is what they wanted anyway.
 */
export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '—';

  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}

/** WebVTT cue timestamp: `HH:MM:SS.mmm`. Hours are mandatory in VTT. */
export function fmtVtt(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  const ms = Math.round(seconds * 1000);
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  const milli = ms % 1000;
  return `${pad(h)}:${pad(m)}:${pad(s)}.${String(milli).padStart(3, '0')}`;
}
