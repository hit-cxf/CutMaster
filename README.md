# CutMaster

CutMaster is a backend-only long-video montage pipeline. Its first version
extracts the production flow used by the Mashup-Benchmark NarratoAI adapter and
turns it into an independent, maintainable Python project.

## Current pipeline

```text
source video
  -> reuse supplied SRT or transcribe with DashScope Fun-ASR
  -> reconstruct complete dialogue sentences in parallel and preserve cue-level time anchors
  -> ask an OpenAI-compatible LLM to select timestamped source segments
  -> detect strong BGM energy peaks
  -> validate the script and align montage cut boundaries to nearby audio peaks
  -> detect internal source-video cuts and search each source window up to 2s forward in parallel
  -> prefer more than 1s between every retained internal cut and either clip boundary;
     infeasible windows use logged 0.75s/0.5s/0.25s fallback tiers instead of failing the task
  -> render each clip from its exact output-frame range before concatenation
  -> FFmpeg trim and normalize every OST=1 segment
  -> concatenate clips
  -> loop and fade the specified BGM while muting source audio in the final output
  -> output.mp4
```

The migration deliberately excludes Streamlit, NarratoAI's UI state, web task
queues, TTS, narration subtitles, stock-material search and benchmark-specific
run records. CutMaster does not import or require the NarratoAI repository.

## Install

Python 3.12+, `ffmpeg`, `ffprobe`, and `uv` are required.

```bash
uv sync
cp config.example.toml config.toml
```

Set the API key referenced by `api_key_env` in `config.toml`:

```bash
export DASHSCOPE_API_KEY="..."
```

## Run

```bash
uv run cutmaster run \
  --video /path/to/source.mp4 \
  --audio /path/to/bgm.mp3 \
  --prompt "Create a montage of every decisive goal" \
  --output-dir outputs/demo \
  --target-duration 60 \
  --target-shot-length 4 \
  --prompt-type event
```

`--custom-clips` overrides the automatically calculated segment count.
`--max-clip-duration` optionally imposes a strict rendered-clip cap; without
it, clip lengths can move slightly around the target shot length to reach audio peaks.

To bypass ASR and use an existing subtitle file, add:

```bash
--subtitle /path/to/source.srt
```

Existing `output.mp4` files are protected by default. Use `--overwrite` only
when the run should replace an existing result.

## Output artifacts

Each run directory contains:

- `source.srt`: reused or generated source subtitles.
- `dialogues.json`: complete dialogue sentences with aggregate ranges and original cue anchors.
- `dialogue_merged.srt`: sentence-level subtitles used for montage-script generation.
- `source_audio.m4a`: 16 kHz mono ASR input when transcription is needed.
- `script_raw.json`: validated LLM-selected source ranges.
- `script_adapted.json`: frame-grid output ranges, beat-aligned timestamps, forward-refined source ranges, and internal-cut diagnostics.
- `clips/`: normalized intermediate clips.
- `montage.mp4`: concatenated original-audio montage before BGM mixing.
- `output.mp4`: final video.
- `result.json`: durations, artifact paths, clip counts, and stage timings.
- `cutmaster.log`: backend execution log.

## Backend modules

- `asr.py`: audio extraction and DashScope Fun-ASR transcription.
- `beats.py`: full-audio energy-peak detection used for cut alignment.
- `cuts.py`: parallel source-cut detection and frame-level forward-only minimax refinement with a 1-second edge constraint.
- `dialogue.py`: parallel LLM sentence reconstruction with cue-level time anchors.
- `llm.py`: OpenAI-compatible text-model client.
- `script.py`: prompt assembly, JSON validation, overlap checks and beat-aware duration adaptation.
- `renderer.py`: frame-exact, video-only FFmpeg clip rendering, lossless-timeline concatenation, and final BGM mixing.
- `pipeline.py`: end-to-end orchestration and artifact recording.
- `cli.py`: command-line backend entry point.

## Verify

```bash
uv run pytest
uv run python -m cutmaster --help
```

## Attribution

The initial workflow is derived from the MIT-licensed NarratoAI project. See
`THIRD_PARTY_NOTICES.md` and `LICENSE`.
