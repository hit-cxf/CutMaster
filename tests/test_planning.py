from cutmaster.planning import (
    _globally_align_boundaries,
    _validate_slots,
    align_slots_to_music,
    path_to_script,
    select_paths,
)


def _slots():
    return [
        {
            "slot_id": "slot_01",
            "content_description": "setup",
            "target_emotional_intensity": 0.2,
            "target_kinetic_energy": 0.2,
            "desired_duration_sec": 5,
        },
        {
            "slot_id": "slot_02",
            "content_description": "climax",
            "target_emotional_intensity": 0.9,
            "target_kinetic_energy": 0.9,
            "desired_duration_sec": 3,
        },
    ]


def test_slot_validation_and_accent_alignment() -> None:
    raw = {
        "slots": [
            {"content_description": "setup", "desired_duration_sec": 5},
            {"content_description": "climax", "desired_duration_sec": 3},
        ]
    }
    slots = _validate_slots(raw, 2)
    aligned = align_slots_to_music(
        slots,
        {"accents_sec": [5.0], "beats_sec": [4.0, 5.0]},
        8.0,
        30,
    )
    assert aligned[0]["output_end_sec"] == 5.0
    assert sum(slot["planned_duration_sec"] for slot in aligned) == 8.0


def test_global_alignment_does_not_exhaust_later_accents() -> None:
    result = _globally_align_boundaries(
        [4.0, 8.0, 12.0],
        [3.8, 4.2, 7.9, 8.1, 11.9, 12.1],
        16.0,
        30,
    )
    assert len(result) == 3
    assert result == sorted(result)


def test_beam_search_uses_pairwise_sequence_score() -> None:
    slots = _slots()
    for slot in slots:
        slot["planned_duration_sec"] = slot["desired_duration_sec"]
    pool = {
        "slot_01": [
            {"candidate_id": "a", "timestamp": "00:00:01,000-00:00:07,000", "description": "Mia starts", "semantic_relevance": 1.0, "emotional_intensity": 0.2, "kinetic_energy": 0.2, "salience": 1.0},
            {"candidate_id": "b", "timestamp": "00:00:20,000-00:00:26,000", "description": "Mia starts", "semantic_relevance": 0.95, "emotional_intensity": 0.2, "kinetic_energy": 0.2, "salience": 1.0},
        ],
        "slot_02": [
            {"candidate_id": "c", "timestamp": "00:00:10,000-00:00:14,000", "description": "Mia climax", "semantic_relevance": 1.0, "emotional_intensity": 0.9, "kinetic_energy": 0.9, "salience": 1.0},
        ],
    }
    greedy, beam, diagnostics = select_paths(slots, pool, beam_width=4)
    assert [item["candidate_id"] for item in greedy] == ["a", "c"]
    assert [item["candidate_id"] for item in beam] == ["a", "c"]
    assert diagnostics["beam_score"] > 0
    script = path_to_script(slots, beam, __import__("pathlib").Path("video.mp4"))
    assert script[0]["planned_duration_sec"] == 5
