import { Pause, Play, Rewind, FastForward } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { usePlayer } from '@/player/PlayerProvider';
import { usePlayerStore } from '@/player/playerStore';
import { fmtClock } from '@/lib/time';

const RATES = [0.75, 1, 1.25, 1.5, 1.75, 2];

export function PlayerBar() {
  const { seek, toggle, nudge, setRate } = usePlayer();
  const currentTime = usePlayerStore((s) => s.currentTime);
  const duration = usePlayerStore((s) => s.duration);
  const playing = usePlayerStore((s) => s.playing);
  const rate = usePlayerStore((s) => s.rate);
  const follow = usePlayerStore((s) => s.follow);
  const set = usePlayerStore((s) => s.set);

  return (
    <div
      className="sticky bottom-0 z-30 border-t border-border bg-surface/90 backdrop-blur-xl"
      style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}
    >
      <div className="flex items-center gap-3 px-3 py-2.5">
        <Button
          size="icon-sm"
          variant="ghost"
          onClick={() => nudge(-15)}
          aria-label="Back 15 seconds"
        >
          <Rewind />
        </Button>

        <Button
          size="icon"
          variant="primary"
          onClick={toggle}
          aria-label={playing ? 'Pause' : 'Play'}
        >
          {playing ? <Pause /> : <Play />}
        </Button>

        <Button
          size="icon-sm"
          variant="ghost"
          onClick={() => nudge(15)}
          aria-label="Forward 15 seconds"
        >
          <FastForward />
        </Button>

        <input
          type="range"
          min={0}
          max={duration || 0}
          step={0.1}
          value={currentTime}
          onChange={(e) => seek(Number(e.target.value))}
          aria-label="Seek"
          // The bare number is meaningless read aloud.
          aria-valuetext={`${fmtClock(currentTime)} of ${fmtClock(duration)}`}
          className="h-1.5 flex-1 cursor-pointer appearance-none rounded-full bg-surface-3 accent-primary"
        />

        <span className="hidden shrink-0 font-mono text-xs text-fg-muted tabular sm:inline">
          {fmtClock(currentTime)} / {fmtClock(duration)}
        </span>

        <select
          value={rate}
          onChange={(e) => setRate(Number(e.target.value))}
          aria-label="Playback speed"
          className="h-8 shrink-0 rounded border border-border-strong bg-surface px-1.5 text-xs"
        >
          {RATES.map((r) => (
            <option key={r} value={r}>
              {r}×
            </option>
          ))}
        </select>

        <label className="hidden shrink-0 items-center gap-1.5 text-xs text-fg-muted sm:flex">
          <input
            type="checkbox"
            checked={follow}
            onChange={(e) => set({ follow: e.target.checked })}
            className="size-3.5 rounded border-border-strong"
          />
          Follow
        </label>
      </div>
    </div>
  );
}
