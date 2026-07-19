from pathlib import Path

import pytest

from cutmaster.cuts import choose_source_window, optimize_script_source_windows


def test_choose_source_window_minimizes_worst_cut_distance() -> None:
    result = choose_source_window(
        original_start_sec=10.0,
        clip_duration_sec=4.0,
        output_start_sec=0.0,
        internal_source_cuts_sec=[11.2, 12.4],
        beat_times=[1.0, 2.0, 3.0],
        source_duration_sec=60.0,
        frame_rate=10.0,
        search_margin_sec=0.5,
    )

    assert result.source_start_sec == pytest.approx(10.1)
    assert result.internal_output_cuts_sec == pytest.approx((1.1, 2.3))
    assert result.max_beat_distance_sec == pytest.approx(0.3)


def test_choose_source_window_keeps_detected_cuts_inside() -> None:
    result = choose_source_window(
        original_start_sec=10.0,
        clip_duration_sec=4.0,
        output_start_sec=5.0,
        internal_source_cuts_sec=[10.1, 12.1],
        candidate_source_cuts_sec=[10.1, 12.1, 14.5],
        beat_times=[5.0, 6.0],
        source_duration_sec=60.0,
        frame_rate=30.0,
        search_margin_sec=2.0,
    )

    assert result.source_start_sec >= 10.0
    for output_cut in result.internal_output_cuts_sec:
        relative_cut = output_cut - 5.0
        assert relative_cut > 1.0
        assert 4.0 - relative_cut > 1.0


def test_choose_source_window_never_moves_backward() -> None:
    result = choose_source_window(
        original_start_sec=10.0,
        clip_duration_sec=4.0,
        output_start_sec=0.0,
        internal_source_cuts_sec=[10.5, 12.5],
        candidate_source_cuts_sec=[10.5, 12.5, 14.5],
        beat_times=[1.0, 2.0, 3.0],
        source_duration_sec=60.0,
        frame_rate=30.0,
    )

    assert 10.0 <= result.source_start_sec <= 12.0


def test_choose_source_window_relaxes_edge_constraint_when_strict_search_is_impossible() -> None:
    result = choose_source_window(
        original_start_sec=10.0,
        clip_duration_sec=2.0,
        output_start_sec=0.0,
        internal_source_cuts_sec=[10.4, 11.0, 11.6],
        candidate_source_cuts_sec=[10.4, 11.0, 11.6, 12.4, 13.0, 13.6],
        beat_times=[0.5, 1.0, 1.5],
        source_duration_sec=60.0,
        frame_rate=10.0,
    )

    assert result.source_start_sec >= 10.0
    assert result.fallback_level > 0
    assert result.effective_min_boundary_distance_sec < 1.0


def test_choose_source_window_accounts_for_cuts_entering_shifted_window() -> None:
    result = choose_source_window(
        original_start_sec=10.0,
        clip_duration_sec=4.0,
        output_start_sec=0.0,
        internal_source_cuts_sec=[12.0],
        candidate_source_cuts_sec=[9.5, 12.0, 14.5],
        beat_times=[2.0],
        source_duration_sec=60.0,
        frame_rate=10.0,
    )

    assert result.source_start_sec == pytest.approx(10.0)
    assert result.internal_source_cuts_sec == pytest.approx((12.0,))


def test_choose_source_window_without_cuts_preserves_range() -> None:
    result = choose_source_window(
        original_start_sec=10.0,
        clip_duration_sec=4.0,
        output_start_sec=0.0,
        internal_source_cuts_sec=[],
        beat_times=[1.0, 2.0],
        source_duration_sec=60.0,
        frame_rate=30.0,
    )

    assert result.source_start_sec == 10.0
    assert result.max_beat_distance_sec == 0.0


def test_edge_constraint_is_enforced_without_audio_beats() -> None:
    result = choose_source_window(
        original_start_sec=10.0,
        clip_duration_sec=4.0,
        output_start_sec=0.0,
        internal_source_cuts_sec=[10.1, 12.1],
        candidate_source_cuts_sec=[10.1, 12.1, 14.5],
        beat_times=[],
        source_duration_sec=60.0,
        frame_rate=30.0,
    )

    assert result.source_start_sec >= 10.0
    assert all(
        1.0 < cut - result.source_start_sec < 3.0
        for cut in result.internal_source_cuts_sec
    )


def test_parallel_optimization_preserves_script_order(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_detect(_path: Path, start: float, end: float) -> tuple[list[float], float]:
        return [start + (end - start) / 2.0], 10.0

    monkeypatch.setattr("cutmaster.cuts.detect_source_cuts", fake_detect)
    items = [
        {
            "_id": index,
            "timestamp": f"00:00:{source:02d},000-00:00:{source + 4:02d},000",
            "output_timestamp": f"00:00:{output:02d},000-00:00:{output + 4:02d},000",
            "output_frame_range": [output * 10, (output + 4) * 10],
        }
        for index, (source, output) in enumerate(((10, 0), (20, 4), (30, 8)), start=1)
    ]

    optimized = optimize_script_source_windows(
        Path("source.mp4"),
        items,
        beat_times=[2.0, 6.0, 10.0],
        source_duration_sec=60.0,
        output_fps=10,
        max_workers=3,
    )

    assert [item["_id"] for item in optimized] == [1, 2, 3]
    assert all(item["cut_optimization"]["max_beat_distance_sec"] == 0.0 for item in optimized)
