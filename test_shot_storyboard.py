import unittest

from shot_storyboard import (
    choose_candidate_shots,
    parse_shot_selection,
    parse_story_event_ranges,
    shot_script_to_timeline,
    validate_timeline_ranges,
)


class ShotStoryboardTests(unittest.TestCase):
    def setUp(self):
        self.shots = [
            {"id": "S00001", "start": 0.0, "end": 2.0, "mid": 1.0},
            {"id": "S00002", "start": 2.0, "end": 5.0, "mid": 3.5},
            {"id": "S00003", "start": 5.0, "end": 8.0, "mid": 6.5},
            {"id": "S00004", "start": 8.0, "end": 12.0, "mid": 10.0},
        ]

    def test_story_events_above_99_minutes_are_parsed(self):
        ranges = parse_story_event_ranges(
            "[EVENT=99:50-100:10] hanh dong\n[EVENT=130:00-131:00] ngoai video",
            video_duration=6015,
        )
        self.assertEqual(ranges, [(5990.0, 6010.0)])

    def test_candidates_are_real_shots_near_event_window(self):
        candidates = choose_candidate_shots(
            self.shots, [(4.5, 8.1)], video_duration=12, padding=0,
            max_per_event=10, max_total=10,
        )
        self.assertEqual(candidates, [1, 2, 3])

    def test_candidate_sampling_is_bounded_and_ordered(self):
        many = [
            {"id": f"S{i + 1:05d}", "start": float(i), "end": float(i + 1), "mid": i + 0.5}
            for i in range(100)
        ]
        candidates = choose_candidate_shots(
            many, [(0, 100)], 100, padding=0, max_per_event=12, max_total=8,
        )
        self.assertEqual(len(candidates), 8)
        self.assertEqual(candidates, sorted(set(candidates)))

    def test_shot_markers_resolve_to_machine_timestamps(self):
        timeline = shot_script_to_timeline(
            "[SHOT=S00002] Anh mo cua,\n"
            "[SHOT=S00003] roi lap tuc chay ra ngoai.",
            self.shots,
            minimum_shots=2,
        )
        self.assertEqual(
            timeline.splitlines(),
            [
                "[TIME=00:02.00-00:05.00] Anh mo cua,",
                "[TIME=00:05.00-00:08.00] roi lap tuc chay ra ngoai.",
            ],
        )

    def test_unknown_or_backwards_shot_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "khong ton tai"):
            shot_script_to_timeline("[SHOT=S99999] Khong co.", self.shots)
        with self.assertRaisesRegex(ValueError, "di lui"):
            shot_script_to_timeline(
                "[SHOT=S00003] Di truoc. [SHOT=S00002] Di lui.", self.shots
            )

    def test_three_pass_selection_preserves_literal_descriptions(self):
        selected, _ = parse_shot_selection(
            "[SHOT=S00001] nhan vat quay dau\n"
            "[SHOT=S00004] nhan vat buoc ra ngoai",
            self.shots,
            minimum_shots=2,
        )
        self.assertEqual([item["id"] for item in selected], ["S00001", "S00004"])
        self.assertEqual(selected[1]["description"], "nhan vat buoc ra ngoai")

    def test_fractional_timeline_outside_video_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "vuot thoi luong video"):
            validate_timeline_ranges(
                "[TIME=00:58.25-01:00.25] qua gioi han", video_duration=60.0
            )

    def test_fractional_real_shot_boundaries_do_not_collapse(self):
        fractional = [
            {"id": "S00001", "start": 0.0, "end": 1.667, "mid": 0.8335},
            {"id": "S00002", "start": 1.667, "end": 3.333, "mid": 2.5},
        ]
        timeline = shot_script_to_timeline(
            "[SHOT=S00001] Mo cua, [SHOT=S00002] roi quay dau.",
            fractional,
            minimum_shots=2,
        )
        self.assertIn("[TIME=00:00.00-00:01.67]", timeline)
        self.assertIn("[TIME=00:01.67-00:03.33]", timeline)
        self.assertTrue(validate_timeline_ranges(timeline, 3.34))


if __name__ == "__main__":
    unittest.main()
