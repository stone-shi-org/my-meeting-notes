import { useMutation } from '@tanstack/react-query';
import { Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import type { Meeting } from '@/types/api';

/**
 * Delete one meeting, audio and all.
 *
 * A meeting created from a calendar event that turned out not to happen is
 * otherwise permanent, and there is no other way to remove one short of
 * deleting the whole thread it sits on.
 *
 * The confirmation names what actually goes, because the answer differs: an
 * empty meeting costs nothing to recreate, one with a transcript is the only
 * copy of an hour of speech.
 */
export function DeleteMeetingButton({
  meeting,
  onDeleted,
  variant = 'button',
  className,
}: {
  meeting: Meeting;
  onDeleted?: () => void;
  /** `icon` is the hover-revealed form used on a list row. */
  variant?: 'button' | 'icon';
  className?: string;
}) {
  const remove = useMutation({
    mutationFn: () => api.del(`/meetings/${meeting.id}`),
    onSuccess: () => onDeleted?.(),
  });

  const blank = !meeting.has_audio && !meeting.has_transcript;
  const confirm = () => {
    const what = blank
      ? `Delete “${meeting.title}”? It has no recording, so nothing else goes with it.`
      : `Delete “${meeting.title}”? Its recording, transcript and summaries are deleted too, ` +
        'and the audio is removed from disk. This cannot be undone.';
    if (window.confirm(what)) remove.mutate();
  };

  if (variant === 'icon') {
    return (
      <button
        type="button"
        onClick={(e) => {
          // The row is a link to the meeting; deleting is not navigating.
          e.preventDefault();
          e.stopPropagation();
          confirm();
        }}
        disabled={remove.isPending}
        aria-label={`Delete meeting ${meeting.title}`}
        title="Delete this meeting"
        className={cn(
          'shrink-0 rounded p-1 text-fg-faint transition-opacity',
          'opacity-0 group-hover:opacity-100 focus-visible:opacity-100',
          'hover:text-danger-ink disabled:opacity-50',
          className,
        )}
      >
        <Trash2 className="size-3.5" aria-hidden />
      </button>
    );
  }

  return (
    <Button
      type="button"
      variant="ghost"
      className={cn('text-danger-ink', className)}
      loading={remove.isPending}
      onClick={confirm}
    >
      <Trash2 />
      Delete meeting
    </Button>
  );
}
