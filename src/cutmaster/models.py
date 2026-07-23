from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LLMConfig:
    model: str
    base_url: str
    api_key: str
    temperature: float = 0.1
    max_tokens: int = 4000
    timeout_sec: float = 180.0
    max_retries: int = 3
    max_concurrency: int = 4


@dataclass(frozen=True)
class ASRConfig:
    backend: str
    api_key: str
    reuse: bool = True
    timeout_sec: float = 1800.0
    poll_interval_sec: float = 2.0
    max_chars: int = 20
    max_subtitle_duration_sec: float = 3.5


@dataclass(frozen=True)
class RenderConfig:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    encoder: str = "auto"
    threads: int = 8
    bgm_volume: float = 0.3
    original_volume: float = 0.0
    audio_sample_rate: int = 48000


@dataclass(frozen=True)
class PlanningConfig:
    candidates_per_slot: int = 4
    retrieval_batch_size: int = 5
    beam_width: int = 8
    review_rounds: int = 1
    motion_sample_fps: float = 2.0
    motion_workers: int = 4


@dataclass(frozen=True)
class AppConfig:
    llm: LLMConfig
    asr: ASRConfig
    render: RenderConfig
    planning: PlanningConfig = PlanningConfig()


@dataclass(frozen=True)
class RunRequest:
    video_path: Path
    audio_path: Path
    prompt: str
    output_dir: Path
    target_output_length_sec: float = 60.0
    target_shot_length_sec: float = 4.0
    prompt_type: str = "event"
    video_title: str = ""
    subtitle_path: Path | None = None
    custom_clips: int | None = None
    max_clip_duration_sec: float | None = None
    overwrite: bool = False


@dataclass(frozen=True)
class PipelineResult:
    status: str
    output_video: str
    source_srt: str
    processed_subtitle: str
    dialogues_json: str
    music_profile: str
    planning_history: str
    edit_plan: str
    candidate_pool: str
    raw_script: str
    adapted_script: str
    montage_video: str
    target_output_length_sec: float
    actual_output_length_sec: float
    raw_script_duration_sec: float
    adapted_script_duration_sec: float
    num_raw_clips: int
    num_adapted_clips: int
    stage_timings_sec: dict[str, float]
    wall_clock_sec: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
