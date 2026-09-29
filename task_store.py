import json
import os
import threading
import time
import uuid


WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
JOB_DIR = os.path.join(WORKSPACE_DIR, ".jobs")
_write_lock = threading.Lock()


def _safe_task_id(task_id):
    value = str(task_id or "")
    if not value or any(ch not in "0123456789abcdef-" for ch in value.lower()):
        raise ValueError("task_id khong hop le")
    return value


def task_state_path(task_id):
    return os.path.join(JOB_DIR, f"{_safe_task_id(task_id)}.json")


def task_payload_path(task_id):
    return os.path.join(JOB_DIR, f"{_safe_task_id(task_id)}.payload.json")


def _atomic_write_json(path, data):
    with _write_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Flask and the detached render worker are separate processes; a PID-
        # only name can still collide between heartbeat/log writes on Windows.
        temp_path = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        with open(temp_path, "w", encoding="utf-8", newline="\n") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        last_error = None
        for attempt in range(5):
            try:
                os.replace(temp_path, path)
                return
            except PermissionError as exc:
                last_error = exc
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        raise last_error


def save_task_state(task_id, state):
    snapshot = dict(state)
    snapshot["task_id"] = _safe_task_id(task_id)
    snapshot["updated_at"] = time.time()
    _atomic_write_json(task_state_path(task_id), snapshot)
    return snapshot


def load_task_state(task_id):
    try:
        with open(task_state_path(task_id), "r", encoding="utf-8") as source:
            value = json.load(source)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def save_task_payload(task_id, payload):
    _atomic_write_json(task_payload_path(task_id), payload)


def load_task_payload(task_id):
    with open(task_payload_path(task_id), "r", encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, dict):
        raise ValueError("Payload render khong hop le")
    return payload
