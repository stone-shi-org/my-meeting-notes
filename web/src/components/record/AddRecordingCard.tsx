import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AudioInput, type FileMeta } from '@/components/record/AudioInput';
import { Button } from '@/components/ui/Button';
import { Card, Input, Label } from '@/components/ui/primitives';
import { watchJob } from '@/hooks/useJob';
import { uploadMeetingAudio } from '@/lib/api';
import type { Meeting } from '@/types/api';

/**
 * Give a meeting the recording it was created without.
 *
 * A meeting made from an upcoming calendar event exists before the meeting has
 * happened -- that is the point of it -- so its audio necessarily arrives
 * later. Uploading through "New meeting" instead would create a *second*
 * meeting and leave the calendar event, the attendee-derived speaker names and
 * the place on the thread's timeline attached to the empty one.
 */
export function AddRecordingCard({ meeting }: { meeting: Meeting }) {
  const navigate = useNavigate();
  const [file, setFile] = useState<File | null>(null);
  const [speakerNames, setSpeakerNames] = useState('');
  const [autoSummarize, setAutoSummarize] = useState(true);
  const [progress, setProgress] = useState<{ loaded: number; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  const uploading = progress !== null;
  // Hints seeded from the calendar event's attendees are already on the
  // meeting; offering the field again would only let them be overwritten.
  const askForSpeakers = meeting.speaker_count === 0;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) {
      setError('Record something, or choose an audio file');
      return;
    }

    setError(null);
    const form = new FormData();
    form.append('file', file);
    form.append('auto_summarize', String(autoSummarize));
    if (speakerNames.trim()) form.append('speaker_names', speakerNames);

    abort.current = new AbortController();
    setProgress({ loaded: 0, total: file.size });

    try {
      const result = await uploadMeetingAudio(
        meeting.id,
        form,
        (loaded, total) => setProgress({ loaded, total }),
        abort.current.signal,
      );
      watchJob(result.job_id);
      navigate(`/jobs/${result.job_id}`, { replace: true });
    } catch (err) {
      setProgress(null);
      setError(err instanceof Error ? err.message : 'Upload failed');
    }
  }

  return (
    <Card className="p-5">
      <h2 className="font-display text-lg font-semibold">Add the recording</h2>
      <p className="mt-1 text-sm text-fg-subtle">
        It joins this meeting, so whatever is already attached to it stays attached.
      </p>

      <form onSubmit={submit} className="mt-4 space-y-5">
        <AudioInput
          file={file}
          onFile={(next: File | null, _meta: FileMeta) => setFile(next)}
          progress={progress}
        />

        {askForSpeakers && (
          <div>
            <Label htmlFor="add-speakers">Speakers (optional)</Label>
            <Input
              id="add-speakers"
              className="mt-1.5"
              value={speakerNames}
              onChange={(e) => setSpeakerNames(e.target.value)}
              placeholder="Alice, Bob, Priya"
              disabled={uploading}
            />
            <p className="mt-1 text-xs text-fg-subtle">
              Comma separated. Offered as suggestions once we know who spoke most.
            </p>
          </div>
        )}

        <label className="flex items-center gap-2 text-sm text-fg-muted">
          <input
            type="checkbox"
            checked={autoSummarize}
            onChange={(e) => setAutoSummarize(e.target.checked)}
            disabled={uploading}
            className="size-4 rounded border-border-strong"
          />
          Summarize automatically when the transcript is ready
        </label>

        {error && (
          <p role="alert" className="text-sm text-danger-ink">
            {error}
          </p>
        )}

        <div className="flex justify-end gap-2">
          {uploading && (
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                abort.current?.abort();
                setProgress(null);
              }}
            >
              Cancel upload
            </Button>
          )}
          <Button type="submit" variant="primary" loading={uploading} disabled={!file}>
            Start processing
          </Button>
        </div>
      </form>
    </Card>
  );
}
