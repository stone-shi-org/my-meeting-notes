import { describe, expect, it } from 'vitest';
import { GROUP_SLOTS, groupRailColor, groupSlot } from '../groupColors';

describe('group identity colours', () => {
  it('gives consecutive groups different slots', () => {
    const slots = [1, 2, 3, 4].map(groupSlot);
    expect(new Set(slots).size).toBe(4);
  });

  it('is stable for a given id', () => {
    expect(groupSlot(3)).toBe(groupSlot(3));
  });

  it('stays inside the palette however large the id gets', () => {
    for (const id of [0, 7, 8, 99, 100_000]) {
      expect(groupSlot(id)).toBeGreaterThanOrEqual(0);
      expect(groupSlot(id)).toBeLessThan(GROUP_SLOTS);
    }
  });

  it('wraps after the palette is exhausted', () => {
    expect(groupSlot(GROUP_SLOTS + 1)).toBe(groupSlot(1));
  });

  it('resolves to a group token', () => {
    expect(groupRailColor(2)).toBe('var(--group-2)');
  });

  it('leaves Ungrouped on the app default, not a ninth group colour', () => {
    expect(groupRailColor(null)).toBe('var(--entity-meeting)');
  });
});
