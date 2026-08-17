#!/usr/bin/env python3
"""
Split a meeting recording audio file into per-speaker audio tracks using LocalAI diarization.

Usage:
    python3 split_speaker_audio.py [INPUT_FILE] [--endpoint ENDPOINT] [--model MODEL] [--output-dir DIR]

Example:
    python3 split_speaker_audio.py ~/tests/recording-tab-audio-2026-08-03-1159.webm \
        --endpoint http://10.100.0.50:4012/v1/audio/diarization \
        --model vibevoice-cpp-asr
"""

import argparse
import json
import os
import subprocess
import sys
import wave
from pathlib import Path
import numpy as np
import requests

DEFAULT_ENDPOINT = "http://10.100.0.50:4012/v1/audio/diarization"
DEFAULT_MODEL = "vibevoice-cpp-asr"
DEFAULT_INPUT = "~/tests/recording-tab-audio-2026-08-03-1159.webm"


def load_audio_pcm(file_path: Path, target_sr: int = 16000) -> tuple[np.ndarray, int]:
    """Loads input audio file as 16kHz mono 16-bit PCM numpy array using ffmpeg."""
    if not file_path.exists():
        raise FileNotFoundError(f"Input audio file not found: {file_path}")

    cmd = [
        "ffmpeg", "-y", "-i", str(file_path),
        "-f", "s16le", "-ac", "1", "-ar", str(target_sr),
        "pipe:1"
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg failed to decode audio file: {e.stderr.decode('utf-8', errors='ignore')}") from e

    samples = np.frombuffer(proc.stdout, dtype=np.int16)
    return samples, target_sr


def save_audio_pcm(file_path: Path, samples: np.ndarray, sample_rate: int = 16000):
    """Writes a 16-bit mono PCM numpy array to a WAV file."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(file_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())
    print(f"-> Saved: {file_path} ({len(samples) / sample_rate:.2f}s)")


def call_diarization_api(audio_path: Path, endpoint: str, model: str, api_key: str = "", timeout: int = 1800) -> dict:
    """POSTs audio file to LocalAI diarization endpoint."""
    upload_name = audio_path.stem + ".wav"
    print(f"-> Sending diarization request ({audio_path.stat().st_size / (1024*1024):.1f} MB) to {endpoint} (model: {model})...")
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    with audio_path.open("rb") as fh:
        files = {"file": (upload_name, fh, "audio/wav")}
        data = {
            "model": model,
            "include_text": "true",
            "response_format": "verbose_json",
        }
        res = requests.post(endpoint, files=files, data=data, headers=headers, timeout=timeout)

    if res.status_code >= 400:
        raise RuntimeError(f"Diarization request failed ({res.status_code}): {res.text[:500]}")

    payload = res.json()
    if not isinstance(payload, dict) or "segments" not in payload:
        raise ValueError(f"Unexpected response format from diarization endpoint: {payload}")

    return payload


def diarize_audio_chunked(
    input_path: Path,
    endpoint: str,
    model: str,
    api_key: str = "",
    chunk_size_sec: int = 600,
    overlap_sec: int = 30
) -> dict:
    """
    Diarizes audio files by breaking long recordings into chunks (e.g. 10 min),
    diarizing each chunk via LocalAI, aligning speaker identities across chunks,
    and combining all segments into a master diarization dictionary.
    """
    pcm_samples, sample_rate = load_audio_pcm(input_path)
    total_sec = len(pcm_samples) / sample_rate

    if total_sec <= chunk_size_sec:
        print(f"-> Audio duration ({total_sec:.1f}s) is within chunk limit ({chunk_size_sec}s). Diarizing in single request...")
        return call_diarization_api(input_path, endpoint, model, api_key=api_key)

    print(f"\n-> Audio duration is {total_sec / 60:.2f} minutes ({total_sec:.1f}s).")
    print(f"-> Using automatic chunking: chunk size = {chunk_size_sec / 60:.1f} min ({chunk_size_sec}s), overlap = {overlap_sec}s.")

    chunk_starts = []
    curr = 0.0
    while curr < total_sec:
        chunk_starts.append(curr)
        if curr + chunk_size_sec >= total_sec:
            break
        curr += (chunk_size_sec - overlap_sec)

    master_segments = []
    prev_chunk_segs = None

    for idx, c_start in enumerate(chunk_starts):
        c_dur = min(chunk_size_sec, total_sec - c_start)
        c_end = c_start + c_dur
        print(f"\n--- Processing Chunk {idx + 1}/{len(chunk_starts)}: [{c_start / 60:.2f}m - {c_end / 60:.2f}m] ---")

        # Export chunk slice to temp wav
        tmp_wav = Path(f"/tmp/diar_chunk_{idx}.wav")
        c_samples_start = int(round(c_start * sample_rate))
        c_samples_end = int(round(c_end * sample_rate))
        chunk_pcm = pcm_samples[c_samples_start:c_samples_end]
        save_audio_pcm(tmp_wav, chunk_pcm, sample_rate)

        try:
            c_res = call_diarization_api(tmp_wav, endpoint, model, api_key=api_key)
        finally:
            tmp_wav.unlink(missing_ok=True)

        raw_c_segs = c_res.get("segments", [])
        if not raw_c_segs:
            print(f"   Notice: Chunk {idx + 1} returned no segments.")
            continue

        # Adjust timestamps to absolute meeting timeline
        abs_c_segs = []
        for s in raw_c_segs:
            abs_s = dict(s)
            abs_s["start"] = s["start"] + c_start
            abs_s["end"] = s["end"] + c_start
            abs_c_segs.append(abs_s)

        local_speakers = sorted(list(set(s["speaker"] for s in abs_c_segs)))

        if idx == 0 or not prev_chunk_segs:
            local_to_master = {spk: spk for spk in local_speakers}
        else:
            # Match speaker identities in overlap region with previous chunk
            overlap_window_start = c_start
            overlap_window_end = c_start + overlap_sec

            prev_overlap_segs = [s for s in prev_chunk_segs if s["start"] >= overlap_window_start - 5.0 and s["start"] <= overlap_window_end + 5.0]
            curr_overlap_segs = [s for s in abs_c_segs if s["start"] >= overlap_window_start - 5.0 and s["start"] <= overlap_window_end + 5.0]

            local_to_master = {}
            for l_spk in local_speakers:
                l_segs = [s for s in curr_overlap_segs if s["speaker"] == l_spk]
                best_match_master_spk = None
                best_match_score = -1.0

                for p_seg in prev_overlap_segs:
                    p_master_spk = p_seg["speaker"]
                    for l_seg in l_segs:
                        ov_start = max(l_seg["start"], p_seg["start"])
                        ov_end = min(l_seg["end"], p_seg["end"])
                        ov_len = max(0.0, ov_end - ov_start)
                        if ov_len > best_match_score:
                            best_match_score = ov_len
                            best_match_master_spk = p_master_spk

                if best_match_master_spk and best_match_score > 0.5:
                    local_to_master[l_spk] = best_match_master_spk
                else:
                    local_to_master[l_spk] = l_spk

        # Apply speaker mapping
        mapped_segs = []
        for s in abs_c_segs:
            s_copy = dict(s)
            s_copy["speaker"] = local_to_master.get(s["speaker"], s["speaker"])
            mapped_segs.append(s_copy)

        # Merge segments into master list
        if idx == 0:
            master_segments.extend(mapped_segs)
        else:
            cutoff = c_start + (overlap_sec / 2.0)
            master_segments = [s for s in master_segments if s["end"] <= cutoff]
            new_segs = [s for s in mapped_segs if s["start"] >= cutoff]
            master_segments.extend(new_segs)

        prev_chunk_segs = mapped_segs

    # Renumber segment IDs
    for i, s in enumerate(master_segments):
        s["id"] = i

    all_speakers = sorted(list(set(s["speaker"] for s in master_segments)))
    return {
        "task": "diarize",
        "duration": total_sec,
        "num_speakers": len(all_speakers),
        "segments": master_segments,
        "speakers": [{"id": spk, "label": spk.replace("SPEAKER_", "")} for spk in all_speakers]
    }


def main():
    parser = argparse.ArgumentParser(description="Split meeting recording into per-speaker audio files using LocalAI diarization.")
    parser.add_argument("input", nargs="?", default=DEFAULT_INPUT, help="Path to input audio file")
    parser.add_argument("--endpoint", "-e", default=DEFAULT_ENDPOINT, help="LocalAI diarization API endpoint URL")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL, help="LocalAI model name")
    parser.add_argument("--output-dir", "-o", help="Output directory for split audio files")
    parser.add_argument("--api-key", default="", help="API key if endpoint requires authentication")
    parser.add_argument("--load-json", help="Path to pre-existing diarization JSON file to skip API call")
    parser.add_argument("--save-json", help="Path to save diarization JSON output")
    parser.add_argument("--chunk-size", type=int, default=600, help="Chunk size in seconds (default: 600s = 10 min)")
    parser.add_argument("--mode", choices=["timeline", "concat"], default="timeline",
                        help="'timeline': keeps full original timing with silence during non-speech turns (best for audio_server.py sync playback). 'concat': concatenates speech segments only.")

    args = parser.parse_args()

    input_path = Path(os.path.expanduser(args.input)).resolve()
    if not input_path.exists():
        print(f"Error: input file {input_path} does not exist.", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(os.path.expanduser(args.output_dir)).resolve() if args.output_dir else input_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    input_stem = input_path.stem
    json_save_path = Path(args.save_json).resolve() if args.save_json else out_dir / f"{input_stem}_diarization.json"

    # Step 1: Get Diarization Result
    if args.load_json:
        load_path = Path(os.path.expanduser(args.load_json)).resolve()
        print(f"-> Loading cached diarization JSON from {load_path}...")
        with load_path.open("r", encoding="utf-8") as f:
            diar_json = json.load(f)
    elif json_save_path.exists():
        print(f"-> Found existing diarization JSON at {json_save_path}, loading cached result...")
        print("   (Note: delete this file or specify --save-json to re-run diarization)")
        with json_save_path.open("r", encoding="utf-8") as f:
            diar_json = json.load(f)
    else:
        diar_json = diarize_audio_chunked(
            input_path,
            args.endpoint,
            args.model,
            api_key=args.api_key,
            chunk_size_sec=args.chunk_size
        )
        print(f"\n-> Diarization completed. Saving JSON output to {json_save_path}...")
        with json_save_path.open("w", encoding="utf-8") as f:
            json.dump(diar_json, f, indent=2, ensure_ascii=False)

    segments = diar_json.get("segments", [])
    if not segments:
        print("Error: No segments found in diarization result.", file=sys.stderr)
        sys.exit(1)

    # Summarize speakers
    speakers = sorted(list(set(seg.get("speaker", "UNKNOWN") for seg in segments)))
    print(f"\n-> Diarization Summary:")
    print(f"   Total Duration : {diar_json.get('duration', 0):.2f}s ({diar_json.get('duration', 0)/60:.2f} min)")
    print(f"   Total Segments : {len(segments)}")
    print(f"   Speakers Found : {len(speakers)} ({', '.join(speakers)})")

    speaker_stats = {spk: {"count": 0, "duration": 0.0} for spk in speakers}
    for seg in segments:
        spk = seg.get("speaker", "UNKNOWN")
        dur = max(0.0, seg.get("end", 0.0) - seg.get("start", 0.0))
        speaker_stats[spk]["count"] += 1
        speaker_stats[spk]["duration"] += dur

    for spk, stats in speaker_stats.items():
        print(f"   - {spk}: {stats['count']} segments, {stats['duration']:.2f}s ({stats['duration']/60:.2f} min) speech")

    # Step 2: Load input audio
    pcm_samples, sample_rate = load_audio_pcm(input_path)
    total_samples_len = len(pcm_samples)
    audio_duration_sec = total_samples_len / sample_rate

    print(f"\n-> Splitting audio into per-speaker files (Mode: {args.mode})...")

    created_files = []

    for idx, spk in enumerate(speakers):
        spk_segments = [s for s in segments if s.get("speaker") == spk]

        clean_spk_id = spk.lower()
        output_filename = f"{input_stem}_{clean_spk_id}.wav"
        output_filepath = out_dir / output_filename

        if args.mode == "timeline":
            # Timeline-preserving: initialize array with silence, copy speaking turns
            spk_pcm = np.zeros(total_samples_len, dtype=np.int16)
            for seg in spk_segments:
                start_sec = max(0.0, seg.get("start", 0.0))
                end_sec = min(audio_duration_sec, seg.get("end", 0.0))
                start_idx = int(round(start_sec * sample_rate))
                end_idx = int(round(end_sec * sample_rate))
                if start_idx < end_idx and start_idx < total_samples_len:
                    end_idx = min(end_idx, total_samples_len)
                    spk_pcm[start_idx:end_idx] = pcm_samples[start_idx:end_idx]
        else:
            # Concat mode: concatenate speech segments only
            chunks = []
            for seg in spk_segments:
                start_sec = max(0.0, seg.get("start", 0.0))
                end_sec = min(audio_duration_sec, seg.get("end", 0.0))
                start_idx = int(round(start_sec * sample_rate))
                end_idx = int(round(end_sec * sample_rate))
                if start_idx < end_idx and start_idx < total_samples_len:
                    end_idx = min(end_idx, total_samples_len)
                    chunks.append(pcm_samples[start_idx:end_idx])
            spk_pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)

        save_audio_pcm(output_filepath, spk_pcm, sample_rate)
        created_files.append(output_filepath)

        # Also create standard alias named speaker_00.wav, speaker_01.wav in out_dir
        alias_filename = f"speaker_{idx:02d}.wav"
        alias_filepath = out_dir / alias_filename
        if alias_filepath != output_filepath:
            save_audio_pcm(alias_filepath, spk_pcm, sample_rate)
            print(f"   -> Standard alias: {alias_filepath}")

    print("\n=======================================================")
    print(" Audio Splitting Complete!")
    print(f" Output files saved to: {out_dir}")
    for f in created_files:
        print(f"   - {f}")
    print("\n Usage in audio_server.py:")
    print(f" 1. Run: python3 audio_server.py --dir {out_dir}")
    print(" 2. Open http://localhost:8000")
    print(" 3. Assign Channel 1 -> speaker_00.wav (or SPEAKER_00 track)")
    print(" 4. Assign Channel 2 -> speaker_01.wav (or SPEAKER_01 track)")
    print(" 5. Click '▶ Play All Channels' to simulate multi-channel meeting!")
    print("=======================================================\n")


if __name__ == "__main__":
    main()
