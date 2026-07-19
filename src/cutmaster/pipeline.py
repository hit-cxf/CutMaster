from __future__ import annotations

import json
import time

from loguru import logger

from cutmaster.asr import prepare_subtitles
from cutmaster.beats import detect_beats
from cutmaster.cuts import optimize_script_source_windows
from cutmaster.dialogue import postprocess_dialogues
from cutmaster.models import AppConfig, PipelineResult, RunRequest
from cutmaster.renderer import media_duration, render_montage
from cutmaster.script import adapt_script, generate_script, script_duration, write_script


def _validate_request(request: RunRequest) -> None:
    if not request.video_path.is_file():
        raise FileNotFoundError(f"Video not found: {request.video_path}")
    if not request.audio_path.is_file():
        raise FileNotFoundError(f"BGM not found: {request.audio_path}")
    if not request.prompt.strip():
        raise ValueError("Prompt must not be empty")
    if request.target_output_length_sec <= 0:
        raise ValueError("Target output duration must be positive")
    if request.target_shot_length_sec <= 0:
        raise ValueError("Target shot duration must be positive")
    if request.max_clip_duration_sec is not None and request.max_clip_duration_sec <= 0:
        raise ValueError("Maximum clip duration must be positive")


def run_pipeline(request: RunRequest, config: AppConfig) -> PipelineResult:
    _validate_request(request)
    output_dir = request.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    final_output = output_dir / "output.mp4"
    if final_output.exists() and not request.overwrite:
        raise FileExistsError(f"Output already exists; pass --overwrite to replace it: {final_output}")

    raw_script_path = output_dir / "script_raw.json"
    adapted_script_path = output_dir / "script_adapted.json"
    result_path = output_dir / "result.json"
    timings: dict[str, float] = {}
    started = time.monotonic()

    stage_started = time.monotonic()
    source_srt = prepare_subtitles(
        request.video_path,
        output_dir,
        config.asr,
        request.subtitle_path,
    )
    timings["subtitle_preparation"] = time.monotonic() - stage_started

    stage_started = time.monotonic()
    processed_subtitle, dialogues_json = postprocess_dialogues(
        source_srt,
        output_dir,
        config.llm,
    )
    timings["dialogue_postprocessing"] = time.monotonic() - stage_started

    stage_started = time.monotonic()
    raw_script = generate_script(request, processed_subtitle, config.llm)
    write_script(raw_script_path, raw_script)
    timings["script_generation"] = time.monotonic() - stage_started

    stage_started = time.monotonic()
    beat_times = detect_beats(request.audio_path, request.target_output_length_sec)
    timings["beat_detection"] = time.monotonic() - stage_started

    stage_started = time.monotonic()
    source_duration = media_duration(request.video_path)
    adapted_script = adapt_script(
        raw_script,
        request.target_output_length_sec,
        request.target_shot_length_sec,
        request.max_clip_duration_sec,
        beat_times,
        source_duration,
        config.render.fps,
    )
    timings["script_adaptation"] = time.monotonic() - stage_started

    stage_started = time.monotonic()
    adapted_script = optimize_script_source_windows(
        request.video_path,
        adapted_script,
        beat_times,
        source_duration,
        output_fps=config.render.fps,
        max_workers=config.render.threads,
    )
    write_script(adapted_script_path, adapted_script)
    timings["source_cut_optimization"] = time.monotonic() - stage_started

    stage_started = time.monotonic()
    montage_path, output_path = render_montage(
        request.video_path,
        request.audio_path,
        adapted_script,
        output_dir,
        config.render,
    )
    timings["rendering"] = time.monotonic() - stage_started

    result = PipelineResult(
        status="success",
        output_video=str(output_path),
        source_srt=str(source_srt),
        processed_subtitle=str(processed_subtitle),
        dialogues_json=str(dialogues_json),
        raw_script=str(raw_script_path),
        adapted_script=str(adapted_script_path),
        montage_video=str(montage_path),
        target_output_length_sec=request.target_output_length_sec,
        actual_output_length_sec=media_duration(output_path),
        raw_script_duration_sec=script_duration(raw_script),
        adapted_script_duration_sec=script_duration(adapted_script),
        num_raw_clips=len(raw_script),
        num_adapted_clips=len(adapted_script),
        stage_timings_sec=timings,
        wall_clock_sec=time.monotonic() - started,
    )
    result_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.success("CutMaster pipeline complete: {}", output_path)
    return result
