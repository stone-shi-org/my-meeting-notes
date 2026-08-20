import { FileAudio, Mic, Upload, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { RecorderPanel } from '@/components/record/RecorderPanel';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/primitives';
import { cn } from '@/lib/cn';

/** Extensions the upload route accepts. Kept in step with audio.ALLOWED_EXTENSIONS. */
const ACCEPT =
  '.wav,.mp3,.m4a,.mp4,.aac,.flac,.ogg,.oga,.opus,.wma,.webm,.qta,.mov,.caf,.aiff,.aif';

export function fmtBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

export interface FileMeta {
  /** Set when the file came from the recorder rather than the disk. */
  recorded: boolean;
  durationSec: number;
}

const EMPTY_META: FileMeta = {
  recorded: false,
  durationSec: 0,
};

/**
 * Choose audio: drop a file, or record one here.
 *
 * Shared by "new meeting" and by "add a recording to this meeting" -- the same
 * two ways of producing audio, and the second page exists precisely because a
 * meeting can be created before its recording is made.
 */
export function AudioInput({
  file,
  onFile,
  disabled,
  progress,
  onModeChange,
  recorderLayout,
  rightExtra,
}: {
  file: File | null;
  onFile: (file: File | null, meta: FileMeta) => void;
  disabled?: boolean;
  /** Upload progress, when the host page is mid-upload. */
  progress?: { loaded: number; total: number } | null;
  /** Fired on mount and whenever the Upload/Record tab changes, so a host
   * page with room to spare (see NewMeetingPage) can widen itself for the
   * 'record' tab's wide layout instead of cramming it into a form-width
   * column. */
  onModeChange?: (mode: 'upload' | 'record') => void;
  /** Forwarded to RecorderPanel; see its own doc comment. */
  recorderLayout?: 'compact' | 'wide';
  /** The rest of the "set up a meeting" form (title, when, thread, submit --
   * see NewMeetingPage) that the host wants to render alongside audio
   * capture rather than below it. In 'record' + 'wide' this ends up in
   * RecorderPanel's right column, next to the controls; otherwise it just
   * renders beneath this component's own content, which is where the host
   * used to render it directly. Centralising the placement here is what
   * lets a single mode switch move it without the host caring which layout
   * is active. */
  rightExtra?: React.ReactNode;
}) {
  const [mode, setMode] = useState<'upload' | 'record'>('upload');
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  // Mirrors RecorderPanel's recorder.live -- see its onLiveChange doc
  // comment for why this has to be asked about *before* switching tabs
  // rather than cleaned up after: unmounting RecorderPanel mid-recording
  // just loses the audio, there is nothing to undo once that happens.
  const [recordingLive, setRecordingLive] = useState(false);

  useEffect(() => {
    onModeChange?.(mode);
    // onModeChange is expected to be a stable setState-style callback; not a
    // dep so the host doesn't have to memoize it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  const uploading = !!progress;
  const pct = progress ? Math.round((progress.loaded / progress.total) * 100) : 0;

  return (
    <div className="space-y-5">
      <div
        role="tablist"
        aria-label="Where the audio comes from"
        className="inline-flex rounded-lg border border-border bg-surface-2/60 p-1"
      >
        {(
          [
            { id: 'upload', label: 'Upload a file', icon: Upload },
            { id: 'record', label: 'Record now', icon: Mic },
          ] as const
        ).map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={mode === id}
            disabled={uploading || disabled}
            onClick={() => {
              // Leaving the Record tab mid-recording unmounts RecorderPanel,
              // which releases the mic without ever finalizing a file --
              // silent data loss, not just "stopped". Ask first.
              if (mode === 'record' && id !== 'record' && recordingLive) {
                const ok = window.confirm(
                  'A recording is in progress. Switching to Upload will stop it and discard ' +
                    'everything captured so far. Continue?',
                );
                if (!ok) return;
              }
              setMode(id);
              // Switching away drops whatever was staged: each mode owns its own
              // source, and a stale file behind the other tab is how you send
              // the wrong thing.
              onFile(null, EMPTY_META);
            }}
            className={cn(
              'inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors duration-fast',
              mode === id
                ? 'bg-surface text-fg shadow-xs'
                : 'text-fg-muted hover:text-fg disabled:opacity-60',
            )}
          >
            <Icon className="size-4" aria-hidden />
            {label}
          </button>
        ))}
      </div>

      {mode === 'record' && (
        <RecorderPanel
          disabled={uploading || disabled}
          layout={recorderLayout}
          rightExtra={rightExtra}
          onLiveChange={setRecordingLive}
          onRecorded={(next, durationSec) => onFile(next, { recorded: true, durationSec })}
        />
      )}

      {mode === 'upload' && !file ? (
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            onFile(e.dataTransfer.files[0] ?? null, EMPTY_META);
          }}
          className={cn(
            'rounded-xl border-2 border-dashed p-10 text-center transition-colors duration-fast',
            dragging ? 'border-primary bg-primary-soft' : 'border-border-strong bg-surface',
          )}
        >
          <Upload className="mx-auto size-8 text-fg-faint" aria-hidden />
          <p className="mt-3 font-medium">Drop audio here</p>
          <p className="mt-1 text-sm text-fg-subtle">wav · mp3 · m4a · ogg · flac and more</p>
          {/* A real input, so the dropzone is keyboard reachable. */}
          <input
            ref={inputRef}
            type="file"
            accept={ACCEPT}
            className="sr-only"
            id="audio-file"
            onChange={(e) => onFile(e.target.files?.[0] ?? null, EMPTY_META)}
          />
          <Button
            type="button"
            variant="secondary"
            className="mt-4"
            disabled={disabled}
            onClick={() => inputRef.current?.click()}
          >
            Browse files
          </Button>
        </div>
      ) : // In record mode the panel shows its own player and Discard, so the
      // file card would say it twice -- except while uploading, when it is the
      // only thing carrying the progress bar.
      file && (mode === 'upload' || uploading) ? (
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
              disabled={disabled}
              onClick={() => onFile(null, EMPTY_META)}
            >
              <X />
            </Button>
          )}
        </Card>
      ) : null}

      {mode === 'upload' && rightExtra}
    </div>
  );
}
