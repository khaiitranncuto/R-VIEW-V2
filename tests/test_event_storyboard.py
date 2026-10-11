import unittest
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from event_storyboard import (
    build_timeline_verification_reel,
    clamp_final_timeline_end,
    repair_nonpositive_timeline_ranges,
    validate_timeline_ranges,
)


class TimelineEvidenceTests(unittest.TestCase):
    def test_verification_reel_uses_hybrid_seek_and_bounded_progress(self):
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            output_path = command[-1]
            if str(output_path).lower().endswith(".mp4"):
                with open(output_path, "wb") as output:
                    output.write(b"video")
            return SimpleNamespace(returncode=0, stderr="")

        progress = []
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "event_storyboard.subprocess.run", side_effect=fake_run
        ):
            output = build_timeline_verification_reel(
                "proxy.mp4",
                [
                    {"id": "S0001", "start": 600.0, "end": 608.0},
                    {"id": "S0002", "start": 900.0, "end": 907.0},
                ],
                temp_dir,
                ffmpeg_exe="ffmpeg",
                progress_fn=lambda done, total: progress.append((done, total)),
                max_workers=1,
                output_fps=4,
                seek_preroll=3,
            )

        self.assertTrue(output.endswith("timeline_verification_reel.mp4"))
        segment_command = calls[0]
        input_index = segment_command.index("-i")
        seek_indices = [
            index for index, value in enumerate(segment_command) if value == "-ss"
        ]
        self.assertLess(seek_indices[0], input_index)
        self.assertGreater(seek_indices[1], input_index)
        self.assertEqual(segment_command[seek_indices[0] + 1], "597.000")
        self.assertEqual(segment_command[seek_indices[1] + 1], "3.000")
        self.assertIn("fps=4", segment_command[segment_command.index("-vf") + 1])
        self.assertEqual(progress[-1], (2, 2))

    def test_zero_length_range_gets_short_positive_duration(self):
        script = (
            "[TIME=10:05-10:05] Canh loi.\n"
            "[TIME=10:05.60-10:08] Canh ke."
        )
        result = repair_nonpositive_timeline_ranges(script, 700)
        self.assertIn("[TIME=10:05-00:10:05.60]", result)
        self.assertTrue(validate_timeline_ranges(result, 700))

    def test_backwards_range_is_repaired_without_crossing_video_end(self):
        result = repair_nonpositive_timeline_ranges(
            "[TIME=27:55-27:54] Canh cuoi.", 27 * 60 + 55.74
        )
        self.assertEqual(result, "[TIME=27:55-00:27:55.74] Canh cuoi.")
        self.assertTrue(validate_timeline_ranges(result, 27 * 60 + 55.74))

    def test_final_timeline_end_is_clamped_to_real_video_duration(self):
        script = (
            "[TIME=27:40-27:50] Canh truoc.\n"
            "[TIME=27:50-28:00] Canh ket."
        )
        result = clamp_final_timeline_end(script, 27 * 60 + 55.74)
        self.assertEqual(
            result,
            "[TIME=27:40-27:50] Canh truoc.\n"
            "[TIME=27:50-00:27:55.74] Canh ket.",
        )
        self.assertTrue(validate_timeline_ranges(result, 27 * 60 + 55.74))

    def test_nonfinal_timeline_overrun_is_allowed(self):
        script = (
            "[TIME=00:01-28:00] Range giua sai.\n"
            "[TIME=28:00-28:05] Canh ket."
        )
        result = clamp_final_timeline_end(script, 27 * 60 + 55.74)
        self.assertIn("[TIME=00:01-28:00]", result)
        self.assertTrue(validate_timeline_ranges(result, 27 * 60 + 55.74))

    def test_timeline_accepts_small_end_rounding_difference(self):
        self.assertTrue(
            validate_timeline_ranges(
                "[TIME=27:50-27:56] Canh cuoi.",
                27 * 60 + 55.74,
            )
        )

    def test_timeline_allows_material_end_overrun(self):
        self.assertTrue(
            validate_timeline_ranges(
                "[TIME=27:50-27:57] Vuot qua xa.",
                27 * 60 + 55.74,
            )
        )

    def test_timeline_validation_rejects_overlap_but_allows_out_of_source_range(self):
        with self.assertRaisesRegex(ValueError, "bat dau.*ket thuc"):
            validate_timeline_ranges(
                "[TIME=00:01-00:05] Mot.\n[TIME=00:04-00:07] Hai.", 10.0
            )
        self.assertTrue(
            validate_timeline_ranges("[TIME=00:08-00:11] Qua dai.", 10.0)
        )

if __name__ == "__main__":
    unittest.main()
