import unittest

from app import (
    build_model_candidates,
    calculate_script_budget,
    is_usable_story_map,
)


class AnalysisPipelineTests(unittest.TestCase):
    def test_auto_budget_uses_one_ratio_and_twenty_minute_cap(self):
        self.assertEqual(calculate_script_budget(30)[0:2], (6.0, 900))
        self.assertEqual(calculate_script_budget(120)[0:2], (20.0, 3000))
        self.assertEqual(calculate_script_budget(300)[0:2], (20.0, 3000))

    def test_shorts_budget_keeps_same_duration_rule(self):
        review_minutes, target_words, _, _ = calculate_script_budget(120, style="shorts")
        self.assertEqual(review_minutes, 20.0)
        self.assertEqual(target_words, 2600)

    def test_manual_budget_never_exceeds_source_duration(self):
        self.assertEqual(calculate_script_budget(5, review_minutes=20)[0], 5.0)

    def test_model_candidates_prioritize_selected_text_model(self):
        models = [
            "models/gemini-3.5-flash-lite",
            "models/gemini-3.5-flash",
            "models/gemini-3.5-flash-tts",
            "models/gemini-3.1-flash-image",
        ]
        candidates = build_model_candidates("gemini-3.5-flash", models)
        self.assertEqual(candidates[0], "gemini-3.5-flash")
        self.assertNotIn("gemini-3.5-flash-tts", candidates)
        self.assertNotIn("gemini-3.1-flash-image", candidates)

    def test_non_text_selected_model_is_rejected(self):
        candidates = build_model_candidates(
            "gemini-3.1-flash-image", ["gemini-3.1-flash-image", "gemini-3.5-flash"]
        )
        self.assertEqual(candidates, ["gemini-3.5-flash"])

    def test_story_map_requires_timestamped_events(self):
        valid = (
            "NHAN VAT: A\n"
            "[EVENT=01:00-02:00] A | roi khoi nha | troi sang | den rung\n"
            "[EVENT=100:05-101:10] A | tro ve | tim thay ban | an toan"
        )
        self.assertTrue(is_usable_story_map(valid, 120))
        self.assertFalse(is_usable_story_map("Tom tat chung chung khong co moc thoi gian", 120))


if __name__ == "__main__":
    unittest.main()
