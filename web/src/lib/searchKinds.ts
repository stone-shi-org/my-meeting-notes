import type { SearchKind } from '@/types/api';

/**
 * The fixed order results are grouped in, and the heading each group gets.
 *
 * Fixed rather than ordered by best score: the sections then stay where the
 * reader left them between one query and the next, and the content kinds --
 * what was said, written, sent -- come before the containers they live in.
 */
export const SEARCH_KIND_ORDER: SearchKind[] = [
  'segment',
  'summary',
  'note',
  'email',
  'event',
  'action_item',
  'meeting',
  'thread',
];

export const SEARCH_KIND_LABELS: Record<SearchKind, string> = {
  segment: 'Transcripts',
  summary: 'Summaries',
  note: 'Notes',
  email: 'Emails',
  event: 'Events',
  action_item: 'Action items',
  meeting: 'Meetings',
  thread: 'Threads',
};
