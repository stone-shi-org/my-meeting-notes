/**
 * Thread group identity colours — the 3px rail down the left of a card.
 *
 * Derived from the group's *id*, never from its position in the list: ordering
 * is by name, so renaming one group would otherwise repaint several others,
 * and deleting one would repaint everything below it. Same rule, same reason,
 * as `speakerColors.ts`.
 *
 * Colour is decoration here. A card always sits under a section heading that
 * names its group, so the rail is a redundant accelerator rather than the only
 * thing saying where the card belongs.
 */

export const GROUP_SLOTS = 8;

/** Stable slot 0..7 for a group id. */
export function groupSlot(groupId: number): number {
  return Math.abs(Math.trunc(groupId)) % GROUP_SLOTS;
}

/**
 * The rail colour for a thread, as a CSS value.
 *
 * Ungrouped keeps `--entity-meeting`, the indigo every one of the app's own
 * objects wears elsewhere: "not filed anywhere" is the absence of a group
 * colour, not a ninth one.
 */
export function groupRailColor(groupId: number | null): string {
  if (groupId === null) return 'var(--entity-meeting)';
  return `var(--group-${groupSlot(groupId)})`;
}
