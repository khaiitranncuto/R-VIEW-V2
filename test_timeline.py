import unittest

from tts_engine import (
    MAX_TTS_RETRIES,
    TTS_MAX_CONCURRENCY,
    TTS_REQUEST_TIMEOUT,
    TTS_RETRY_DELAYS,
    _build_tts_groups,
    _split_long_tts_text,
    _tts_cache_paths,
    format_timeline_rows,
    expand_visual_beats,
    parse_sentences_and_visual_beats,
    parse_sentences_and_time_ranges,
    parse_sentences_and_timestamps,
    remove_non_story_timeline_rows,
    split_text_into_sentences,
)
from video_renderer import (
    _pause_duration_for_text,
    _remove_voice_control_rows,
    _require_exact_audio_alignment,
    build_visual_edit_plan,
    fit_tts_to_time_ranges,
)


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

    def test_minute_timestamps_above_99_are_not_collapsed(self):
        source = (
            "[TIME=99:55-100:05] Cau truoc. "
            "[TIME=100:05-100:15] Cau sau."
        )
        rows = split_text_into_sentences(source)
        clean, starts, ends = parse_sentences_and_time_ranges(rows)
        self.assertEqual(clean, ["Cau truoc.", "Cau sau."])
        self.assertEqual(starts, [5995, 6005])
        self.assertEqual(ends, [6005, 6015])

    def test_oversized_tts_is_split_at_safe_boundaries(self):
        text = ("Mot cau ke tu nhien, " * 100).strip()
        pieces = _split_long_tts_text(text, max_chars=200)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(0 < len(piece) <= 200 for piece in pieces))
        self.assertEqual(" ".join(pieces), text)

    def test_action_group_becomes_two_ordered_visual_beats(self):
        source = (
            "[TIME=02:03-02:14] Con rắn xuất hiện. "
            "Nó lao tới và cuốn lấy cả hai người."
        )
        sentences = split_text_into_sentences(source)
        self.assertEqual(len(sentences), 2)
        clean, starts, ends = parse_sentences_and_time_ranges(sentences)
        self.assertEqual(clean, ["Con rắn xuất hiện.", "Nó lao tới và cuốn lấy cả hai người."])
        self.assertEqual(starts, [123, 126])
        self.assertEqual(ends, [126, 134])

    def test_visual_beats_are_capped_at_two(self):
        source = (
            "[TIME=00:10-00:19] Anh mở cửa. "
            "Sau đó anh chạy ra ngoài. Cuối cùng anh nhảy xuống nước."
        )
        sentences = split_text_into_sentences(source)
        _, starts, ends = parse_sentences_and_time_ranges(sentences)
        self.assertEqual(len(sentences), 2)
        self.assertEqual(starts[0], 10)
        self.assertEqual(ends[-1], 19)
        self.assertEqual(ends[0], starts[1])

    def test_inline_visual_beats_keep_one_natural_tts_sentence(self):
        source = (
            "[TIME=01:20-01:24] Luke nhat manh xuong len, "
            "[BEAT=01:27-01:31] roi phat hien dau chan gau tren cat."
        )
        rows = split_text_into_sentences(source)
        self.assertEqual(len(rows), 1)
        clean, starts, ends, groups = parse_sentences_and_visual_beats(rows)
        self.assertEqual(
            clean,
            ["Luke nhat manh xuong len, roi phat hien dau chan gau tren cat."],
        )
        self.assertEqual([(b["start"], b["end"]) for b in groups[0]], [(80, 84), (87, 91)])

    def test_visual_edl_uses_word_boundaries_inside_one_sentence(self):
        sentences = ["Luke nhat xuong, roi tim thay dau chan gau."]
        groups = [[
            {"text": "Luke nhat xuong,", "start": 80, "end": 84},
            {"text": "roi tim thay dau chan gau.", "start": 87, "end": 91},
        ]]
        cues = [[
            {"start": 0.0, "end": 0.4, "content": "Luke"},
            {"start": 0.4, "end": 0.8, "content": "nhat"},
            {"start": 0.8, "end": 1.2, "content": "xuong"},
            {"start": 1.5, "end": 1.8, "content": "roi"},
            {"start": 1.8, "end": 2.2, "content": "tim"},
            {"start": 2.2, "end": 2.6, "content": "thay"},
            {"start": 2.6, "end": 3.0, "content": "dau"},
            {"start": 3.0, "end": 3.4, "content": "chan"},
            {"start": 3.4, "end": 4.0, "content": "gau"},
        ]]
        texts, durations, starts, ends = expand_visual_beats(
            sentences, [4.0], groups, cues
        )
        self.assertEqual(len(texts), 2)
        self.assertAlmostEqual(sum(durations), 4.0)
        self.assertEqual(starts, [80, 87])
        self.assertEqual(ends, [84, 91])

    def test_tts_groups_adjacent_beats_but_respects_voice_changes(self):
        sentences = ["Cau mot.", "Cau hai.", "Cau ba."]
        groups = _build_tts_groups(sentences, ["voice-a", "voice-a", "voice-b"])
        self.assertEqual([len(items) for _, items in groups], [2, 1])

    def test_long_video_tts_retry_policy(self):
        self.assertEqual(MAX_TTS_RETRIES, 3)
        self.assertEqual(TTS_MAX_CONCURRENCY, 3)
        self.assertEqual(TTS_RETRY_DELAYS, (5.0, 15.0))
        self.assertEqual(TTS_REQUEST_TIMEOUT, 60.0)

    def test_tts_cache_key_includes_text_voice_and_rate(self):
        base = _tts_cache_paths("Xin chao", "voice-a", "+0%")
        self.assertNotEqual(base, _tts_cache_paths("Xin chao ban", "voice-a", "+0%"))
        self.assertNotEqual(base, _tts_cache_paths("Xin chao", "voice-b", "+0%"))
        self.assertNotEqual(base, _tts_cache_paths("Xin chao", "voice-a", "+10%"))

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

    def test_tts_overflow_keeps_natural_cadence_by_default(self):
        paths, durations, adjusted = fit_tts_to_time_ranges(
            ["voice.mp3"], [6.0], [10.0], [13.0], "unused"
        )
        self.assertEqual(paths, ["voice.mp3"])
        self.assertEqual(durations, [6.0])
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
        self.assertLessEqual(plan[0]["source_start"] + plan[0]["source_duration"], 15.0)

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

    def test_voice_control_row_does_not_shift_visual_timeline(self):
        sentences, starts, ends = _remove_voice_control_rows(
            ["Cau mot", "<VOICE=voice-b>", "Cau hai"],
            [1.0, None, 3.0],
            [3.0, None, 5.0],
        )
        self.assertEqual(sentences, ["Cau mot", "Cau hai"])
        self.assertEqual(starts, [1.0, 3.0])
        self.assertEqual(ends, [3.0, 5.0])

    def test_render_stops_when_audio_count_does_not_match_scenes(self):
        with self.assertRaises(RuntimeError):
            _require_exact_audio_alignment(["Cau mot", "Cau hai"], ["one.mp3"], [1.0])


if __name__ == "__main__":
    unittest.main()
