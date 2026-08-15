import { FileAudio, Mic, Upload, X } from 'lucide-react';
import { useRef, useState } from 'react';
import { RecorderPanel, type RoomSpeakers } from '@/components/record/RecorderPanel';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/primitives';
import type { ChannelMap } from '@/hooks/useRecorder';
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
  /** 'mic_room' when the recording kept mic and room audio on separate
   * channels. Always null for an uploaded file -- there is no capture graph
   * to have split anything. */
  channelMap: ChannelMap;
  /** Only meaningful alongside channelMap; see RecorderPanel's selector. */
  roomSpeakers: RoomSpeakers;
}

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
}: {
  file: File | null;
  onFile: (file: File | null, meta: FileMeta) => void;
  disabled?: boolean;
  /** Upload progress, when the host page is mid-upload. */
  progress?: { loaded: number; total: number } | null;
}) {
  const [mode, setMode] = useState<'upload' | 'record'>('upload');
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

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
              setMode(id);
              // Switching away drops whatever was staged: each mode owns its own
              // source, and a stale file behind the other tab is how you send
              // the wrong thing.
              onFile(null, { recorded: false, durationSec: 0, channelMap: null, roomSpeakers: 'multiple' });
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
          onRecorded={(next, durationSec, channelMap, roomSpeakers) =>
            onFile(next, { recorded: true, durationSec, channelMap, roomSpeakers })
          }
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
            onFile(e.dataTransfer.files[0] ?? null, { recorded: false, durationSec: 0, channelMap: null, roomSpeakers: 'multiple' });
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
            onChange={(e) =>
              onFile(e.target.files?.[0] ?? null, { recorded: false, durationSec: 0, channelMap: null, roomSpeakers: 'multiple' })
            }
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
              onClick={() => onFile(null, { recorded: false, durationSec: 0, channelMap: null, roomSpeakers: 'multiple' })}
            >
              <X />
            </Button>
          )}
        </Card>
      ) : null}
    </div>
  );
}
