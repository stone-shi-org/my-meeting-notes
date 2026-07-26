import { useQuery } from '@tanstack/react-query';
import { FileAudio, Upload, X } from 'lucide-react';
import { useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { Card, Input, Label, Select, Textarea } from '@/components/ui/primitives';
import { api, uploadMeeting } from '@/lib/api';
import { watchJob } from '@/hooks/useJob';
import { cn } from '@/lib/cn';
import type { Paginated, Thread } from '@/types/api';

const ACCEPT =
  '.wav,.mp3,.m4a,.mp4,.aac,.flac,.ogg,.oga,.opus,.wma,.webm,.qta,.mov,.caf,.aiff,.aif';

function fmtBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function localDatetimeValue(date = new Date()): string {
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

export function NewMeetingPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const presetThread = params.get('threadId');

  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
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
  const inputRef = useRef<HTMLInputElement>(null);

  const threads = useQuery({
    queryKey: ['threads', 'picker'],
    queryFn: () => api.get<Paginated<Thread>>('/threads', { page_size: 100 }),
  });

  const creatingNewThread = threadId === '';

  function pick(next: File | null) {
    setFile(next);
    setError(null);
    if (next && !title) {
      // A filename is a better starting point than an empty box.
      setTitle(next.name.replace(/\.[^.]+$/, '').replace(/[-_]+/g, ' '));
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) {
      setError('Choose an audio file first');
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
  const pct = progress ? Math.round((progress.loaded / progress.total) * 100) : 0;

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <h1 className="font-display text-2xl font-semibold">New meeting</h1>
        <p className="mt-1 text-sm text-fg-subtle">
          Upload a recording. We convert it, transcribe it with speaker labels, and summarize it.
        </p>
      </div>

      <form onSubmit={submit} className="space-y-5">
        {!file ? (
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              pick(e.dataTransfer.files[0] ?? null);
            }}
            className={cn(
              'rounded-xl border-2 border-dashed p-10 text-center transition-colors duration-fast',
              dragging ? 'border-primary bg-primary-soft' : 'border-border-strong bg-surface',
            )}
          >
            <Upload className="mx-auto size-8 text-fg-faint" aria-hidden />
            <p className="mt-3 font-medium">Drop audio here</p>
            <p className="mt-1 text-sm text-fg-subtle">
              wav · mp3 · m4a · ogg · flac and more
            </p>
            {/* A real input, so the dropzone is keyboard reachable. */}
            <input
              ref={inputRef}
              type="file"
              accept={ACCEPT}
              className="sr-only"
              id="audio-file"
              onChange={(e) => pick(e.target.files?.[0] ?? null)}
            />
            <Button
              type="button"
              variant="secondary"
              className="mt-4"
              onClick={() => inputRef.current?.click()}
            >
              Browse files
            </Button>
          </div>
        ) : (
          <Card className="flex items-center gap-3 p-4">
            <div className="grid size-10 shrink-0 place-items-center rounded-md bg-primary-soft">
              <FileAudio className="size-5 text-primary-soft-fg" aria-hidden />
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate font-medium">{file.name}</p>
              <p className="text-sm text-fg-subtle">{fmtBytes(file.size)}</p>
              {uploading && (
                <div className="mt-2">
                  <div className="h-1.5 overflow-hidden rounded-full bg-surface-2">
                    <div
                      className="h-full rounded-full bg-primary transition-[width]"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <p className="mt-1 text-xs text-fg-subtle tabular">
                    {pct}% · {fmtBytes(progress!.loaded)} of {fmtBytes(progress!.total)}
                  </p>
                </div>
              )}
            </div>
            {!uploading && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label="Remove file"
                onClick={() => pick(null)}
              >
                <X />
              </Button>
            )}
          </Card>
        )}

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
