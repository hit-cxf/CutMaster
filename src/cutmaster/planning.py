from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cv2

from cutmaster.models import LLMConfig, PlanningConfig, RunRequest
from cutmaster.planning_context import PlanningContext
from cutmaster.script import subtitle_ranges
from cutmaster.timecode import format_range, parse_range


PLANNER_SYSTEM = (
    "You are the planning component of a professional video editor. "
    "Plan an output timeline but do not select source timestamps. Return strict JSON only."
)
RETRIEVER_SYSTEM = (
    "You retrieve real source-video passages from timestamped ASR subtitles. "
    "Never invent timestamps or dialogue. Return strict JSON only."
)
REVIEW_SYSTEM = (
    "You review a structured edit timeline and return minimal patch operations. "
    "Use only candidate IDs supplied in the maintained context. Return strict JSON only."
)


def _request_metadata(request: RunRequest, clip_count: int) -> dict[str, Any]:
    return {
        "instruction": request.prompt,
        "prompt_type": request.prompt_type,
        "video_title": request.video_title or request.video_path.stem,
        "target_duration_sec": request.target_output_length_sec,
        "target_shot_length_sec": request.target_shot_length_sec,
        "clip_count": clip_count,
    }


def _validate_slots(parsed: dict[str, Any], clip_count: int) -> list[dict[str, Any]]:
    raw = parsed.get("slots")
    if not isinstance(raw, list) or len(raw) != clip_count:
        raise ValueError(f"Expected exactly {clip_count} edit slots")
    slots: list[dict[str, Any]] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Slot {index} must be an object")
        description = str(item.get("content_description") or "").strip()
        if not description:
            raise ValueError(f"Slot {index} has no content description")
        duration = float(item.get("desired_duration_sec") or 0)
        if duration <= 0:
            raise ValueError(f"Slot {index} has invalid desired duration")
        slots.append(
            {
                "slot_id": f"slot_{index:02d}",
                "narrative_role": str(item.get("narrative_role") or "development"),
                "content_description": description,
                "target_emotion": str(item.get("target_emotion") or "neutral"),
                "target_emotional_intensity": max(
                    0.0, min(1.0, float(item.get("target_emotional_intensity", 0.5)))
                ),
                "target_kinetic_energy": max(
                    0.0, min(1.0, float(item.get("target_kinetic_energy", 0.5)))
                ),
                "desired_duration_sec": duration,
                "continuity_from_previous": str(item.get("continuity_from_previous") or ""),
            }
        )
    return slots


def plan_edit_slots(
    request: RunRequest,
    music_profile: dict[str, Any],
    config: LLMConfig,
    context: PlanningContext,
) -> list[dict[str, Any]]:
    clip_count = request.custom_clips or max(
        1, math.ceil(request.target_output_length_sec / request.target_shot_length_sec)
    )
    context.set_artifact("request", _request_metadata(request, clip_count))
    context.set_artifact("music_profile", music_profile)
    prompt = f"""Create exactly {clip_count} sequential edit slots for the maintained request and music profile.

Each slot describes WHAT should appear, not WHERE it appears in the source.
Use the music sections and energy curve to vary desired duration: high kinetic energy generally uses shorter clips; low energy uses longer clips.
Maintain a coherent progression. Every slot after the first must explain how it continues or contrasts with the previous slot.
The desired durations should total approximately {request.target_output_length_sec:.1f} seconds.

Return:
{{"slots":[{{
  "narrative_role":"setup|development|turning_point|climax|resolution",
  "content_description":"people, visible action, emotion, setting, editorial purpose",
  "target_emotion":"short label",
  "target_emotional_intensity":0.0,
  "target_kinetic_energy":0.0,
  "desired_duration_sec":4.0,
  "continuity_from_previous":"semantic or visual relationship"
}}]}}"""
    return context.call_json(
        operation="Edit slot planning",
        prompt=prompt,
        config=config,
        context_keys=["request", "music_profile"],
        system_prompt=PLANNER_SYSTEM,
        enable_thinking=True,
        validate=lambda parsed: _validate_slots(parsed, clip_count),
        output_artifact="edit_plan_unaligned",
    )


def align_slots_to_music(
    slots: list[dict[str, Any]],
    music_profile: dict[str, Any],
    total_duration_sec: float,
    output_fps: int,
) -> list[dict[str, Any]]:
    weights = [max(0.1, float(slot["desired_duration_sec"])) for slot in slots]
    scale = total_duration_sec / sum(weights)
    elapsed = 0.0
    ideal: list[float] = []
    for weight in weights[:-1]:
        elapsed += weight * scale
        ideal.append(elapsed)
    accents = music_profile.get("accents_sec") or []
    try:
        boundaries = _globally_align_boundaries(
            ideal, accents, total_duration_sec, output_fps
        )
    except ValueError:
        boundaries = _globally_align_boundaries(
            ideal, music_profile.get("beats_sec", []), total_duration_sec, output_fps
        )
    edges = [0.0, *boundaries, total_duration_sec]
    aligned: list[dict[str, Any]] = []
    for slot, start, end in zip(slots, edges[:-1], edges[1:], strict=True):
        item = dict(slot)
        item["output_start_sec"] = round(start, 6)
        item["output_end_sec"] = round(end, 6)
        item["planned_duration_sec"] = round(end - start, 6)
        aligned.append(item)
    return aligned


def _globally_align_boundaries(
    ideal: list[float],
    candidates: list[float],
    total_duration_sec: float,
    output_fps: int,
    min_clip_duration_sec: float = 1.5,
) -> list[float]:
    if not ideal:
        return []
    values = sorted(
        {
            round(float(value) * output_fps) / output_fps
            for value in candidates
            if min_clip_duration_sec
            <= float(value)
            <= total_duration_sec - min_clip_duration_sec
        }
    )
    if len(values) < len(ideal):
        raise ValueError("Not enough musical accents to align all edit boundaries")
    states: dict[int, tuple[float, list[float]]] = {}
    for index, value in enumerate(values):
        if value >= min_clip_duration_sec:
            states[index] = (abs(value - ideal[0]), [value])
    for boundary_index in range(1, len(ideal)):
        next_states: dict[int, tuple[float, list[float]]] = {}
        for index, value in enumerate(values):
            best: tuple[float, list[float]] | None = None
            for previous_index, (cost, path) in states.items():
                if previous_index >= index or value - path[-1] < min_clip_duration_sec:
                    continue
                proposal = (cost + abs(value - ideal[boundary_index]), [*path, value])
                if best is None or proposal[0] < best[0]:
                    best = proposal
            if best is not None:
                next_states[index] = best
        states = next_states
        if not states:
            raise ValueError("No monotonic musical-boundary path satisfies minimum clip length")
    feasible = [
        state
        for state in states.values()
        if total_duration_sec - state[1][-1] >= min_clip_duration_sec
    ]
    if not feasible:
        raise ValueError("No musical-boundary path leaves room for the final clip")
    return min(feasible, key=lambda state: state[0])[1]


def _validate_candidates(
    parsed: dict[str, Any],
    slots: list[dict[str, Any]],
    subtitle_content: str,
    per_slot: int,
) -> dict[str, list[dict[str, Any]]]:
    valid_ranges = subtitle_ranges(subtitle_content)
    allowed_slots = {slot["slot_id"] for slot in slots}
    slots_by_id = {slot["slot_id"]: slot for slot in slots}
    result: dict[str, list[dict[str, Any]]] = {slot_id: [] for slot_id in allowed_slots}
    groups = parsed.get("candidates")
    if not isinstance(groups, list):
        raise ValueError("Candidate response must contain a candidates array")
    for group in groups:
        slot_id = str(group.get("slot_id") or "")
        if slot_id not in allowed_slots:
            raise ValueError(f"Unknown slot ID: {slot_id}")
        for raw in group.get("items") or []:
            start, end = parse_range(str(raw.get("timestamp") or ""))
            if not any(start < cue_end and end > cue_start for cue_start, cue_end in valid_ranges):
                raise ValueError(f"Candidate for {slot_id} has no ASR evidence")
            if end - start + 1e-6 < float(slots_by_id[slot_id]["planned_duration_sec"]):
                continue
            result[slot_id].append(
                {
                    "candidate_id": f"{slot_id}_candidate_{len(result[slot_id]) + 1:02d}",
                    "slot_id": slot_id,
                    "timestamp": format_range(start, end),
                    "description": str(raw.get("description") or "").strip(),
                    "matched_dialogue": str(raw.get("matched_dialogue") or "").strip(),
                    "semantic_relevance": max(
                        0.0, min(1.0, float(raw.get("semantic_relevance", 0.5)))
                    ),
                    "emotional_intensity": max(
                        0.0, min(1.0, float(raw.get("emotional_intensity", 0.5)))
                    ),
                    "salience": max(0.0, min(1.0, float(raw.get("salience", 0.5)))),
                }
            )
        result[slot_id] = result[slot_id][:per_slot]
        if not result[slot_id]:
            raise ValueError(f"No valid candidates returned for {slot_id}")
    return result


def retrieve_candidates(
    slots: list[dict[str, Any]],
    subtitle_path: Path,
    video_path: Path,
    config: LLMConfig,
    planning_config: PlanningConfig,
    context: PlanningContext,
) -> dict[str, list[dict[str, Any]]]:
    subtitle_content = subtitle_path.read_text(encoding="utf-8-sig")
    batches = [
        slots[index : index + planning_config.retrieval_batch_size]
        for index in range(0, len(slots), planning_config.retrieval_batch_size)
    ]
    pool: dict[str, list[dict[str, Any]]] = {}
    for batch_index, batch in enumerate(batches, 1):
        prompt = f"""Retrieve exactly {planning_config.candidates_per_slot} distinct source candidates for every supplied edit slot.
Candidates must be grounded in the ASR subtitles below. Return enough surrounding time for the slot's planned duration.
Score semantic relevance, emotional intensity, and event salience from 0 to 1.

<slots>
{json.dumps(batch, ensure_ascii=False)}
</slots>
<subtitles>
{subtitle_content}
</subtitles>

Return:
{{"candidates":[{{"slot_id":"slot_01","items":[{{
  "timestamp":"00:00:01,000-00:00:08,000",
  "description":"visible content inferred from the dialogue context",
  "matched_dialogue":"quoted ASR evidence",
  "semantic_relevance":0.9,
  "emotional_intensity":0.7,
  "salience":0.8
}}]}}]}}"""
        batch_pool = context.call_json(
            operation=f"Candidate retrieval batch {batch_index}/{len(batches)}",
            prompt=prompt,
            config=config,
            context_keys=["request"],
            system_prompt=RETRIEVER_SYSTEM,
            enable_thinking=True,
            validate=lambda parsed, batch=batch: _validate_candidates(
                parsed,
                batch,
                subtitle_content,
                planning_config.candidates_per_slot,
            ),
        )
        pool.update(batch_pool)
    add_kinetic_features(
        video_path,
        pool,
        planning_config.motion_sample_fps,
        planning_config.motion_workers,
    )
    context.set_artifact("candidate_pool", pool)
    return pool


def _candidate_motion(video: cv2.VideoCapture, start: float, end: float, fps: float) -> float:
    values: list[float] = []
    previous = None
    sample_step = 1.0 / max(fps, 0.1)
    next_sample = start
    video.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    while True:
        ok, frame = video.read()
        if not ok:
            break
        time_sec = float(video.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0
        if time_sec >= end:
            break
        if time_sec + 1e-6 < next_sample:
            continue
        gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
        if previous is not None:
            values.append(float(cv2.absdiff(gray, previous).mean()) / 255.0)
        previous = gray
        next_sample += sample_step
    if not values:
        return 0.0
    raw = sum(values) / len(values)
    return max(0.0, min(1.0, raw / 0.18))


def add_kinetic_features(
    video_path: Path,
    pool: dict[str, list[dict[str, Any]]],
    sample_fps: float,
    max_workers: int = 4,
) -> None:
    candidates = [candidate for values in pool.values() for candidate in values]
    worker_count = max(1, min(max_workers, len(candidates)))
    groups = [candidates[index::worker_count] for index in range(worker_count)]

    def process(group: list[dict[str, Any]]) -> None:
        video = cv2.VideoCapture(str(video_path))
        if not video.isOpened():
            raise RuntimeError(f"Could not open source video: {video_path}")
        try:
            for candidate in group:
                start, end = parse_range(candidate["timestamp"])
                candidate["kinetic_energy"] = round(
                    _candidate_motion(video, start, end, sample_fps), 4
                )
        finally:
            video.release()

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        list(executor.map(process, groups))


def _words(text: str) -> set[str]:
    return set(re.findall(r"[\w\u4e00-\u9fff]+", text.lower()))


def _unary(slot: dict[str, Any], candidate: dict[str, Any]) -> float:
    emotion_match = 1.0 - abs(
        float(slot["target_emotional_intensity"]) - float(candidate["emotional_intensity"])
    )
    kinetic_match = 1.0 - abs(
        float(slot["target_kinetic_energy"]) - float(candidate["kinetic_energy"])
    )
    duration = parse_range(candidate["timestamp"])[1] - parse_range(candidate["timestamp"])[0]
    duration_match = min(1.0, duration / max(0.1, float(slot["planned_duration_sec"])))
    return (
        0.42 * float(candidate["semantic_relevance"])
        + 0.20 * emotion_match
        + 0.20 * kinetic_match
        + 0.10 * float(candidate["salience"])
        + 0.08 * duration_match
    )


def _pairwise(
    previous_slot: dict[str, Any],
    previous: dict[str, Any],
    slot: dict[str, Any],
    current: dict[str, Any],
) -> float:
    prev_start, prev_end = parse_range(previous["timestamp"])
    start, end = parse_range(current["timestamp"])
    if start < prev_end and end > prev_start:
        return -math.inf
    chronology = 1.0 if start >= prev_start else 0.25
    energy_flow = 1.0 - abs(
        (float(slot["target_kinetic_energy"]) - float(previous_slot["target_kinetic_energy"]))
        - (float(current["kinetic_energy"]) - float(previous["kinetic_energy"]))
    )
    previous_words = _words(previous["description"])
    current_words = _words(current["description"])
    union = previous_words | current_words
    semantic_bridge = len(previous_words & current_words) / len(union) if union else 0.0
    return 0.45 * chronology + 0.35 * max(0.0, energy_flow) + 0.20 * semantic_bridge


def select_paths(
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
    beam_width: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    greedy = [
        max(pool[slot["slot_id"]], key=lambda item: _unary(slot, item))
        for slot in slots
    ]
    beams: list[tuple[float, list[dict[str, Any]]]] = [(0.0, [])]
    for index, slot in enumerate(slots):
        expanded: list[tuple[float, list[dict[str, Any]]]] = []
        for score, path in beams:
            for candidate in pool[slot["slot_id"]]:
                start, end = parse_range(candidate["timestamp"])
                if any(
                    start < existing_end and end > existing_start
                    for existing_start, existing_end in (
                        parse_range(item["timestamp"]) for item in path
                    )
                ):
                    continue
                pair = (
                    _pairwise(slots[index - 1], path[-1], slot, candidate)
                    if path
                    else 0.0
                )
                if math.isinf(pair) and pair < 0:
                    continue
                expanded.append((score + _unary(slot, candidate) + 0.35 * pair, [*path, candidate]))
        if not expanded:
            raise ValueError(f"No non-overlapping beam remains at {slot['slot_id']}")
        beams = sorted(expanded, key=lambda item: item[0], reverse=True)[:beam_width]
    best_score, best = beams[0]
    diagnostics = {
        "beam_score": round(best_score, 6),
        "beam_width": beam_width,
        "greedy_candidate_ids": [item["candidate_id"] for item in greedy],
        "beam_candidate_ids": [item["candidate_id"] for item in best],
    }
    return greedy, best, diagnostics


def path_to_script(
    slots: list[dict[str, Any]],
    path: list[dict[str, Any]],
    video_path: Path,
) -> list[dict[str, Any]]:
    script: list[dict[str, Any]] = []
    output_cursor = 0.0
    for index, (slot, candidate) in enumerate(zip(slots, path, strict=True), 1):
        output_start = float(slot.get("output_start_sec", output_cursor))
        output_end = float(
            slot.get(
                "output_end_sec",
                output_start + float(slot["planned_duration_sec"]),
            )
        )
        script.append(
            {
                "_id": index,
                "video_id": 1,
                "video_name": video_path.name,
                "timestamp": candidate["timestamp"],
                "picture": slot["content_description"],
                "narration": f"播放原片{index}",
                "OST": 1,
                "slot_id": slot["slot_id"],
                "candidate_id": candidate["candidate_id"],
                "output_start_sec": output_start,
                "output_end_sec": output_end,
                "planned_duration_sec": slot["planned_duration_sec"],
                "selection_scores": {
                    "unary": round(_unary(slot, candidate), 6),
                    "semantic_relevance": candidate["semantic_relevance"],
                    "emotional_intensity": candidate["emotional_intensity"],
                    "kinetic_energy": candidate["kinetic_energy"],
                    "salience": candidate["salience"],
                },
            }
        )
        output_cursor = output_end
    return script


def _validate_patches(
    parsed: dict[str, Any],
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    valid = {
        slot["slot_id"]: {item["candidate_id"] for item in pool[slot["slot_id"]]}
        for slot in slots
    }
    patches = parsed.get("patches") or []
    if not isinstance(patches, list):
        raise ValueError("patches must be an array")
    normalized: list[dict[str, Any]] = []
    for patch in patches:
        operation = str(patch.get("operation") or "").lower()
        slot_id = str(patch.get("slot_id") or "")
        if operation == "keep":
            continue
        if operation != "replace" or slot_id not in valid:
            raise ValueError(f"Unsupported patch: {patch}")
        candidate_id = str(patch.get("candidate_id") or "")
        if candidate_id not in valid[slot_id]:
            raise ValueError(f"Invalid candidate for {slot_id}: {candidate_id}")
        normalized.append(
            {
                "operation": "replace",
                "slot_id": slot_id,
                "candidate_id": candidate_id,
                "reason": str(patch.get("reason") or ""),
            }
        )
    return normalized


def review_and_patch(
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
    script: list[dict[str, Any]],
    config: LLMConfig,
    context: PlanningContext,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prompt = """Review the current script as a sequence, focusing on instruction coverage, music-energy fit, temporal progression, and adjacent-clip continuity.
Return only minimal replacements that clearly improve the full path. Do not change a slot merely for variety.
Return {"patches":[{"operation":"keep|replace","slot_id":"slot_01","candidate_id":"slot_01_candidate_02","reason":"short reason"}]}."""
    patches = context.call_json(
        operation="Script patch review",
        prompt=prompt,
        config=config,
        context_keys=["request", "music_profile", "edit_plan", "candidate_pool", "current_script"],
        system_prompt=REVIEW_SYSTEM,
        enable_thinking=True,
        validate=lambda parsed: _validate_patches(parsed, slots, pool),
        output_artifact="latest_patches",
    )
    by_slot = {item["slot_id"]: item for item in script}
    candidates = {
        candidate["candidate_id"]: candidate
        for values in pool.values()
        for candidate in values
    }
    for patch in patches:
        slot_id = patch["slot_id"]
        slot = next(value for value in slots if value["slot_id"] == slot_id)
        replacement = path_to_script([slot], [candidates[patch["candidate_id"]]], Path(script[0]["video_name"]))[0]
        replacement["_id"] = by_slot[slot_id]["_id"]
        replacement["video_name"] = by_slot[slot_id]["video_name"]
        replacement["narration"] = by_slot[slot_id]["narration"]
        by_slot[slot_id] = replacement
    patched = [by_slot[slot["slot_id"]] for slot in slots]
    ranges = [parse_range(item["timestamp"]) for item in patched]
    if any(
        start < other_end and end > other_start
        for index, (start, end) in enumerate(ranges)
        for other_start, other_end in ranges[:index]
    ):
        patched = script
        patches = []
    context.record_script_version(patched, source="llm_patch", patches=patches)
    return patched, patches
