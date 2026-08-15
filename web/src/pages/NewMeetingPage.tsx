import { useQuery } from '@tanstack/react-query';
import { useCallback, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { AudioInput, type FileMeta } from '@/components/record/AudioInput';
import { Button } from '@/components/ui/Button';
import { Card, Input, Label, Select, Textarea } from '@/components/ui/primitives';
import { api, uploadMeeting } from '@/lib/api';
import { watchJob } from '@/hooks/useJob';
import type { ChannelMap } from '@/hooks/useRecorder';
import type { RoomSpeakers } from '@/components/record/RecorderPanel';
import { localDatetimeValue } from '@/lib/calendar';
import type { Paginated, Thread } from '@/types/api';

export function NewMeetingPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const presetThread = params.get('threadId');

  const [file, setFile] = useState<File | null>(null);
  const [channelMap, setChannelMap] = useState<ChannelMap>(null);
  const [roomSpeakers, setRoomSpeakers] = useState<RoomSpeakers>('multiple');
  const [title, setTitle] = useState('');
  const [when, setWhen] = useState(localDatetimeValue());
  const [threadId, setThreadId] = useState(presetThread ?? '');
  const [newThreadTitle, setNewThreadTitle] = useState('');
  const [newThreadDescription, setNewThreadDescription] = useState('');
  const [speakerNames, setSpeakerNames] = useState('');
  const [autoSummarize, setAutoSummarize] = useState(true);

  const [progress, setProgress] = useState<{ loaded: number; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  const threads = useQuery({
    queryKey: ['threads', 'picker'],
    queryFn: () => api.get<Paginated<Thread>>('/threads', { page_size: 100 }),
  });

  const creatingNewThread = threadId === '';

  /** Take whichever source produced audio, and suggest a title from it.
   *
   * A dropped file names itself usefully; a recording's generated name is a
   * timestamp, which makes a poor meeting title, so that one is titled from the
   * clock instead. */
  const pick = useCallback((next: File | null, meta: FileMeta) => {
    setFile(next);
    setChannelMap(meta.channelMap);
    setRoomSpeakers(meta.roomSpeakers);
    setError(null);
    if (!next) return;

    setTitle((current) => {
      if (current) return current;
      return meta.recorded
        ? `Recording ${new Date(next.lastModified).toLocaleString(undefined, {
            day: 'numeric',
            month: 'short',
            hour: '2-digit',
            minute: '2-digit',
          })}`
        : next.name.replace(/\.[^.]+$/, '').replace(/[-_]+/g, ' ');
    });

    if (meta.recorded && meta.durationSec < 1) {
      setError('That recording is under a second long — it will not transcribe to much.');
    }
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) {
      setError('Record something, or choose an audio file');
      return;
    }
    if (creatingNewThread && !newThreadTitle.trim()) {
      setError('Give the new thread a title, or pick an existing one');
      return;
    }

    setError(null);
    const form = new FormData();
    form.append('file', file);
    form.append('title', title);
    form.append('meeting_at', new Date(when).toISOString());
    form.append('auto_summarize', String(autoSummarize));
    if (creatingNewThread) {
      form.append('new_thread_title', newThreadTitle);
      if (newThreadDescription) form.append('new_thread_description', newThreadDescription);
    } else {
      form.append('thread_id', threadId);
    }
    if (speakerNames.trim()) form.append('speaker_names', speakerNames);
    if (channelMap) {
      form.append('channel_map', channelMap);
      form.append('room_speakers', roomSpeakers);
    }

    abort.current = new AbortController();
    setProgress({ loaded: 0, total: file.size });

    try {
      const result = await uploadMeeting(
        form,
        (loaded, total) => setProgress({ loaded, total }),
        abort.current.signal,
      );
      // Remember it so the dock can recover after a refresh.
      watchJob(result.job_id);
      navigate(`/jobs/${result.job_id}`, { replace: true });
    } catch (err) {
      setProgress(null);
      setError(err instanceof Error ? err.message : 'Upload failed');
    }
  }

  const uploading = progress !== null;

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <h1 className="font-display text-2xl font-semibold">New meeting</h1>
        <p className="mt-1 text-sm text-fg-subtle">
          Record it here or upload a file. We convert it, transcribe it with speaker labels, and
          summarize it.
        </p>
      </div>

      <form onSubmit={submit} className="space-y-5">
        <AudioInput file={file} onFile={pick} progress={progress} />

        <Card className="space-y-4 p-5">
          <div>
            <Label htmlFor="m-title">Title</Label>
            <Input
              id="m-title"
              className="mt-1.5"
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Cutover go/no-go"
              disabled={uploading}
            />
          </div>

          <div>
            <Label htmlFor="m-when">When</Label>
            <Input
              id="m-when"
              className="mt-1.5"
              type="datetime-local"
              value={when}
              onChange={(e) => setWhen(e.target.value)}
              disabled={uploading}
            />
          </div>

          <div>
            <Label htmlFor="m-thread">Thread</Label>
            <Select
              id="m-thread"
              className="mt-1.5"
              value={threadId}
              onChange={(e) => setThreadId(e.target.value)}
              disabled={uploading}
            >
              <option value="">＋ Create a new thread</option>
              {threads.data?.items.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.title}
                </option>
              ))}
            </Select>
          </div>

          {creatingNewThread && (
            <div className="space-y-3 rounded-md border border-border bg-surface-2/50 p-3">
              <div>
                <Label htmlFor="nt-title">New thread title</Label>
                <Input
                  id="nt-title"
                  className="mt-1.5"
                  value={newThreadTitle}
                  onChange={(e) => setNewThreadTitle(e.target.value)}
                  placeholder="Atlas Migration"
                  disabled={uploading}
                />
              </div>
              <div>
                <Label htmlFor="nt-desc">Description</Label>
                <Textarea
                  id="nt-desc"
                  className="mt-1.5"
                  rows={2}
                  value={newThreadDescription}
                  onChange={(e) => setNewThreadDescription(e.target.value)}
                  disabled={uploading}
                />
              </div>
            </div>
          )}

          <div>
            <Label htmlFor="m-speakers">Speakers (optional)</Label>
            <Input
              id="m-speakers"
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
        </Card>

        {error && (
          <p role="alert" className="text-sm text-danger-ink">
            {error}
          </p>
        )}

        <div className="flex justify-end gap-2">
          {uploading ? (
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
          ) : (
            <Button type="button" variant="ghost" onClick={() => navigate(-1)}>
              Cancel
            </Button>
          )}
          <Button type="submit" variant="primary" size="lg" loading={uploading} disabled={!file}>
            Start processing
          </Button>
        </div>
      </form>
    </div>
  );
}
