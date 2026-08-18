import { FileAudio, Mic, Plus, Trash2, Upload, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { RecorderPanel } from '@/components/record/RecorderPanel';
import { Button } from '@/components/ui/Button';
import { Card, Input, Label, Select } from '@/components/ui/primitives';
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

/** One row of app/routers/meetings.py's `channels` JSON field -- one entry
 * per resulting channel, in channel_index order. `runDiarization` off (the
 * default here) means "this whole source is one known speaker"; on means
 * "let the model tell voices on this source apart" -- see
 * meeting_audio_channels' doc comment in db.py. */
export interface UploadChannelMeta {
  label: string;
  runDiarization: boolean;
  /** Only meaningful for mode 'multi_file' with more than one source. */
  startOffsetSec: number;
}

/** Upload-only alternative to a single blended file -- see
 * app/routers/meetings.py's `mode`/`channels`/`extra_files` fields.
 * 'multi_channel': one uploaded file that already has one speaker per
 * channel. 'multi_file': N separately uploaded files, one per speaker. */
export interface MultiSourceUpload {
  mode: 'multi_channel' | 'multi_file';
  files: File[];
  channels: UploadChannelMeta[];
}

export interface FileMeta {
  /** Set when the file came from the recorder rather than the disk. */
  recorded: boolean;
  durationSec: number;
  /** 'mic_room' when the recording kept mic and room audio on separate
   * channels. Always null for an uploaded file -- there is no capture graph
   * to have split anything. */
  channelMap: ChannelMap;
  /** Set only for an upload with more than one speaker source; see
   * MultiSourceUpload. `file` (the first onFile argument) is still that
   * upload's first/primary file -- multi.files is the *rest*, matching the
   * backend's file + extra_files split. */
  multi?: MultiSourceUpload | null;
}

const EMPTY_META: FileMeta = {
  recorded: false,
  durationSec: 0,
  channelMap: null,
  multi: null,
};

function newChannelMeta(): UploadChannelMeta {
  return { label: '', runDiarization: false, startOffsetSec: 0 };
}

/**
 * One speaker's settings within a multi-source upload -- a name (which
 * doubles as that channel's speaker id and skips diarizing it, see
 * UploadChannelMeta) and, only when it's still a mix of people, a diarize
 * toggle. The offset field only makes sense for 'multi_file' with more than
 * one source, where nothing guarantees the files started together.
 */
function ChannelRow({
  index,
  channel,
  onChange,
  disabled,
  showOffset = false,
}: {
  index: number;
  channel: UploadChannelMeta;
  onChange: (next: UploadChannelMeta) => void;
  disabled?: boolean;
  showOffset?: boolean;
}) {
  return (
    <div className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-end">
      <div>
        <Label htmlFor={`channel-label-${index}`}>Speaker {index + 1} name (optional)</Label>
        <Input
          id={`channel-label-${index}`}
          className="mt-1.5"
          value={channel.label}
          placeholder="Leave blank if unknown"
          disabled={disabled}
          onChange={(e) => onChange({ ...channel, label: e.target.value })}
        />
      </div>
      <div className="flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2 text-sm text-fg-muted">
          <input
            type="checkbox"
            checked={channel.runDiarization}
            disabled={disabled}
            onChange={(e) => onChange({ ...channel, runDiarization: e.target.checked })}
            className="size-4 rounded border-border-strong"
          />
          Diarize this one
        </label>
        {showOffset && (
          <div>
            <Label htmlFor={`channel-offset-${index}`}>Starts at (seconds)</Label>
            <Input
              id={`channel-offset-${index}`}
              className="mt-1.5 w-28"
              type="number"
              min={0}
              step="0.1"
              value={channel.startOffsetSec}
              disabled={disabled}
              onChange={(e) =>
                onChange({ ...channel, startOffsetSec: Math.max(0, Number(e.target.value) || 0) })
              }
            />
          </div>
        )}
      </div>
    </div>
  );
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
  onModeChange,
  recorderLayout,
  rightExtra,
  skipDiarization,
  onSkipDiarizationChange,
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
  /** Upload-only: skip the model diarization call and get a flat,
   * single-speaker transcript instead. A recording made here already knows
   * its own channel/speaker shape (see RecorderPanel), so this only ever
   * shows on the Upload tab -- rendered here rather than in the host's own
   * form because only this component knows which tab is active. */
  skipDiarization?: boolean;
  onSkipDiarizationChange?: (skip: boolean) => void;
}) {
  const [mode, setMode] = useState<'upload' | 'record'>('upload');
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const multiFileInputRef = useRef<HTMLInputElement>(null);
  // Mirrors RecorderPanel's recorder.live -- see its onLiveChange doc
  // comment for why this has to be asked about *before* switching tabs
  // rather than cleaned up after: unmounting RecorderPanel mid-recording
  // just loses the audio, there is nothing to undo once that happens.
  const [recordingLive, setRecordingLive] = useState(false);

  // 'single' is the overwhelmingly common case and what every non-upload
  // path (the dropzone, the recorder) still produces -- this only branches
  // the UI into MultiSourceUpload territory when someone deliberately picks
  // one of the other two. See FileMeta.multi for what each becomes on submit.
  const [uploadShape, setUploadShape] = useState<'single' | 'multi_channel' | 'multi_file'>(
    'single',
  );
  const [multiFile, setMultiFile] = useState<File | null>(null); // multi_channel: the one file
  const [multiChannelCount, setMultiChannelCount] = useState(2); // multi_channel only
  const [multiFiles, setMultiFiles] = useState<File[]>([]); // multi_file: every file
  const [multiMeta, setMultiMeta] = useState<UploadChannelMeta[]>([
    newChannelMeta(),
    newChannelMeta(),
  ]);

  function resetMultiState() {
    setUploadShape('single');
    setMultiFile(null);
    setMultiFiles([]);
    setMultiChannelCount(2);
    setMultiMeta([newChannelMeta(), newChannelMeta()]);
  }

  function withMetaCount<T>(list: T[], count: number, make: () => T): T[] {
    const next = list.slice(0, count);
    while (next.length < count) next.push(make());
    return next;
  }

  function emitMultiChannel(nextFile: File | null, meta: UploadChannelMeta[]) {
    onFile(nextFile, {
      ...EMPTY_META,
      multi: nextFile ? { mode: 'multi_channel', files: [], channels: meta } : null,
    });
  }

  function emitMultiFile(files: File[], meta: UploadChannelMeta[]) {
    const [first, ...rest] = files;
    onFile(first ?? null, {
      ...EMPTY_META,
      multi: first ? { mode: 'multi_file', files: rest, channels: meta } : null,
    });
  }

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
              resetMultiState();
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
          onRecorded={(next, durationSec, channelMap) =>
            onFile(next, { recorded: true, durationSec, channelMap })
          }
        />
      )}

      {mode === 'upload' && (
        <div>
          <Label htmlFor="upload-shape">Speakers</Label>
          <Select
            id="upload-shape"
            className="mt-1.5"
            value={uploadShape}
            disabled={uploading || disabled}
            onChange={(e) => {
              const next = e.target.value as typeof uploadShape;
              resetMultiState();
              setUploadShape(next);
              onFile(null, EMPTY_META);
            }}
          >
            <option value="single">One file (mixed, or a single speaker)</option>
            <option value="multi_channel">One file, already split into a channel per speaker</option>
            <option value="multi_file">A separate file for each speaker</option>
          </Select>
          {uploadShape !== 'single' && (
            <p className="mt-1 text-xs text-fg-subtle">
              Each speaker's audio is transcribed on its own -- name one to skip diarizing it
              (it's already known to be just them), or leave it blank and turn on "Diarize this
              one" if it's still a mix of multiple people.
            </p>
          )}
        </div>
      )}

      {mode === 'upload' && uploadShape === 'multi_channel' && (
        <Card className="space-y-4 p-4">
          <div>
            <Label htmlFor="upload-multi-channel-file">Audio file</Label>
            <Input
              id="upload-multi-channel-file"
              className="mt-1.5"
              type="file"
              accept={ACCEPT}
              disabled={uploading || disabled}
              onChange={(e) => {
                const picked = e.target.files?.[0] ?? null;
                setMultiFile(picked);
                emitMultiChannel(picked, multiMeta);
              }}
            />
            {multiFile && (
              <p className="mt-1 text-xs text-fg-subtle">
                {multiFile.name} · {fmtBytes(multiFile.size)}
              </p>
            )}
          </div>
          <div>
            <Label htmlFor="upload-channel-count">Number of channels</Label>
            <Input
              id="upload-channel-count"
              className="mt-1.5 w-24"
              type="number"
              min={2}
              max={16}
              value={multiChannelCount}
              disabled={uploading || disabled}
              onChange={(e) => {
                const count = Math.max(2, Math.min(16, Number(e.target.value) || 2));
                const nextMeta = withMetaCount(multiMeta, count, newChannelMeta);
                setMultiChannelCount(count);
                setMultiMeta(nextMeta);
                emitMultiChannel(multiFile, nextMeta);
              }}
            />
          </div>
          <div className="space-y-3">
            {multiMeta.map((ch, i) => (
              <ChannelRow
                key={i}
                index={i}
                channel={ch}
                disabled={uploading || disabled}
                onChange={(next) => {
                  const nextMeta = multiMeta.map((c, j) => (j === i ? next : c));
                  setMultiMeta(nextMeta);
                  emitMultiChannel(multiFile, nextMeta);
                }}
              />
            ))}
          </div>
        </Card>
      )}

      {mode === 'upload' && uploadShape === 'multi_file' && (
        <Card className="space-y-4 p-4">
          <div>
            <Label htmlFor="upload-multi-files">Audio files, one per speaker</Label>
            <input
              ref={multiFileInputRef}
              id="upload-multi-files"
              className="sr-only"
              type="file"
              accept={ACCEPT}
              multiple
              disabled={uploading || disabled}
              onChange={(e) => {
                const picked = Array.from(e.target.files ?? []);
                const files = [...multiFiles, ...picked];
                const nextMeta = withMetaCount(multiMeta, files.length, newChannelMeta);
                setMultiFiles(files);
                setMultiMeta(nextMeta);
                emitMultiFile(files, nextMeta);
                e.target.value = '';
              }}
            />
            <Button
              type="button"
              variant="secondary"
              size="sm"
              className="mt-1.5"
              disabled={uploading || disabled}
              onClick={() => multiFileInputRef.current?.click()}
            >
              <Plus />
              Add file{multiFiles.length > 0 ? 's' : ''}
            </Button>
          </div>
          <div className="space-y-3">
            {multiFiles.map((f, i) => (
              <div key={i} className="space-y-2 rounded-lg border border-border p-3">
                <div className="flex items-center justify-between gap-2">
                  <p className="min-w-0 truncate text-sm font-medium">
                    {f.name} <span className="font-normal text-fg-subtle">· {fmtBytes(f.size)}</span>
                  </p>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Remove ${f.name}`}
                    disabled={uploading || disabled}
                    onClick={() => {
                      const files = multiFiles.filter((_, j) => j !== i);
                      const meta = multiMeta.filter((_, j) => j !== i);
                      setMultiFiles(files);
                      setMultiMeta(meta);
                      emitMultiFile(files, meta);
                    }}
                  >
                    <Trash2 />
                  </Button>
                </div>
                <ChannelRow
                  index={i}
                  channel={multiMeta[i] ?? newChannelMeta()}
                  showOffset={multiFiles.length > 1}
                  disabled={uploading || disabled}
                  onChange={(next) => {
                    const nextMeta = multiMeta.map((c, j) => (j === i ? next : c));
                    setMultiMeta(nextMeta);
                    emitMultiFile(multiFiles, nextMeta);
                  }}
                />
              </div>
            ))}
            {multiFiles.length === 0 && (
              <p className="text-sm text-fg-faint">No files added yet.</p>
            )}
          </div>
        </Card>
      )}

      {mode === 'upload' && uploadShape === 'single' && !file ? (
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
      // only thing carrying the progress bar. In a multi-source upload shape
      // the per-row lists above already show each selected file, so this
      // only reappears there to carry the progress bar mid-upload.
      file && ((mode === 'upload' && uploadShape === 'single') || uploading) ? (
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

      {mode === 'upload' && uploadShape === 'single' && (
        <label className="flex items-center gap-2 text-sm text-fg-muted">
          <input
            type="checkbox"
            checked={!skipDiarization}
            disabled={disabled}
            onChange={(e) => onSkipDiarizationChange?.(!e.target.checked)}
            className="size-4 rounded border-border-strong"
          />
          Run diarization
          <span className="text-xs text-fg-subtle">
            — off skips speaker separation entirely and gives one flat transcript instead
          </span>
        </label>
      )}

      {mode === 'upload' && rightExtra}
    </div>
  );
}
