from pathlib import Path

import pytest

from cutmaster.script import adapt_script, align_cut_boundaries, normalize_script, script_duration, subtitle_ranges
from cutmaster.timecode import format_time, parse_range, parse_time


def test_timecode_round_trip() -> None:
    assert parse_time("01:02:03,456") == pytest.approx(3723.456)
    assert parse_time("01:02:03.456") == pytest.approx(3723.456)
    assert format_time(3723.456) == "01:02:03,456"
    assert parse_range("00:00:01,000 --> 00:00:02,500") == (1.0, 2.5)


def test_normalize_and_adapt_script() -> None:
    raw = normalize_script(
        [
            {"timestamp": "00:00:10,000-00:00:20,000", "picture": "First event"},
            {"timestamp": "00:00:30,000-00:00:40,000", "picture": "Second event"},
        ],
        Path("source.mp4"),
    )
    adapted = adapt_script(raw, target_output_length_sec=7.0, target_shot_length_sec=4.0)
    assert len(adapted) == 2
    assert adapted[0]["timestamp"] == "00:00:10,000-00:00:14,000"
    assert adapted[1]["timestamp"] == "00:00:30,000-00:00:33,000"
    assert script_duration(adapted) == pytest.approx(7.0)
    assert all(item["OST"] == 1 for item in adapted)


def test_adapt_script_aligns_internal_cuts_to_audio_beats() -> None:
    raw = normalize_script(
        [
            {"timestamp": "00:00:10,000-00:00:15,000", "picture": "First event"},
            {"timestamp": "00:00:30,000-00:00:35,000", "picture": "Second event"},
        ],
        Path("source.mp4"),
    )
    adapted = adapt_script(
        raw,
        target_output_length_sec=8.0,
        target_shot_length_sec=4.0,
        beat_times=[3.9, 8.1],
        source_duration_sec=60.0,
    )
    assert adapted[0]["timestamp"] == "00:00:10,000-00:00:13,900"
    assert adapted[0]["output_timestamp"] == "00:00:00,000-00:00:03,900"
    assert adapted[1]["timestamp"] == "00:00:30,000-00:00:34,100"
    assert adapted[1]["output_timestamp"] == "00:00:03,900-00:00:08,000"
    assert script_duration(adapted) == pytest.approx(8.0)


def test_adapt_script_uses_output_frame_grid_as_timeline_source() -> None:
    raw = normalize_script(
        [
            {"timestamp": "00:00:10,000-00:00:15,000", "picture": "First event"},
            {"timestamp": "00:00:30,000-00:00:35,000", "picture": "Second event"},
        ],
        Path("source.mp4"),
    )
    adapted = adapt_script(
        raw,
        target_output_length_sec=8.0,
        target_shot_length_sec=4.0,
        beat_times=[4.05],
        source_duration_sec=60.0,
        output_fps=30,
    )

    assert adapted[0]["output_frame_range"] == [0, 122]
    assert adapted[1]["output_frame_range"] == [122, 240]
    assert adapted[0]["output_timestamp"] == "00:00:00,000-00:00:04,067"
    assert adapted[1]["output_timestamp"] == "00:00:04,067-00:00:08,000"


def test_align_cut_boundaries_honors_explicit_clip_cap() -> None:
    with pytest.raises(ValueError, match="No audio beat"):
        align_cut_boundaries([4.0], [3.9, 4.1], 8.0, max_clip_duration_sec=4.0)


def test_overlapping_script_is_rejected() -> None:
    with pytest.raises(ValueError, match="overlaps"):
        normalize_script(
            [
                {"timestamp": "00:00:10,000-00:00:15,000", "picture": "A"},
                {"timestamp": "00:00:14,000-00:00:18,000", "picture": "B"},
            ],
            Path("source.mp4"),
        )


def test_script_must_hit_subtitle_timeline() -> None:
    subtitle = "1\n00:00:10,000 --> 00:00:12,000\nHello\n"
    assert subtitle_ranges(subtitle) == [(10.0, 12.0)]
    with pytest.raises(ValueError, match="outside the subtitle timeline"):
        normalize_script(
            [{"timestamp": "00:00:13,000-00:00:14,000", "picture": "Outside"}],
            Path("source.mp4"),
            subtitle,
        )
