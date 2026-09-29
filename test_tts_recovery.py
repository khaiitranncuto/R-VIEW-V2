import asyncio
import subprocess
import unittest
from unittest import mock

import process_utils
import tts_engine


class TtsRecoveryQueueTests(unittest.TestCase):
    def test_recovery_retries_only_failed_group_and_keeps_index(self):
        failure = RuntimeError("temporary failure")
        failed = [(4, ("voice-a", [(10, "Cau thu muoi mot")]), failure)]
        expected = (4, [(10, "TTS", "chunk.mp3", None, 1.25)], "group.mp3")

        async def fake_process(*args, **kwargs):
            return expected

        with (
            mock.patch.object(tts_engine, "TTS_RECOVERY_DELAYS", (0.0,)),
            mock.patch.object(tts_engine, "_invalidate_tts_group_cache", return_value=True),
            mock.patch.object(tts_engine, "process_tts_group", side_effect=fake_process) as process_mock,
        ):
            recovered, pending, _ = asyncio.run(
                tts_engine._recover_failed_tts_groups(
                    failed, "+0%", "temp", "ffmpeg", None
                )
            )

        self.assertEqual(recovered, [expected])
        self.assertEqual(pending, [])
        self.assertEqual(process_mock.call_count, 1)
        self.assertEqual(process_mock.call_args.args[0], 4)
        self.assertEqual(process_mock.call_args.args[1], [(10, "Cau thu muoi mot")])


class HiddenSubprocessPolicyTests(unittest.TestCase):
    def test_windows_policy_adds_no_window_without_rewriting_command(self):
        original_public_popen = subprocess.Popen
        fake_popen = mock.Mock(return_value="process")
        try:
            process_utils._POLICY_INSTALLED = False
            with (
                mock.patch.object(process_utils.os, "name", "nt"),
                mock.patch.object(process_utils, "_ORIGINAL_POPEN", fake_popen),
            ):
                process_utils.install_hidden_subprocess_policy()
                result = subprocess.Popen(["ffmpeg", "-version"], stdout=subprocess.DEVNULL)
            self.assertEqual(result, "process")
            self.assertEqual(fake_popen.call_args.args[0], ["ffmpeg", "-version"])
            flags = fake_popen.call_args.kwargs["creationflags"]
            self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
            self.assertEqual(fake_popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        finally:
            subprocess.Popen = original_public_popen
            process_utils._POLICY_INSTALLED = False


if __name__ == "__main__":
    unittest.main()
