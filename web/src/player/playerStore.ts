import { create } from 'zustand';

interface PlayerState {
  currentTime: number;
  duration: number;
  activeIndex: number;
  playing: boolean;
  rate: number;
  follow: boolean;
  set: (patch: Partial<PlayerState>) => void;
}

/**
 * Playhead state lives in a store, not in Context.
 *
 * The tick runs at 10 Hz; pushing it through Context would re-render the whole
 * transcript ten times a second. With selector subscriptions only the two rows
 * whose active flag flipped re-render.
 */
export const usePlayerStore = create<PlayerState>((set) => ({
  currentTime: 0,
  duration: 0,
  activeIndex: -1,
  playing: false,
  rate: 1,
  follow: true,
  set: (patch) => set(patch),
}));

/** Subscribe a single row to just its own active flag. */
export const useIsActive = (index: number) =>
  usePlayerStore((s) => s.activeIndex === index);
