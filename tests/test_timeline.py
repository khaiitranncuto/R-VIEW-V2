import unittest

from tts_engine import (
    MAX_TTS_RETRIES,
    TTS_MAX_CONCURRENCY,
    TTS_REQUEST_TIMEOUT,
    TTS_RETRY_DELAYS,
    _build_tts_groups,
    _cue_partitions,
    _word_locked_beat_boundaries,
    _split_long_tts_text,
    _tts_cache_paths,
    format_timeline_rows,
    expand_visual_beats,
    parse_sentences_and_visual_beats,
    remove_non_story_timeline_rows,
    split_text_into_sentences,
)
from video_renderer import (
    _pause_duration_for_text,
    _quantize_durations_to_frames,
    _remove_voice_control_rows,
    _require_exact_audio_alignment,
    build_shot_spans,
    build_visual_edit_plan,
    fit_tts_to_time_ranges,
)
from scene_matcher import ADAPTIVE_SEARCH_WINDOWS


class TimelineParsingTests(unittest.TestCase):
    def test_scene_search_expands_in_small_steps_to_ten_seconds(self):
        self.assertEqual(ADAPTIVE_SEARCH_WINDOWS, (3.0, 5.0, 10.0))

    def test_missing_word_cues_never_create_out_of_range_partitions(self):
        partitions = _cue_partitions([object(), object()], ["one", "two", "three", "four"])
        self.assertEqual(len(partitions), 4)
        self.assertTrue(all(0 <= start <= end <= 2 for start, end in partitions))

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

    def test_oversized_tts_is_split_at_safe_boundaries(self):
        text = ("Mot cau ke tu nhien, " * 100).strip()
        pieces = _split_long_tts_text(text, max_chars=200)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(0 < len(piece) <= 200 for piece in pieces))
        self.assertEqual(" ".join(pieces), text)

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

    def test_nested_inline_beat_becomes_internal_scene_boundary(self):
        source = (
            "[TIME=00:31-01:26] Kukuru lao xuong suoi, "
            "[BEAT=01:05-01:26] roi bat duoc mot con ca."
        )
        rows = split_text_into_sentences(source)
        _, starts, ends, groups = parse_sentences_and_visual_beats(rows)
        self.assertEqual(starts, [31])
        self.assertEqual(ends, [86])
        self.assertEqual(
            [(beat["start"], beat["end"]) for beat in groups[0]],
            [(31, 65), (65, 86)],
        )

    def test_consecutive_overlapping_rows_are_trimmed_at_next_start(self):
        source = (
            "[TIME=85:00-85:53] Nobita giai cuu cac ban.\n"
            "[TIME=85:17-86:06] Kukuru cung dan lang vung len."
        )
        rows = split_text_into_sentences(source)
        _, starts, ends, groups = parse_sentences_and_visual_beats(rows)
        self.assertEqual(starts, [5100, 5117])
        self.assertEqual(ends, [5117, 5166])
        self.assertEqual(groups[0][0]["end"], 5117)

    def test_fully_backwards_range_is_moved_after_previous_range(self):
        source = (
            "[TIME=85:00-85:10] Su kien duoc ke truoc.\n"
            "[TIME=84:50-85:00] Su kien duoc ke sau nhung Gemini dat moc lui."
        )
        rows = split_text_into_sentences(source)
        _, starts, ends, groups = parse_sentences_and_visual_beats(rows)
        self.assertEqual(starts, [5100, 5110])
        self.assertEqual(ends, [5110, 5111])
        self.assertEqual(groups[1][0]["start"], 5110)
        self.assertGreater(groups[1][0]["end"], groups[1][0]["start"])

    def test_zero_length_range_is_given_safe_positive_duration(self):
        rows = split_text_into_sentences("[TIME=70:00-70:00] Mot canh ngan.")
        _, starts, ends, groups = parse_sentences_and_visual_beats(rows)
        self.assertEqual(starts, [4200])
        self.assertEqual(ends, [4201])
        self.assertEqual(groups[0][0]["end"], 4201)

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

    def test_visual_beat_cut_is_locked_to_trigger_word_with_small_lead(self):
        beats = [
            {"text": "John mo cua buoc vao,"},
            {"text": "nguoi dan ong lap tuc rut sung."},
        ]
        cues = [
            {"start": 0.0, "end": 0.3, "content": "John"},
            {"start": 0.3, "end": 0.6, "content": "mo"},
            {"start": 0.6, "end": 0.9, "content": "cua"},
            {"start": 0.9, "end": 1.2, "content": "buoc"},
            {"start": 1.2, "end": 1.5, "content": "vao"},
            {"start": 1.8, "end": 2.1, "content": "nguoi"},
            {"start": 2.1, "end": 2.4, "content": "dan"},
            {"start": 2.4, "end": 2.7, "content": "ong"},
            {"start": 2.7, "end": 3.1, "content": "lap tuc rut sung"},
        ]
        boundaries = _word_locked_beat_boundaries(beats, cues, 3.1)
        self.assertAlmostEqual(boundaries[1], 1.7)

    def test_visual_beat_alignment_falls_back_when_trigger_is_missing(self):
        beats = [{"text": "canh mot"}, {"text": "cum khong ton tai"}]
        cues = [
            {"start": 0.0, "end": 0.5, "content": "canh"},
            {"start": 0.5, "end": 1.0, "content": "mot"},
        ]
        self.assertIsNone(_word_locked_beat_boundaries(beats, cues, 1.0))

    def test_tts_groups_adjacent_beats_but_respects_voice_changes(self):
        sentences = ["Cau mot.", "Nhung cau hai tiep noi.", "Cau ba."]
        groups = _build_tts_groups(sentences, ["voice-a", "voice-a", "voice-b"])
        self.assertEqual([len(items) for _, items in groups], [2, 1])

    def test_tts_batches_four_consecutive_sentences_even_after_full_stops(self):
        sentences = [f"Cau thu {index}." for index in range(6)]
        groups = _build_tts_groups(sentences, ["voice-a"] * len(sentences))
        self.assertEqual([len(items) for _, items in groups], [4, 2])
        self.assertEqual(
            [index for _, items in groups for index, _ in items],
            list(range(6)),
        )

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

    def test_locked_start_is_not_pushed_by_previous_narration_overrun(self):
        plan = build_visual_edit_plan(
            durations=[6.0, 4.0],
            starts=[10.0, 15.0],
            ends=[15.0, 19.0],
            video_duration=100.0,
        )
        self.assertEqual(plan[0]["source_start"], 10.0)
        self.assertEqual(plan[1]["source_start"], 15.0)
        self.assertEqual(plan[0]["source_duration"], 5.0)
        self.assertEqual(plan[0]["hold_duration"], 1.0)
        self.assertEqual(plan[0]["playback_speed"], 1.0)

    def test_audio_overflow_extends_end_when_gap_before_next_action_is_available(self):
        plan = build_visual_edit_plan(
            durations=[6.0, 4.0],
            starts=[10.0, 18.0],
            ends=[14.0, 22.0],
            video_duration=100.0,
        )
        self.assertEqual(plan[0]["source_start"], 10.0)
        self.assertEqual(plan[0]["source_duration"], 6.0)
        self.assertEqual(plan[0]["extended_by"], 2.0)
        self.assertEqual(plan[0]["hold_duration"], 0.0)
        self.assertEqual(plan[0]["playback_speed"], 1.0)

    def test_locked_range_counts_all_natural_shots_without_extra_clip_files(self):
        scenes = [
            {"start": 0.0, "end": 11.5},
            {"start": 11.5, "end": 13.0},
            {"start": 13.0, "end": 18.0},
            {"start": 18.0, "end": 30.0},
        ]
        plan = build_visual_edit_plan(
            durations=[6.0],
            starts=[10.0],
            ends=[14.0],
            video_duration=100.0,
            scenes=scenes,
        )
        self.assertEqual(plan[0]["source_start"], 10.0)
        self.assertEqual(plan[0]["source_duration"], 6.0)
        self.assertEqual(plan[0]["extended_by"], 2.0)
        self.assertEqual(plan[0]["shot_count"], 3)

    def test_shot_selector_spreads_visuals_across_complete_locked_time(self):
        scenes = [
            {"start": 0.0, "end": 2.0},
            {"start": 2.0, "end": 4.0},
            {"start": 4.0, "end": 6.0},
            {"start": 6.0, "end": 8.0},
        ]
        plan = build_visual_edit_plan(
            durations=[6.0], starts=[0.0], ends=[8.0],
            video_duration=20.0, scenes=scenes,
        )
        spans = build_shot_spans(plan[0], scenes)
        self.assertEqual(len(spans), 3)
        self.assertAlmostEqual(sum(duration for _, duration in spans), 6.0)
        self.assertEqual(spans, sorted(spans))
        self.assertGreaterEqual(spans[0][0], 0.0)
        self.assertLessEqual(spans[-1][0] + spans[-1][1], 8.0)
        self.assertGreaterEqual(spans[-1][0], 6.0)

    def test_shot_selector_uses_bounded_end_extension_for_long_tts(self):
        scenes = [
            {"start": 0.0, "end": 2.0},
            {"start": 2.0, "end": 4.0},
            {"start": 4.0, "end": 6.0},
            {"start": 6.0, "end": 8.0},
            {"start": 8.0, "end": 10.0},
        ]
        plan = build_visual_edit_plan(
            durations=[8.0, 2.0], starts=[0.0, 10.0], ends=[4.0, 12.0],
            video_duration=20.0, scenes=scenes,
        )
        spans = build_shot_spans(plan[0], scenes)
        self.assertEqual(plan[0]["visual_window_end"], 8.0)
        self.assertAlmostEqual(sum(duration for _, duration in spans), 8.0)
        self.assertLessEqual(max(start + duration for start, duration in spans), 8.0)

    def test_locked_starts_do_not_accumulate_drift(self):
        plan = build_visual_edit_plan(
            durations=[8.0, 8.0, 8.0],
            starts=[10.0, 15.0, 20.0],
            ends=[15.0, 20.0, 25.0],
            video_duration=100.0,
        )
        self.assertEqual([item["source_start"] for item in plan], [10.0, 15.0, 20.0])

    def test_audio_led_edl_uses_real_frames_at_source_end(self):
        plan = build_visual_edit_plan(
            durations=[6.0],
            starts=[98.0],
            ends=[100.0],
            video_duration=100.0,
        )
        self.assertEqual(plan[0]["source_start"], 98.0)
        self.assertAlmostEqual(plan[0]["source_start"] + plan[0]["source_duration"], 100.0)
        self.assertEqual(plan[0]["hold_duration"], 4.0)

    def test_last_locked_range_extends_over_real_source_footage(self):
        plan = build_visual_edit_plan(
            durations=[10.0],
            starts=[20.0],
            ends=[24.0],
            video_duration=100.0,
        )
        self.assertEqual(plan[0]["source_start"], 20.0)
        self.assertEqual(plan[0]["source_duration"], 10.0)
        self.assertEqual(plan[0]["extended_by"], 6.0)
        self.assertEqual(plan[0]["hold_duration"], 0.0)
        self.assertAlmostEqual(plan[0]["playback_speed"], 1.0)

    def test_frame_rounding_uses_cumulative_clock_without_drift(self):
        durations = [0.05] * 20
        quantized = _quantize_durations_to_frames(durations, fps=30)
        self.assertAlmostEqual(sum(quantized), round(sum(durations) * 30) / 30)

    def test_short_audio_uses_only_needed_source_frames_at_normal_speed(self):
        plan = build_visual_edit_plan(
            durations=[8.0],
            starts=[20.0],
            ends=[32.0],
            video_duration=100.0,
        )
        self.assertEqual(plan[0]["source_duration"], 8.0)
        self.assertAlmostEqual(plan[0]["playback_speed"], 1.0)

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
