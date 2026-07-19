from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from cutmaster.models import ASRConfig, AppConfig, LLMConfig, RenderConfig


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"Config section [{name}] must be a table")
    return value


def _secret(section: dict[str, Any], section_name: str) -> str:
    direct = str(section.get("api_key") or "").strip()
    if direct:
        return direct
    env_name = str(section.get("api_key_env") or "").strip()
    value = os.getenv(env_name, "").strip() if env_name else ""
    if not value:
        source = f"environment variable {env_name}" if env_name else "api_key"
        raise ValueError(f"Missing [{section_name}] API key from {source}")
    return value


def load_config(path: Path) -> AppConfig:
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("rb") as handle:
        data = tomllib.load(handle)

    llm = _section(data, "llm")
    asr = _section(data, "asr")
    render = _section(data, "render")
    model = str(llm.get("model") or "").strip()
    if not model:
        raise ValueError("Missing [llm].model")

    return AppConfig(
        llm=LLMConfig(
            model=model,
            base_url=str(llm.get("base_url") or "").strip(),
            api_key=_secret(llm, "llm"),
            temperature=float(llm.get("temperature", 0.1)),
            max_tokens=int(llm.get("max_tokens", 4000)),
            timeout_sec=float(llm.get("timeout_sec", 180.0)),
            max_retries=int(llm.get("max_retries", 3)),
            max_concurrency=int(llm.get("max_concurrency", 4)),
        ),
        asr=ASRConfig(
            backend=str(asr.get("backend") or "bailian").strip().lower(),
            api_key=_secret(asr, "asr"),
            reuse=bool(asr.get("reuse", True)),
            timeout_sec=float(asr.get("timeout_sec", 1800.0)),
            poll_interval_sec=float(asr.get("poll_interval_sec", 2.0)),
            max_chars=int(asr.get("max_chars", 20)),
            max_subtitle_duration_sec=float(asr.get("max_subtitle_duration_sec", 3.5)),
        ),
        render=RenderConfig(
            width=int(render.get("width", 1920)),
            height=int(render.get("height", 1080)),
            fps=int(render.get("fps", 30)),
            encoder=str(render.get("encoder") or "auto").strip(),
            threads=int(render.get("threads", 8)),
            bgm_volume=float(render.get("bgm_volume", 0.3)),
            original_volume=float(render.get("original_volume", 0.0)),
            audio_sample_rate=int(render.get("audio_sample_rate", 48000)),
        ),
    )
