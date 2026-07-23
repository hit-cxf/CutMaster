from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from cutmaster.llm import generate_text, request_json_with_retries
from cutmaster.models import LLMConfig, RunRequest
from cutmaster.timecode import format_range, parse_range


def build_script_prompt(request: RunRequest, subtitle_content: str, clip_count: int) -> str:
    title = request.video_title or request.video_path.stem
    return f"""# Long-video montage script generation

## Goal
Select source-video segments for a music montage that follows the user's instruction.

<user_instruction>
{request.prompt}
</user_instruction>

<task_metadata>
Title: {title}
Prompt type: {request.prompt_type}
Target output duration: {request.target_output_length_sec:.1f} seconds
Target shot duration: {request.target_shot_length_sec:.1f} seconds
Requested segment count: {clip_count}
</task_metadata>

<subtitles>
{subtitle_content}
</subtitles>

## Selection rules
1. Select exactly {clip_count} segments when enough relevant material exists.
2. Prioritize direct evidence for the user instruction, then viewing clarity and narrative progression.
3. Use only timestamps present in the subtitles. Timestamp format must be HH:MM:SS,mmm-HH:MM:SS,mmm.
4. Segments must not overlap. Keep source chronology unless a deliberate montage order better serves the instruction.
5. Each selected source range should contain at least {request.target_shot_length_sec + 0.5:.1f} seconds of context when available. The adapter will trim it to a nearby music beat.
6. `picture` must briefly describe visible people, action, emotion, setting, and editorial purpose.
7. `OST` must remain 1 for script-schema compatibility and `narration` must be `播放原片+_id`; the final renderer may mute source audio and keep only the BGM.

## Output
Return strict JSON only:
{{
  "items": [
    {{
      "_id": 1,
      "video_id": 1,
      "video_name": "{request.video_path.name}",
      "timestamp": "00:00:01,000-00:00:08,000",
      "picture": "Visible action and its editorial purpose",
      "narration": "播放原片1",
      "OST": 1
    }}
  ]
}}
"""


SRT_RANGE_RE = re.compile(
    r"(?m)^\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*$"
)


def subtitle_ranges(subtitle_content: str) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    for start_raw, end_raw in SRT_RANGE_RE.findall(subtitle_content):
        ranges.append(parse_range(f"{start_raw}-{end_raw}"))
    return ranges


def normalize_script(
    raw_items: Any,
    video_path: Path,
    subtitle_content: str | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("LLM script items must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    occupied: list[tuple[float, float, int]] = []
    cues = subtitle_ranges(subtitle_content or "")
    subtitle_start = min((start for start, _ in cues), default=None)
    subtitle_end = max((end for _, end in cues), default=None)
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Script item {index} must be an object")
        timestamp = str(raw.get("timestamp") or "").strip()
        start, end = parse_range(timestamp)
        if subtitle_start is not None and subtitle_end is not None:
            if start < subtitle_start or end > subtitle_end:
                raise ValueError(f"Script item {index} is outside the subtitle timeline")
            if not any(start < cue_end and end > cue_start for cue_start, cue_end in cues):
                raise ValueError(f"Script item {index} does not overlap any subtitle cue")
        for previous_start, previous_end, previous_id in occupied:
            if start < previous_end and end > previous_start:
                raise ValueError(f"Script item {index} overlaps item {previous_id}")
        occupied.append((start, end, index))
        picture = str(raw.get("picture") or raw.get("content") or "").strip()
        if not picture:
            raise ValueError(f"Script item {index} has an empty picture description")
        normalized.append(
            {
                "_id": index,
                "video_id": 1,
                "video_name": video_path.name,
                "timestamp": format_range(start, end),
                "picture": picture,
                "narration": f"播放原片{index}",
                "OST": 1,
            }
        )
    return normalized


def generate_script(request: RunRequest, subtitle_path: Path, config: LLMConfig) -> list[dict[str, Any]]:
    subtitle_content = subtitle_path.read_text(encoding="utf-8-sig")
    if len(subtitle_content.strip()) < 10 or "-->" not in subtitle_content:
        raise ValueError(f"Subtitle is empty or has no SRT timecodes: {subtitle_path}")
    clip_count = request.custom_clips or max(
        1,
        math.ceil(request.target_output_length_sec / max(request.target_shot_length_sec, 0.1)),
    )
    prompt = build_script_prompt(request, subtitle_content, clip_count)
    return request_json_with_retries(
        lambda: generate_text(prompt, config, enable_thinking=True),
        config,
        operation="Script generation",
        validate=lambda parsed: normalize_script(
            parsed.get("items") or parsed.get("segments") or parsed.get("plot_points"),
            request.video_path,
            subtitle_content,
        ),
    )


def adapt_script(
    raw_script: list[dict[str, Any]],
    target_output_length_sec: float,
    target_shot_length_sec: float,
    max_clip_duration_sec: float | None = None,
    beat_times: list[float] | None = None,
    source_duration_sec: float | None = None,
    output_fps: int | None = None,
) -> list[dict[str, Any]]:
    planned: list[tuple[dict[str, Any], float, float]] = []
    has_aligned_timeline = all(
        "output_start_sec" in raw and "output_end_sec" in raw for raw in raw_script
    )
    total = 0.0
    default_clip_cap = float(max_clip_duration_sec or target_shot_length_sec)
    for raw in raw_script:
        remaining = target_output_length_sec - total
        if remaining <= 0.001:
            break
        start, end = parse_range(str(raw["timestamp"]))
        planned_duration = float(raw.get("planned_duration_sec") or default_clip_cap)
        if max_clip_duration_sec is not None:
            planned_duration = min(planned_duration, max_clip_duration_sec)
        duration = min(end - start, planned_duration, remaining)
        if duration <= 0.001:
            continue
        planned.append((raw, start, duration))
        total += duration
    if not planned:
        raise ValueError("No usable clips remain after script adaptation")
    if output_fps is not None:
        total = round(total * output_fps) / output_fps

    if has_aligned_timeline and len(planned) == len(raw_script):
        output_boundaries = [float(planned[0][0]["output_start_sec"])]
        output_boundaries.extend(float(raw["output_end_sec"]) for raw, _, _ in planned)
        if output_fps is not None:
            output_boundaries = [
                round(boundary * output_fps) / output_fps for boundary in output_boundaries
            ]
        if abs(output_boundaries[0]) > 1e-6:
            raise ValueError("Aligned output timeline must start at 0")
        if any(
            end <= start
            for start, end in zip(output_boundaries, output_boundaries[1:])
        ):
            raise ValueError("Aligned output timeline must be strictly increasing")
    else:
        ideal_boundaries: list[float] = []
        elapsed = 0.0
        for _, _, duration in planned[:-1]:
            elapsed += duration
            ideal_boundaries.append(elapsed)
        aligned_boundaries = align_cut_boundaries(
            ideal_boundaries,
            beat_times or [],
            total,
            max_clip_duration_sec,
            output_fps,
        )
        output_boundaries = [0.0, *aligned_boundaries, total]

    adapted: list[dict[str, Any]] = []
    for index, ((raw, source_start, _), output_start, output_end) in enumerate(
        zip(planned, output_boundaries[:-1], output_boundaries[1:], strict=True),
        start=1,
    ):
        duration = output_end - output_start
        if source_duration_sec is not None and source_start + duration > source_duration_sec:
            source_start = max(0.0, source_duration_sec - duration)
        item = dict(raw)
        item["_id"] = index
        item["timestamp"] = format_range(source_start, source_start + duration)
        item["output_timestamp"] = format_range(output_start, output_end)
        if output_fps is not None:
            item["output_frame_range"] = [
                round(output_start * output_fps),
                round(output_end * output_fps),
            ]
        item["narration"] = f"播放原片{item['_id']}"
        item["OST"] = 1
        adapted.append(item)
    return adapted


def align_cut_boundaries(
    ideal_boundaries: list[float],
    beat_times: list[float],
    total_duration_sec: float,
    max_clip_duration_sec: float | None = None,
    output_fps: int | None = None,
) -> list[float]:
    def snap_to_frame(value: float) -> float:
        return round(value * output_fps) / output_fps if output_fps else value

    if not ideal_boundaries:
        return []
    if not beat_times:
        return [snap_to_frame(boundary) for boundary in ideal_boundaries]

    beats = sorted(
        {
            snap_to_frame(float(beat))
            for beat in beat_times
            if 0.0 < beat < total_duration_sec
        }
    )
    if not beats:
        return ideal_boundaries

    aligned: list[float] = []
    previous = 0.0
    total_clips = len(ideal_boundaries) + 1
    for index, ideal in enumerate(ideal_boundaries, start=1):
        remaining_clips = total_clips - index
        lower = previous + 0.001
        upper = total_duration_sec - (remaining_clips * 0.001)
        if max_clip_duration_sec is not None:
            lower = max(lower, total_duration_sec - remaining_clips * max_clip_duration_sec)
            upper = min(upper, previous + max_clip_duration_sec)
        candidates = [beat for beat in beats if lower <= beat <= upper]
        if not candidates:
            raise ValueError(f"No audio beat can satisfy cut boundary {index} near {ideal:.3f}s")
        selected = min(candidates, key=lambda beat: (abs(beat - ideal), beat))
        aligned.append(selected)
        previous = selected
    return aligned


def script_duration(items: list[dict[str, Any]]) -> float:
    return sum(parse_range(str(item["timestamp"]))[1] - parse_range(str(item["timestamp"]))[0] for item in items)


def write_script(path: Path, items: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
