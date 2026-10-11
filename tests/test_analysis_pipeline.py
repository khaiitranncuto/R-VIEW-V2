import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import (
    MAX_CONTINUATION_ROUNDS,
    TASKS,
    _format_review_time,
    analysis_proxy_fps,
    analyze_video_bg,
    app as flask_app,
    ascii_upload_filename,
    build_model_candidates,
    calculate_script_budget,
    create_task,
    generate_gemini_text_with_recovery,
    gemini_retry_delay_seconds,
    is_daily_project_gemini_quota_error,
    is_gemini_auth_error,
    is_per_minute_gemini_quota_error,
    is_transient_gemini_error,
    measure_script_narration,
    normalize_existing_video_filenames,
    normalize_simple_gemini_timeline,
    resolve_selected_gemini_model,
    stage_gemini_upload,
    split_video_into_three_parts,
    upload_gemini_file_with_recovery,
    validate_gemini_api_key,
    wait_for_gemini_file,
)


class AnalysisPipelineTests(unittest.TestCase):
    def test_missing_scene_continuation_is_limited_to_two_rounds(self):
        self.assertEqual(MAX_CONTINUATION_ROUNDS, 2)

    def test_budget_uses_minus_fifteen_plus_twenty_percent(self):
        minutes, target, minimum, maximum = calculate_script_budget(30)
        self.assertEqual(minutes, 7.5)
        self.assertEqual(target, 1125)
        self.assertEqual(minimum, 957)
        self.assertEqual(maximum, 1350)

    def test_proxy_fps_decreases_for_long_video(self):
        self.assertEqual(analysis_proxy_fps(10 * 60), 12)
        self.assertEqual(analysis_proxy_fps(30 * 60), 8)
        self.assertEqual(analysis_proxy_fps(60 * 60), 6)
        self.assertEqual(analysis_proxy_fps(61 * 60), 3)

    def test_timeline_normalizes_and_clamps_narration(self):
        words = " ".join(f"tu{index}" for index in range(30))
        normalized = normalize_simple_gemini_timeline(
            f"```\n[TIME=00:01-00:06] {words}\n```", 20
        )
        count, events = measure_script_narration(normalized)
        self.assertEqual(events, 1)
        self.assertEqual(count, 30)
        self.assertEqual(_format_review_time(65.5), "01:05.5")

    def test_existing_video_is_renamed_to_ascii_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            unicode_name = "cảnh quay.mp4"
            with open(os.path.join(temp_dir, unicode_name), "wb") as output:
                output.write(b"unicode")
            with open(os.path.join(temp_dir, "canh_quay.mp4"), "wb") as output:
                output.write(b"existing")
            videos = normalize_existing_video_filenames(temp_dir)
            self.assertEqual(videos, ["canh_quay.mp4", "canh_quay_1.mp4"])
            with open(os.path.join(temp_dir, "canh_quay.mp4"), "rb") as source:
                self.assertEqual(source.read(), b"existing")

    def test_uploaded_filename_is_safe_ascii(self):
        self.assertEqual(
            ascii_upload_filename("Phim tiếng Việt - tập 1.MP4"),
            "Phim_tieng_Viet_tap_1.mp4",
        )
        self.assertEqual(ascii_upload_filename("../../cảnh quay.mov"), "canh_quay.mov")

    def test_video_is_split_into_three_equal_local_clips(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            with open(command[-1], "wb") as output:
                output.write(b"clip")
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.subprocess.run", side_effect=fake_run):
                boundaries, outputs = split_video_into_three_parts(
                    "source.mp4", temp_dir, 90, "ffmpeg"
                )

        self.assertEqual(boundaries, [0.0, 30.0, 60.0, 90.0])
        self.assertEqual(len(outputs), 3)
        self.assertEqual(
            [call[call.index("-ss") + 1] for call in calls],
            ["0.000", "30.000", "60.000"],
        )
        self.assertEqual(
            [call[call.index("-t") + 1] for call in calls],
            ["30.000"] * 3,
        )

    def test_video_upload_returns_ascii_filename(self):
        response = flask_app.test_client().post(
            "/api/upload-file",
            data={
                "type": "video",
                "file": (io.BytesIO(b"small test video"), "video tiếng Việt.mp4"),
            },
            content_type="multipart/form-data",
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["success"])
        payload["filename"].encode("ascii")
        uploaded_path = os.path.join(flask_app.root_path, payload["filename"])
        try:
            self.assertTrue(os.path.isfile(uploaded_path))
        finally:
            if os.path.isfile(uploaded_path):
                os.remove(uploaded_path)

    def test_split_endpoint_falls_back_to_timeline_rows(self):
        script = "[TIME=00:01-00:04] Cau mot.\n[TIME=00:05-00:08] Cau hai."
        with patch("app.split_text_into_sentences", side_effect=ValueError("bad token")):
            response = flask_app.test_client().post(
                "/api/split-sentences", json={"text": script}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), script.splitlines())

    def test_api_key_validation_and_quota_classification(self):
        self.assertEqual(validate_gemini_api_key("AIzaSy_valid-Key_123"), "")
        self.assertIn("Unicode", validate_gemini_api_key("AIzaSy_key_tiếng_Việt"))
        self.assertIn("dieu khien", validate_gemini_api_key("AIzaSy123\nnext"))
        daily = "429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier"
        minute = "429 RESOURCE_EXHAUSTED GenerateRequestsPerMinutePerProjectPerModel-FreeTier retryDelay: 30s"
        self.assertTrue(is_daily_project_gemini_quota_error(daily))
        self.assertFalse(is_per_minute_gemini_quota_error(daily))
        self.assertTrue(is_per_minute_gemini_quota_error(minute))
        self.assertTrue(is_transient_gemini_error("503 UNAVAILABLE"))
        self.assertFalse(is_transient_gemini_error(minute))
        self.assertTrue(is_gemini_auth_error("401 UNAUTHENTICATED API_KEY_INVALID"))

    def test_retry_delay_honors_google_retry_info(self):
        error = "429 RESOURCE_EXHAUSTED retryDelay: '58.5s' input_token_count PerMinute"
        self.assertEqual(gemini_retry_delay_seconds(error, 0), 59.5)


    def test_generate_recovery_retries_only_when_allowed(self):
        outcomes = [RuntimeError("503 UNAVAILABLE"), SimpleNamespace(text="ok")]

        class Models:
            def generate_content(self, **kwargs):
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        client = SimpleNamespace(models=Models())
        result = generate_gemini_text_with_recovery(
            client, "model", ["prompt"], None, "test",
            max_attempts=2, sleep_fn=lambda _: None,
        )
        self.assertEqual(result, "ok")

    def test_upload_retries_remote_protocol_disconnect(self):
        outcomes = [
            RuntimeError("RemoteProtocolError: Server disconnected without sending a response"),
            SimpleNamespace(name="files/video"),
        ]

        class Files:
            def upload(self, **kwargs):
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        messages = []
        result = upload_gemini_file_with_recovery(
            SimpleNamespace(files=Files()), "video.mp4",
            update_progress=messages.append,
            max_attempts=2,
            sleep_fn=lambda _: None,
        )
        self.assertEqual(result.name, "files/video")
        self.assertIn("thu lai 2/2", messages[0])

    def test_wait_for_uploaded_file_handles_eventual_active(self):
        states = [RuntimeError("404 not found"), SimpleNamespace(state="PROCESSING"), SimpleNamespace(state="ACTIVE")]

        class Files:
            def get(self, **kwargs):
                value = states.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value

        result = wait_for_gemini_file(
            SimpleNamespace(files=Files()), "file", max_wait_seconds=5,
            poll_interval=1, sleep_fn=lambda _: None,
        )
        self.assertEqual(result.state, "ACTIVE")

    def test_upload_staging_uses_ascii_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "video tiếng Việt.mp4")
            staging = os.path.join(temp_dir, "ascii_stage")
            os.makedirs(staging)
            with open(source, "wb") as output:
                output.write(b"video")
            staged = stage_gemini_upload(source, staging, "source_video")
            staged.encode("ascii")
            self.assertTrue(os.path.isfile(staged))

    def test_model_selection_filters_non_text_models(self):
        models = [
            SimpleNamespace(name="models/gemini-3.5-flash", supported_actions=["generateContent"]),
            SimpleNamespace(name="models/gemini-3.1-flash-image", supported_actions=["generateContent"]),
            SimpleNamespace(name="models/gemini-robotics", supported_actions=["bidiGenerateContent"]),
        ]
        self.assertEqual(build_model_candidates("gemini-3.5-flash", models), ["gemini-3.5-flash"])
        self.assertEqual(resolve_selected_gemini_model("models/gemini-3.5-flash"), "gemini-3.5-flash")

    def test_analysis_rotates_only_on_daily_quota(self):
        task_id = create_task()
        daily = RuntimeError(
            "429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier"
        )
        try:
            with patch("app._analyze_video_bg_once", side_effect=[daily, None]) as run:
                analyze_video_bg(
                    task_id, ["key-one", "key-two"], "model", "video.mp4", "style", ""
                )
            self.assertEqual([call.args[1] for call in run.call_args_list], ["key-one", "key-two"])
        finally:
            TASKS.pop(task_id, None)

    def test_analyze_endpoint_rejects_duplicate_keys(self):
        response = flask_app.test_client().post("/api/analyze-video", json={
            "api_keys": ["AIzaSy_same", "AIzaSy_same"],
            "video_name": "source.mp4",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("API 2", response.get_json()["message"])


if __name__ == "__main__":
    unittest.main()
