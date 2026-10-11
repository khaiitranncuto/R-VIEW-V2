import os
import tempfile
import unittest
from unittest import mock

import task_store


class PersistentTaskStoreTests(unittest.TestCase):
    def test_state_and_payload_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(task_store, "JOB_DIR", temp_dir):
            task_id = "11111111-1111-1111-1111-111111111111"
            task_store.save_task_state(task_id, {"status": "running", "progress": 25})
            task_store.save_task_payload(task_id, {"sentences": ["Cau mot"]})

            state = task_store.load_task_state(task_id)
            payload = task_store.load_task_payload(task_id)
            self.assertEqual(state["status"], "running")
            self.assertEqual(state["progress"], 25)
            self.assertEqual(payload["sentences"], ["Cau mot"])
            self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(temp_dir)))

    def test_invalid_task_id_cannot_escape_job_directory(self):
        with self.assertRaises(ValueError):
            task_store.task_state_path("../outside")


if __name__ == "__main__":
    unittest.main()
