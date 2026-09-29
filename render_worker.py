import os
import sys
import threading
import time
import traceback

from task_store import load_task_payload, load_task_state
from process_utils import install_hidden_subprocess_policy


def _keep_windows_awake(enable):
    if os.name != "nt":
        return
    try:
        import ctypes

        es_continuous = 0x80000000
        es_system_required = 0x00000001
        flags = es_continuous | es_system_required if enable else es_continuous
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def main(task_id):
    # Importing here keeps the web process and render process completely separate.
    # Import the application before replacing subprocess.Popen. On Windows,
    # asyncio.windows_utils defines a class derived from subprocess.Popen during
    # import, so replacing Popen with a function first makes asyncio crash.
    import app as application

    # The worker invokes FFmpeg/FFprobe hundreds of times for long videos. Hide
    # their console windows only after all application dependencies are loaded.
    install_hidden_subprocess_policy()

    payload = load_task_payload(task_id)
    state = load_task_state(task_id) or {
        "status": "queued",
        "progress": 0,
        "message": "Dang khoi tao worker...",
        "logs": [],
    }
    state.update({
        "persistent": True,
        "worker_pid": os.getpid(),
        "status": "running",
        "heartbeat_at": time.time(),
    })
    application.restore_persistent_task(task_id, state)

    stop_heartbeat = threading.Event()

    def heartbeat_loop():
        while not stop_heartbeat.wait(3.0):
            application.update_task_fields(task_id, heartbeat_at=time.time())

    heartbeat = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat.start()
    _keep_windows_awake(True)
    try:
        application.render_video_bg(
            task_id,
            payload["video_name"],
            payload.get("bgm_name", ""),
            float(payload.get("bgm_volume", 0.12)),
            payload["voice_name"],
            payload["voice_rate"],
            payload["sentences"],
            payload["options"],
            payload.get("export_mode", "full_mp4"),
        )
    except BaseException as exc:
        # render_video_bg handles normal exceptions. This catches process-level
        # failures as well so the persisted task never remains "running" forever.
        application.append_task_log(
            task_id,
            "ERROR",
            f"Worker dung dot ngot: {type(exc).__name__}: {exc}",
            traceback.format_exc(),
        )
        application.update_task_fields(
            task_id,
            status="failed",
            message=f"Worker render dung dot ngot: {exc}",
        )
        raise
    finally:
        stop_heartbeat.set()
        heartbeat.join(timeout=2.0)
        application.update_task_fields(task_id, heartbeat_at=time.time())
        _keep_windows_awake(False)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: render_worker.py TASK_ID")
    main(sys.argv[1])
