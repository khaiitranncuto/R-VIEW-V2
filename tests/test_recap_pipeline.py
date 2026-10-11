import json
import unittest

from recap_pipeline import (
    apply_selective_repairs,
    build_single_part_continuation_prompt,
    build_single_part_video_prompt,
    build_selective_repair_prompt,
    build_three_part_video_prompt,
    clamp_payload_narrations,
    inspect_three_part_payload,
    issue_scene_ids,
    minimum_scene_count,
    merge_single_part_additions,
    merge_single_part_responses,
    recommended_scene_range,
    single_part_missing_requirements,
    normalize_three_part_payload,
    payload_to_timeline,
    three_part_targets,
    validate_single_part_response,
)


def sample_payload():
    return {
        "characters": [{"id": "P1", "name": "Nam", "description": "áo đen"}],
        "parts": [
            {
                "part": 1, "video_start": "00:00", "video_end": "00:30",
                "target_words": 6,
                "scenes": [{
                    "scene_id": "S0001", "start": "00:02", "end": "00:08",
                    "type": "main", "characters": ["P1"],
                    "event": "Nam mở cửa", "tts": "Nam mở cửa và bước vào nhà.",
                    "status": "ok", "errors": [],
                }],
                "reserve_scenes": [],
            },
            {
                "part": 2, "video_start": "00:30", "video_end": "01:00",
                "target_words": 6,
                "scenes": [{
                    "scene_id": "S0002", "start": "00:35", "end": "00:42",
                    "type": "bridge", "characters": ["P1"],
                    "event": "Nam tìm chìa khóa", "tts": "Mọi chuyện.",
                    "status": "error", "errors": ["tts_scene_mismatch"],
                }],
                "reserve_scenes": [{
                    "scene_id": "S0901", "start": "00:45", "end": "00:52",
                    "type": "reserve", "characters": ["P1"],
                    "event": "Nam nhặt chìa khóa", "tts": "",
                    "status": "ok", "errors": [],
                }],
            },
            {
                "part": 3, "video_start": "01:00", "video_end": "01:30",
                "target_words": 6,
                "scenes": [{
                    "scene_id": "S0003", "start": "01:10", "end": "01:18",
                    "type": "main", "characters": ["P1"],
                    "event": "Nam rời đi", "tts": "Nam khóa cửa rồi rời khỏi nhà.",
                    "status": "ok", "errors": [],
                }],
                "reserve_scenes": [],
            },
        ],
    }


class ThreePartRecapTests(unittest.TestCase):
    def test_targets_preserve_total(self):
        targets = three_part_targets(10)
        self.assertEqual(targets, [4, 3, 3])
        self.assertEqual(sum(targets), 10)

    def test_minimum_scene_count_respects_tts_word_ceiling(self):
        self.assertEqual(minimum_scene_count(25), 1)
        self.assertEqual(minimum_scene_count(26), 2)

    def test_recommended_scene_range_uses_minus_15_plus_20_percent(self):
        self.assertEqual(recommended_scene_range(180), (10, 9, 12))

    def test_prompt_requests_three_parts_characters_and_selective_status(self):
        prompt = build_three_part_video_prompt(90, 18, 16, 22)
        self.assertIn("đúng 3 phần", prompt)
        self.assertIn("P1, P2", prompt)
        self.assertIn('status="error"', prompt)
        self.assertIn("thiếu tối đa 15%", prompt)
        self.assertIn("thừa tối đa 20%", prompt)
        self.assertIn("bắt buộc 12-25 từ", prompt)
        self.assertIn("không viết như danh sách nhật ký", prompt)

    def test_prompt_requests_word_locked_visual_beats(self):
        prompt = build_three_part_video_prompt(90, 18, 16, 22)
        self.assertIn("visual_beats", prompt)
        self.assertIn("trigger_text", prompt)

    def test_prompt_keeps_balanced_parts_without_sacrificing_ending(self):
        prompt = build_three_part_video_prompt(5400, 1800, 1530, 2160)
        self.assertIn("GIỮ CÂN BẰNG", prompt)
        self.assertIn("20-30% phần 3", prompt)
        self.assertIn("không cắt phần kết", prompt)
        self.assertIn("narration_scene", prompt)
        self.assertIn("story_role", prompt)

    def test_single_part_prompt_sets_exact_local_budget(self):
        prompt = build_single_part_video_prompt(2, 30, 30, 60, 90)
        self.assertIn("phần 2/3", prompt)
        self.assertIn("Tổng TTS mục tiêu: 90 từ", prompt)
        self.assertIn("thời gian CỤC BỘ", prompt)
        self.assertIn("không được ít hơn 4", prompt)

    def test_merge_single_parts_offsets_timestamps_and_scene_ids(self):
        responses = []
        for part_number in range(1, 4):
            raw = {
                "characters": [{"id": "P1", "name": "Nam", "description": "ao den"}],
                "parts": [{
                    "part": part_number,
                    "video_start": "00:00",
                    "video_end": "00:30",
                    "scenes": [{
                        "scene_id": "S0001", "start": "00:02", "end": "00:08",
                        "type": "main", "characters": ["P1"], "event": "x",
                        "tts": "Nam tiep tuc hanh dong trong cau chuyen dang dien ra truoc mat.",
                        "story_role": "ending" if part_number == 3 else "development",
                        "visual_beats": [], "status": "ok", "errors": [],
                    }],
                    "reserve_scenes": [],
                }],
            }
            responses.append(json.dumps(raw))

        payload = merge_single_part_responses(responses, [0, 30, 60, 90], 18)

        self.assertEqual(
            [part["scenes"][0]["start"] for part in payload["parts"]],
            ["00:02.00", "00:32.00", "01:02.00"],
        )
        self.assertEqual(
            [part["scenes"][0]["scene_id"] for part in payload["parts"]],
            ["S0001", "S0002", "S0003"],
        )

    def test_single_part_validation_rejects_insufficient_capacity(self):
        raw = {
            "parts": [{"part": 1, "scenes": [{}], "reserve_scenes": []}],
        }
        with self.assertRaisesRegex(ValueError, "cần ít nhất 2"):
            validate_single_part_response(json.dumps(raw), 1, 26)

    def test_single_part_validation_rejects_too_few_actual_words(self):
        raw = {
            "parts": [{
                "part": 1,
                "scenes": [{"tts": "qua ngan"}, {"tts": "van qua ngan"}],
                "reserve_scenes": [],
            }],
        }
        with self.assertRaisesRegex(ValueError, "từ TTS"):
            validate_single_part_response(json.dumps(raw), 1, 26)

    def test_single_part_validation_accepts_words_above_target(self):
        sentence = "mot hai ba bon nam sau bay tam chin muoi muoi mot muoi hai muoi ba"
        raw = {
            "parts": [{
                "part": 1,
                "video_end": "00:10",
                "scenes": [
                    {"start": "00:01", "end": "00:05", "tts": sentence},
                    {"start": "00:06", "end": "00:10", "tts": sentence},
                ],
                "reserve_scenes": [],
            }],
        }
        validated = validate_single_part_response(
            json.dumps(raw), 1, 10, require_coverage=False
        )
        self.assertEqual(len(validated["parts"][0]["scenes"]), 2)

    def test_single_part_detects_missing_middle_and_final_coverage(self):
        sentence = "mot hai ba bon nam sau bay tam chin muoi muoi mot muoi hai muoi ba"
        raw = {
            "parts": [{
                "part": 1, "video_start": "00:00", "video_end": "01:40",
                "scenes": [
                    {"start": "00:01", "end": "00:05", "tts": sentence},
                    {"start": "00:10", "end": "00:14", "tts": sentence},
                    {"start": "00:20", "end": "00:24", "tts": sentence},
                ],
                "reserve_scenes": [],
            }],
        }
        requirements = single_part_missing_requirements(json.dumps(raw), 100, 45)
        self.assertEqual(
            [region["band"] for region in requirements["regions"]], [2, 3]
        )
        final_region = requirements["regions"][-1]
        self.assertEqual(final_region["start"], 90.0)

    def test_invalid_continuation_may_keep_word_complete_base_result(self):
        sentence = "mot hai ba bon nam sau bay tam chin muoi muoi mot muoi hai muoi ba"
        raw = {
            "parts": [{
                "part": 1, "video_start": "00:00", "video_end": "01:40",
                "scenes": [
                    {"start": "00:01", "end": "00:05", "tts": sentence},
                    {"start": "00:10", "end": "00:14", "tts": sentence},
                    {"start": "00:20", "end": "00:24", "tts": sentence},
                ],
                "reserve_scenes": [],
            }],
        }
        with self.assertRaisesRegex(ValueError, "chưa phủ đủ thời gian"):
            validate_single_part_response(json.dumps(raw), 1, 45)
        validated = validate_single_part_response(
            json.dumps(raw), 1, 45, require_coverage=False
        )
        self.assertEqual(validated["parts"][0]["part"], 1)

    def test_single_part_additions_accept_only_requested_nonoverlapping_scene(self):
        current = {
            "characters": [],
            "parts": [{
                "part": 1,
                "scenes": [{"scene_id": "S1", "start": "00:01", "end": "00:06", "tts": "old"}],
                "reserve_scenes": [],
            }],
        }
        additions = {
            "characters": [],
            "parts": [{
                "part": 1,
                "scenes": [
                    {"scene_id": "N1", "start": "00:03", "end": "00:05", "tts": "duplicate"},
                    {"scene_id": "N2", "start": "01:32", "end": "01:38", "tts": "new ending"},
                ],
                "reserve_scenes": [],
            }],
        }
        merged, added = merge_single_part_additions(
            json.dumps(current), json.dumps(additions),
            [{"band": 3, "start": 90.0, "end": 100.0, "new_scenes": 1}],
        )
        self.assertEqual(added, 1)
        self.assertEqual(len(json.loads(merged)["parts"][0]["scenes"]), 2)

    def test_continuation_prompt_requests_only_missing_regions(self):
        raw = {
            "characters": [],
            "parts": [{"part": 3, "scenes": [], "reserve_scenes": []}],
        }
        requirements = {
            "regions": [{"band": 3, "start": 90.0, "end": 100.0, "new_scenes": 2}],
            "actual_words": 20, "minimum_words": 40, "missing_words": 20,
        }
        prompt = build_single_part_continuation_prompt(
            3, 100, 45, json.dumps(raw), requirements
        )
        self.assertIn("VIẾT TIẾP", prompt)
        self.assertIn("thêm đúng 2 scenes mới", prompt)
        self.assertIn("không viết lại kết quả cũ", prompt)

    def test_visual_beat_is_normalized_and_encoded_at_trigger_phrase(self):
        raw = sample_payload()
        raw["parts"][0]["scenes"][0].update({
            "tts": "Nam mo cua, nguoi dan ong lap tuc rut sung.",
            "visual_beats": [{
                "trigger_text": "nguoi dan ong",
                "start": "00:10",
                "end": "00:13",
                "match_type": "literal",
                "confidence": 0.91,
            }],
        })
        payload = normalize_three_part_payload(json.dumps(raw), 90, 18)
        timeline = payload_to_timeline(payload)
        self.assertIn(
            "Nam mo cua, [BEAT=00:10.00-00:13.00] nguoi dan ong",
            timeline,
        )

    def test_invalid_or_ambiguous_trigger_is_not_emitted(self):
        raw = sample_payload()
        raw["parts"][0]["scenes"][0].update({
            "tts": "Nam thay Nam roi bo di.",
            "visual_beats": [{
                "trigger_text": "Nam",
                "start": "00:10",
                "end": "00:13",
            }],
        })
        payload = normalize_three_part_payload(json.dumps(raw), 90, 18)
        self.assertNotIn("[BEAT=", payload_to_timeline(payload))

    def test_low_confidence_visual_beat_is_not_emitted(self):
        raw = sample_payload()
        raw["parts"][0]["scenes"][0].update({
            "tts": "Nam mo cua, nguoi dan ong lap tuc rut sung.",
            "visual_beats": [{
                "trigger_text": "nguoi dan ong",
                "start": "00:10",
                "end": "00:13",
                "confidence": 0.55,
            }],
        })
        payload = normalize_three_part_payload(json.dumps(raw), 90, 18)
        self.assertNotIn("[BEAT=", payload_to_timeline(payload))

    def test_short_scene_narration_is_sent_to_repair(self):
        payload = normalize_three_part_payload(json.dumps(sample_payload()), 90, 18)
        issues = inspect_three_part_payload(payload)
        short_ids = {
            issue["target_id"] for issue in issues
            if issue["code"] == "tts_too_short"
        }
        self.assertEqual(short_ids, {"S0001", "S0002", "S0003"})
        prompt = build_selective_repair_prompt(payload, issues)
        self.assertIn("tuyệt đối không chèn từ rỗng", prompt)

    def test_part_above_target_is_not_sent_to_repair(self):
        raw = sample_payload()
        raw["parts"][0]["scenes"][0]["tts"] = " ".join(["dai"] * 40)
        payload = normalize_three_part_payload(json.dumps(raw), 90, 18)
        issues = inspect_three_part_payload(payload)
        self.assertFalse(any(issue["code"] == "part_too_long" for issue in issues))

    def test_normalize_requires_three_contiguous_parts(self):
        raw = sample_payload()
        normalized = normalize_three_part_payload(json.dumps(raw), 90, 18)
        self.assertEqual([item["part"] for item in normalized["parts"]], [1, 2, 3])
        broken = sample_payload()
        broken["parts"][1]["video_start"] = "00:40"
        with self.assertRaisesRegex(ValueError, "nối tiếp"):
            normalize_three_part_payload(json.dumps(broken), 90, 18)

        unordered = sample_payload()
        unordered["parts"][0], unordered["parts"][1] = (
            unordered["parts"][1], unordered["parts"][0]
        )
        with self.assertRaisesRegex(ValueError, "thứ tự 1, 2, 3"):
            normalize_three_part_payload(json.dumps(unordered), 90, 18)

    def test_scene_may_cross_part_boundary_when_it_overlaps_the_part(self):
        raw = sample_payload()
        raw["parts"][1]["scenes"][0]["start"] = "00:28"
        raw["parts"][1]["scenes"][0]["end"] = "00:35"

        normalized = normalize_three_part_payload(json.dumps(raw), 90, 18)

        scene = normalized["parts"][1]["scenes"][0]
        self.assertEqual(scene["start"], "00:28.00")
        self.assertEqual(scene["end"], "00:35.00")

    def test_scene_may_be_fully_outside_its_assigned_part(self):
        raw = sample_payload()
        raw["parts"][1]["scenes"][0]["start"] = "00:20"
        raw["parts"][1]["scenes"][0]["end"] = "00:25"

        normalized = normalize_three_part_payload(json.dumps(raw), 90, 18)

        scene = normalized["parts"][1]["scenes"][0]
        self.assertEqual(scene["start"], "00:20.00")
        self.assertEqual(scene["end"], "00:25.00")

    def test_normalize_retries_when_scene_capacity_cannot_meet_budget(self):
        with self.assertRaisesRegex(ValueError, "cần ít nhất 2"):
            normalize_three_part_payload(json.dumps(sample_payload()), 90, 90)

    def test_reserve_scene_is_rehomed_by_timestamp(self):
        raw = sample_payload()
        misplaced = raw["parts"][1]["reserve_scenes"].pop()
        raw["parts"][0]["reserve_scenes"].append(misplaced)
        normalized = normalize_three_part_payload(json.dumps(raw), 90, 18)
        self.assertEqual(normalized["parts"][0]["reserve_scenes"], [])
        self.assertEqual(
            normalized["parts"][1]["reserve_scenes"][0]["scene_id"],
            "S0901",
        )

    def test_only_error_and_related_reserve_ids_are_selected(self):
        raw = sample_payload()
        raw["parts"][0]["scenes"][0]["tts"] = (
            "Nam mo cua roi than trong buoc vao ngoi nha toi truoc mat."
        )
        raw["parts"][2]["scenes"][0]["tts"] = (
            "Sau khi khoa cua, Nam roi khoi nha va tiep tuc len duong."
        )
        payload = normalize_three_part_payload(json.dumps(raw), 90, 36)
        issues = inspect_three_part_payload(payload)
        ids = issue_scene_ids(payload, issues)
        self.assertIn("S0002", ids)
        self.assertIn("S0901", ids)
        self.assertNotIn("S0001", ids)
        prompt = build_selective_repair_prompt(payload, issues)
        self.assertIn("CHỈ gồm các scene_id bị lỗi", prompt)

    def test_too_few_scenes_activates_related_reserves(self):
        payload = normalize_three_part_payload(json.dumps(sample_payload()), 90, 18)
        for part in payload["parts"]:
            part["target_words"] = 30
        issues = inspect_three_part_payload(payload)
        few_scene_issues = [
            issue for issue in issues
            if issue["code"] == "part_too_few_scenes"
        ]
        self.assertEqual(len(few_scene_issues), 3)
        self.assertIn("S0901", issue_scene_ids(payload, few_scene_issues))
        prompt = build_selective_repair_prompt(payload, few_scene_issues)
        self.assertIn("không kéo dài câu cũ", prompt)

    def test_too_many_scenes_requests_bridge_removal(self):
        payload = normalize_three_part_payload(json.dumps(sample_payload()), 90, 18)
        part = payload["parts"][0]
        part["target_words"] = 18
        template = part["scenes"][0]
        for index in range(2, 5):
            extra = dict(template)
            extra.update({
                "scene_id": f"S10{index:02d}",
                "start": f"00:{8 + index * 2:02d}",
                "end": f"00:{9 + index * 2:02d}",
                "type": "bridge",
            })
            part["scenes"].append(extra)
        issues = inspect_three_part_payload(payload)
        excessive = [issue for issue in issues if issue["code"] == "part_too_many_scenes"]
        self.assertEqual(len(excessive), 1)
        self.assertEqual(excessive[0]["excess_scenes"], 3)
        self.assertIn("excess_scenes", build_selective_repair_prompt(payload, excessive))

    def test_material_uncovered_tail_requests_story_ending_repair(self):
        payload = normalize_three_part_payload(json.dumps(sample_payload()), 90, 18)
        payload["parts"][0]["video_end"] = "06:40.00"
        payload["parts"][1]["video_start"] = "06:40.00"
        payload["parts"][1]["video_end"] = "13:20.00"
        payload["parts"][2]["video_start"] = "13:20.00"
        payload["parts"][2]["video_end"] = "20:00.00"
        payload["parts"][2]["scenes"][0]["start"] = "14:00.00"
        payload["parts"][2]["scenes"][0]["end"] = "14:10.00"
        issues = inspect_three_part_payload(payload)
        ending = [item for item in issues if item["code"] == "story_ending_missing"]
        self.assertEqual(len(ending), 1)
        self.assertGreater(ending[0]["uncovered_seconds"], 300)

    def test_normal_credit_tail_does_not_trigger_missing_ending(self):
        payload = normalize_three_part_payload(json.dumps(sample_payload()), 90, 18)
        issues = inspect_three_part_payload(payload)
        self.assertFalse(any(item["code"] == "story_ending_missing" for item in issues))

    def test_long_story_requires_ending_budget_inside_balanced_part_three(self):
        payload = normalize_three_part_payload(json.dumps(sample_payload()), 90, 18)
        payload["parts"][2]["video_end"] = "20:00.00"
        payload["parts"][2]["scenes"] = []
        for index, role in enumerate(["climax", "climax", "climax", "climax", "ending"]):
            payload["parts"][2]["scenes"].append({
                "scene_id": f"S20{index:02d}",
                "start": f"19:{index * 5:02d}.00",
                "end": f"19:{index * 5 + 4:02d}.00",
                "type": "main",
                "characters": ["P1"],
                "event": "event",
                "tts": "Mot cau ke co du noi dung cu the cho canh phim nay.",
                "story_role": role,
                "visual_beats": [],
                "status": "ok",
                "errors": [],
            })
        issues = inspect_three_part_payload(payload)
        budget = [item for item in issues if item["code"] == "part3_ending_budget_low"]
        self.assertEqual(len(budget), 1)
        self.assertEqual(budget[0]["minimum_closing_scenes"], 2)

    def test_repairs_cannot_change_timestamp_or_drop_main(self):
        payload = normalize_three_part_payload(json.dumps(sample_payload()), 90, 18)
        repaired = apply_selective_repairs(payload, json.dumps({
            "repairs": [{
                "scene_id": "S0002", "tts": "Nam cúi xuống tìm chiếc chìa khóa."
            }],
            "additions": [{
                "scene_id": "S0901", "tts": "Anh nhặt chiếc chìa khóa dưới sàn."
            }],
            "drop_scene_ids": ["S0001"],
        }))
        timeline = payload_to_timeline(repaired)
        self.assertIn("[TIME=00:02.00-00:08.00]", timeline)
        self.assertIn("Nam cúi xuống", timeline)
        self.assertIn("Anh nhặt chiếc", timeline)

    def test_repairs_ignore_scene_ids_outside_allowed_batch(self):
        payload = normalize_three_part_payload(json.dumps(sample_payload()), 90, 18)
        repaired = apply_selective_repairs(payload, json.dumps({
            "repairs": [
                {"scene_id": "S0001", "tts": "Câu tốt bị đổi trái phép."},
                {"scene_id": "S0002", "tts": "Nam cúi xuống tìm chìa khóa."},
            ],
            "additions": [],
            "drop_scene_ids": [],
        }), allowed_scene_ids=["S0002"])
        timeline = payload_to_timeline(repaired)
        self.assertIn("Nam mở cửa và bước vào nhà.", timeline)
        self.assertNotIn("bị đổi trái phép", timeline)

    def test_final_word_clamp_updates_payload_and_timeline_together(self):
        payload = normalize_three_part_payload(json.dumps(sample_payload()), 90, 18)
        payload["parts"][0]["scenes"][0]["tts"] = " ".join(
            f"tu{index}" for index in range(30)
        )
        clamped = clamp_payload_narrations(payload)
        narration = clamped["parts"][0]["scenes"][0]["tts"]
        self.assertEqual(len(narration.split()), 25)
        self.assertIn(narration, payload_to_timeline(clamped))


if __name__ == "__main__":
    unittest.main()
