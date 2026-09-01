import { Download } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Select } from '@/components/ui/primitives';
import type { Meeting } from '@/types/api';

/**
 * Download a meeting's audio.
 *
 * Once the pipeline has converted a recording there are two files worth
 * having -- the 16kHz mono copy everything downstream runs on, and the
 * original upload -- so the control becomes a picker rather than a single
 * button. A meeting whose audio was never converted (uploaded already in the
 * target format, or still mid-pipeline) only has the one file, and offering
 * a choice with a single real answer is worse than not offering one.
 *
 * Shared between the normal transcript view and the "recording but no
 * transcript" gap left when the ingest job failed -- the download itself
 * doesn't care whether the transcript exists.
 */
export function DownloadAudioButton({ meeting }: { meeting: Meeting }) {
  if (meeting.audio_converted) {
    return (
      <Select
        className="w-auto"
        aria-label="Download audio"
        defaultValue=""
        onChange={(e) => {
          if (!e.target.value) return;
          window.open(`/api/meetings/${meeting.id}/audio?original=${e.target.value}`, '_blank');
          e.target.value = '';
        }}
      >
        <option value="">Download audio…</option>
        <option value="false">Converted (16kHz mono)</option>
        <option value="true">Original recording</option>
      </Select>
    );
  }

  return (
    <Button variant="ghost" onClick={() => window.open(`/api/meetings/${meeting.id}/audio`, '_blank')}>
      <Download />
      Download audio
    </Button>
  );
}
