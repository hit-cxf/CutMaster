from __future__ import annotations

import array
import math
import subprocess
from pathlib import Path


def detect_beats(
    audio_path: Path,
    duration_sec: float,
    *,
    sample_rate: int = 16000,
    window_sec: float = 0.05,
) -> list[float]:
    """Detect strong audio-energy peaks and repeat them when the BGM loops."""
    process = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(audio_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"Could not decode BGM for beat detection: {detail}")

    samples = array.array("h")
    samples.frombytes(process.stdout)
    if not samples:
        raise ValueError(f"BGM contains no audio samples: {audio_path}")

    window_size = max(1, round(sample_rate * window_sec))
    rms = []
    for start in range(0, len(samples), window_size):
        chunk = samples[start : start + window_size]
        if chunk:
            rms.append(math.sqrt(sum((sample / 32768.0) ** 2 for sample in chunk) / len(chunk)))
    if len(rms) < 3:
        raise ValueError(f"BGM is too short for beat detection: {audio_path}")

    mean = sum(rms) / len(rms)
    std = math.sqrt(sum((value - mean) ** 2 for value in rms) / len(rms))
    threshold = mean + 0.35 * std
    min_gap = max(1, round(0.30 / window_sec))
    source_beats: list[float] = []
    last_index = -min_gap
    for index in range(1, len(rms) - 1):
        if index - last_index < min_gap:
            continue
        if rms[index] >= threshold and rms[index] >= rms[index - 1] and rms[index] >= rms[index + 1]:
            source_beats.append(index * window_sec)
            last_index = index
    if not source_beats:
        raise ValueError(f"Could not detect audio beats: {audio_path}")

    source_duration = len(samples) / sample_rate
    repeats = max(1, math.ceil(duration_sec / source_duration))
    return [
        beat + repeat * source_duration
        for repeat in range(repeats)
        for beat in source_beats
        if beat + repeat * source_duration < duration_sec
    ]
