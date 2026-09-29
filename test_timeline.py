import unittest

from tts_engine import (
    format_timeline_rows,
    parse_sentences_and_time_ranges,
    parse_sentences_and_timestamps,
    remove_non_story_timeline_rows,
    split_text_into_sentences,
)
from video_renderer import _pause_duration_for_text, build_visual_edit_plan, fit_tts_to_time_ranges


class TimelineParsingTests(unittest.TestCase):
    def test_each_timestamp_is_formatted_as_one_row(self):
        source = (
            "[TIME=00:12] Câu thứ nhất. [TIME=00:36] "
            "Câu thứ hai. Câu thứ ba."
        )
        self.assertEqual(
            format_timeline_rows(source).splitlines(),
            [
                "[TIME=00:12] Câu thứ nhất.",
                "[TIME=00:36] Câu thứ hai. Câu thứ ba.",
            ],
        )

    def test_every_action_inherits_its_timeline_anchor(self):
        source = (
            "[TIME=01:57] Con rắn xuất hiện, lao tới và cuốn lấy hai người."
        )
        sentences = split_text_into_sentences(source)
        clean, timestamps = parse_sentences_and_timestamps(sentences)
        self.assertEqual(timestamps, [117, 117, 117])
        self.assertEqual(
            clean,
            ["Con rắn xuất hiện", "lao tới", "và cuốn lấy hai người."],
        )

    def test_multiple_sentences_keep_the_same_anchor(self):
        source = "[TIME=00:10] Chuột chạy đi. Ếch lập tức đuổi theo."
        _, timestamps = parse_sentences_and_timestamps(split_text_into_sentences(source))
        self.assertEqual(timestamps, [10, 10])

    def test_logo_and_credit_rows_are_removed(self):
        source = (
            "[TIME=00:01] Logo hãng phim xuất hiện.\n"
            "[TIME=00:12] Chú chuột bước ra khỏi nhà.\n"
            "[TIME=06:30] Danh sách đoàn làm phim hiện lên."
        )
        self.assertEqual(
            remove_non_story_timeline_rows(source),
            "[TIME=00:12] Chú chuột bước ra khỏi nhà.",
        )

    def test_start_end_ranges_are_preserved_and_parsed(self):
        source = (
            "[TIME=01:36-01:51] Chuột kể chiến tích. "
            "[TIME=01:51-01:57] Chuột tiếp tục khoe khoang."
        )
        formatted = format_timeline_rows(source)
        sentences = split_text_into_sentences(formatted)
        clean, starts, ends = parse_sentences_and_time_ranges(sentences)
        self.assertEqual(clean, ["Chuột kể chiến tích.", "Chuột tiếp tục khoe khoang."])
        self.assertEqual(starts, [96, 111])
        self.assertEqual(ends, [111, 117])

    def test_action_group_keeps_related_sentences_in_one_tts_chunk(self):
        source = (
            "[TIME=02:03-02:14] Con rắn xuất hiện. "
            "Nó lao tới và cuốn lấy cả hai người."
        )
        sentences = split_text_into_sentences(source)
        self.assertEqual(len(sentences), 1)
        clean, starts, ends = parse_sentences_and_time_ranges(sentences)
        self.assertEqual(clean, ["Con rắn xuất hiện. Nó lao tới và cuốn lấy cả hai người."])
        self.assertEqual(starts, [123])
        self.assertEqual(ends, [134])

    def test_pause_policy_is_short_and_punctuation_aware(self):
        self.assertEqual(_pause_duration_for_text("Câu đang nối"), 0.12)
        self.assertEqual(_pause_duration_for_text("Kết thúc ý."), 0.22)
        self.assertEqual(_pause_duration_for_text("Nhưng rồi..."), 0.28)

    def test_tts_inside_visual_range_is_not_changed(self):
        paths, durations, adjusted = fit_tts_to_time_ranges(
            ["voice.mp3"], [4.0], [10.0], [15.0], "unused"
        )
        self.assertEqual(paths, ["voice.mp3"])
        self.assertEqual(durations, [4.0])
        self.assertFalse(adjusted)

    def test_audio_led_edl_keeps_each_gemini_anchor_independent(self):
        plan = build_visual_edit_plan(
            durations=[6.0, 4.0],
            starts=[10.0, 15.0],
            ends=[15.0, 19.0],
            video_duration=100.0,
        )
        self.assertEqual(plan[0]["source_start"], 10.0)
        self.assertEqual(plan[1]["source_start"], 15.0)
        self.assertGreaterEqual(plan[0]["playback_speed"], 0.90)

    def test_audio_led_edl_uses_real_frames_at_source_end(self):
        plan = build_visual_edit_plan(
            durations=[6.0],
            starts=[98.0],
            ends=[100.0],
            video_duration=100.0,
        )
        self.assertAlmostEqual(plan[0]["source_start"] + plan[0]["source_duration"], 100.0)
        self.assertGreater(plan[0]["source_duration"], 0.0)

    def test_audio_led_edl_uses_interpolation_for_missing_anchor(self):
        plan = build_visual_edit_plan(
            durations=[2.0, 3.0],
            starts=[10.0, None],
            ends=[12.0, None],
            video_duration=100.0,
            fallback_starts=[10.0, 24.0],
        )
        self.assertEqual(plan[1]["source_start"], 24.0)


if __name__ == "__main__":
    unittest.main()
