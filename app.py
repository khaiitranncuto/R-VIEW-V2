import os
import uuid
import copy
import threading
import shutil
import subprocess
import json
import time
import re
import traceback
import faulthandler
import sys
import math
import tempfile
import unicodedata
from flask import Flask, request, jsonify, render_template, send_from_directory
from google import genai
import asyncio
import edge_tts
from tts_engine import (
    extract_time_range,
    format_timeline_rows,
    parse_sentences_and_visual_beats,
    remove_non_story_timeline_rows,
    split_text_into_sentences,
)
from video_renderer import render_review_video_ffmpeg, export_capcut_package, generate_all_images, render_ai_image_video_ffmpeg
from task_store import (
    load_task_state,
    save_task_payload,
    save_task_state,
)
from event_storyboard import (
    build_timeline_verification_reel,
    clamp_final_timeline_end,
    repair_nonpositive_timeline_ranges,
    validate_timeline_ranges,
)
from recap_pipeline import (
    apply_selective_repairs,
    build_single_part_continuation_prompt,
    build_single_part_video_prompt,
    build_selective_repair_prompt,
    build_three_part_video_prompt,
    clamp_payload_narrations,
    inspect_three_part_payload,
    issue_scene_ids,
    merge_single_part_additions,
    merge_single_part_responses,
    normalize_three_part_payload,
    payload_to_timeline,
    scenes_by_ids,
    single_part_missing_requirements,
    three_part_targets,
    validate_single_part_response,
)

# =====================================================================
# Cấu hình Môi trường
# =====================================================================
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(WORKSPACE_DIR, "app_settings.json")
LOG_DIR = os.path.join(WORKSPACE_DIR, "logs")

# Tạo các thư mục cần thiết
os.makedirs(os.path.join(WORKSPACE_DIR, "templates"), exist_ok=True)
os.makedirs(os.path.join(WORKSPACE_DIR, "static", "css"), exist_ok=True)
os.makedirs(os.path.join(WORKSPACE_DIR, "static", "js"), exist_ok=True)
os.makedirs(os.path.join(WORKSPACE_DIR, "static", "bgm"), exist_ok=True)
os.makedirs(os.path.join(WORKSPACE_DIR, "static", "output"), exist_ok=True)
os.makedirs(os.path.join(WORKSPACE_DIR, "static", "temp_preview"), exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# Keep native Python crash information even when the web process disappears.
_crash_log_handle = open(os.path.join(LOG_DIR, "python_crash.log"), "a", encoding="utf-8", buffering=1)
faulthandler.enable(file=_crash_log_handle, all_threads=True)

def _load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as settings_file:
            return json.load(settings_file)
    except (OSError, json.JSONDecodeError):
        return {}

def _save_settings(settings):
    temp_path = SETTINGS_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8", newline="\n") as settings_file:
        json.dump(settings, settings_file, ensure_ascii=False, indent=2)
        settings_file.flush()
        os.fsync(settings_file.fileno())
    os.replace(temp_path, SETTINGS_PATH)


def get_configured_export_directory():
    export_dir = _load_settings().get("export_directory", "")
    if not isinstance(export_dir, str):
        return ""
    export_dir = export_dir.strip().strip('"')
    return os.path.abspath(os.path.normpath(export_dir)) if export_dir else ""

def get_export_directory():
    export_dir = get_configured_export_directory()
    return export_dir if export_dir and os.path.isdir(export_dir) else ""

export_copy_lock = threading.Lock()

def copy_result_to_export_directory(result_url):
    """Atomically copy a completed MP4/ZIP and verify its size."""
    configured_dir = get_configured_export_directory()
    if not configured_dir:
        return "", ""
    if not os.path.isdir(configured_dir):
        return "", f"Thu muc xuat khong ton tai: {configured_dir}. Vui long chon lai."

    filename = os.path.basename(result_url.split("?", 1)[0].rstrip("/\\"))
    source_path = os.path.join(WORKSPACE_DIR, "static", "output", filename)
    if not os.path.isfile(source_path):
        return "", "Khong tim thay file ket qua de sao chep."

    with export_copy_lock:
        target_path = os.path.join(configured_dir, filename)
        stem, extension = os.path.splitext(filename)
        counter = 1
        while os.path.exists(target_path):
            target_path = os.path.join(configured_dir, f"{stem}_{counter}{extension}")
            counter += 1

        partial_path = target_path + f".{uuid.uuid4().hex}.partial"
        last_error = None
        for attempt in range(3):
            try:
                shutil.copy2(source_path, partial_path)
                if os.path.getsize(partial_path) != os.path.getsize(source_path):
                    raise OSError("Kich thuoc file sau khi chep khong khop file goc.")
                os.replace(partial_path, target_path)
                if not os.path.isfile(target_path):
                    raise OSError("File dich khong ton tai sau khi chep.")
                return target_path, ""
            except OSError as exc:
                last_error = exc
                try:
                    if os.path.exists(partial_path):
                        os.remove(partial_path)
                except OSError:
                    pass
                if attempt < 2:
                    time.sleep(1)
        return "", f"Khong chep duoc video vao '{configured_dir}': {last_error}"

app = Flask(__name__, static_folder=os.path.join(WORKSPACE_DIR, "static"), template_folder=os.path.join(WORKSPACE_DIR, "templates"))
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024 * 1024  # 10GB cho video dai
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0


@app.after_request
def disable_local_ui_cache(response):
    """The local tool must not keep an old HTML/JS build after code updates."""
    if request.path == "/" or request.path.endswith((".js", ".css")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

# Khóa bảo mật mặc định
DEFAULT_API_KEY = ""
MAX_CONTINUATION_ROUNDS = 2

# =====================================================================
# Quản lý Tiến Trình Chạy Ngầm (Thread-safe)
# =====================================================================
TASKS = {}
PROJECTS = {}
task_lock = threading.Lock()

# Whitelist dinh dang file duoc phep upload
ALLOWED_VIDEO_EXT = {'.mp4', '.mkv', '.avi', '.mov', '.webm'}
ALLOWED_AUDIO_EXT = {'.mp3', '.wav', '.m4a', '.ogg'}

def create_task(project_id="", video_name="", persistent=False):
    task_id = str(uuid.uuid4())
    with task_lock:
        TASKS[task_id] = {
            "status": "running",
            "progress": 0,
            "message": "Dang khoi tao...",
            "result": None,
            "project_id": project_id,
            "video_name": video_name,
            "created_at": __import__('time').time(),
            "logs": [],
            "persistent": bool(persistent),
        }
        snapshot = copy.deepcopy(TASKS[task_id])
    if persistent:
        save_task_state(task_id, snapshot)
    return task_id


def _persist_task_if_needed(task_id):
    with task_lock:
        task = TASKS.get(task_id)
        snapshot = copy.deepcopy(task) if task and task.get("persistent") else None
    if snapshot is not None:
        save_task_state(task_id, snapshot)


def update_task_fields(task_id, **fields):
    """Update RAM state and atomically checkpoint persistent render state."""
    with task_lock:
        task = TASKS.setdefault(task_id, {"logs": [], "persistent": True})
        task.update(fields)
        snapshot = copy.deepcopy(task) if task.get("persistent") else None
    if snapshot is not None:
        save_task_state(task_id, snapshot)
    return snapshot


def restore_persistent_task(task_id, state):
    restored = copy.deepcopy(state or {})
    restored["persistent"] = True
    restored.setdefault("logs", [])
    with task_lock:
        TASKS[task_id] = restored
    save_task_state(task_id, restored)

def _task_log_path(task_id):
    return os.path.join(LOG_DIR, f"task_{task_id}.log")

def append_task_log(task_id, level, message, details=""):
    """Store diagnostic events so the browser can show the real backend log."""
    entry = {
        "time": time.strftime("%H:%M:%S"),
        "level": str(level or "INFO").upper(),
        "message": str(message),
    }
    if details:
        entry["details"] = str(details)
    with task_lock:
        task = TASKS.get(task_id)
        if task is not None:
            task.setdefault("logs", []).append(entry)
            # Prevent an unusually long run from growing memory forever.
            if len(task["logs"]) > 2000:
                task["logs"] = task["logs"][-2000:]
    _persist_task_if_needed(task_id)
    # File log is deliberately independent from TASKS, which only lives in RAM.
    # It therefore survives a Flask/VS Code crash and can be inspected afterward.
    try:
        with open(_task_log_path(task_id), "a", encoding="utf-8", newline="\n") as log_file:
            log_file.write(f"[{entry['time']}] [{entry['level']}] {entry['message']}\n")
            if details:
                log_file.write(str(details).rstrip() + "\n")
            log_file.flush()
            os.fsync(log_file.fileno())
    except OSError as log_error:
        print(f"[LOG ERROR] Khong ghi duoc task log: {log_error}", flush=True)
    print(f"[{entry['time']}] [{entry['level']}] [task {task_id[:8]}] {entry['message']}", flush=True)
    if details:
        print(details, flush=True)

def calculate_script_budget(video_duration_min, review_minutes="auto", style="dramatic"):
    """Return one predictable recap budget for every source duration."""
    video_duration_min = max(1.0, float(video_duration_min))
    if review_minutes == "auto":
        # One rule for every video: target a recap around 25% of the source,
        # capped at 20 minutes. This is guidance, not a destructive hard stop.
        target_review_min = min(20.0, max(1.5, video_duration_min * 0.25))
    else:
        target_review_min = min(float(review_minutes), video_duration_min)

    words_per_minute = 130 if str(style).lower() == "shorts" else 150
    minimum_words = 150 if words_per_minute == 130 else 250
    target_word_count = max(minimum_words, int(target_review_min * words_per_minute))
    min_word_count = max(100 if words_per_minute == 130 else 200, math.ceil(target_word_count * 0.85))
    max_word_count = int(target_word_count * 1.20)
    return target_review_min, target_word_count, min_word_count, max_word_count


def _format_review_time(seconds):
    value = max(0.0, float(seconds))
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    secs = value % 60
    seconds_text = f"{secs:05.2f}".rstrip("0").rstrip(".")
    if len(seconds_text.split(".", 1)[0]) < 2:
        seconds_text = "0" + seconds_text
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds_text}"
    return f"{minutes:02d}:{seconds_text}"


def measure_script_narration(script_text):
    """Count spoken words and valid timeline rows, excluding TIME metadata."""
    word_count = 0
    event_count = 0
    for row in format_timeline_rows(script_text).splitlines():
        row = row.strip()
        if not row:
            continue
        start, end, narration = extract_time_range(row)
        if start is None or end is None:
            continue
        event_count += 1
        narration = re.sub(r"\[VOICE=[^\]]+\]", " ", narration, flags=re.IGNORECASE)
        word_count += len(re.findall(r"\b\w+\b", narration, flags=re.UNICODE))
    return word_count, event_count


def trim_timeline_ends_to_locked_starts(script_text):
    """Resolve ordinary overlaps by trimming END only; never move a START."""
    rows = [
        row.strip() for row in format_timeline_rows(script_text).splitlines()
        if row.strip()
    ]
    parsed = []
    for row in rows:
        start, end, narration = extract_time_range(row)
        if start is None or end is None:
            parsed.append([start, end, narration, row])
        else:
            parsed.append([float(start), float(end), narration, row])

    for index in range(1, len(parsed)):
        previous = parsed[index - 1]
        current = parsed[index]
        if previous[0] is None or current[0] is None:
            continue
        if current[0] <= previous[0]:
            # Repeated/backwards START values are evidence errors. Preserve
            # them so validation reports the bad Gemini anchor explicitly.
            continue
        if previous[1] is not None and previous[1] > current[0]:
            previous[1] = current[0]

    normalized = []
    for start, end, narration, original in parsed:
        if start is None or end is None:
            normalized.append(original)
            continue
        normalized.append(
            f"[TIME={_format_review_time(start)}-{_format_review_time(end)}] {narration}"
        )
    return "\n".join(normalized)


def analysis_proxy_fps(video_duration_sec):
    """Keep enough temporal detail for Gemini without making long uploads excessive."""
    duration_min = max(0.0, float(video_duration_sec)) / 60.0
    if duration_min <= 10:
        return 12
    if duration_min <= 30:
        return 8
    if duration_min <= 60:
        return 6
    return 3


def build_model_candidates(selected_model, available_models):
    """Use the selected compatible model first, then known Flash fallbacks."""
    blocked_markers = (
        "-tts", "-image", "-live", "transcribe", "embedding", "aqa",
        "robotics", "bidi", "computer-use", "deep-research", "native-audio",
    )
    usable = []
    for model in available_models:
        raw_name = getattr(model, "name", model)
        name = str(raw_name).replace("models/", "")
        if "gemini" not in name.lower() or any(marker in name.lower() for marker in blocked_markers):
            continue
        supported_actions = getattr(model, "supported_actions", None)
        if supported_actions:
            action_names = [
                str(getattr(action, "value", action)).lower().replace("_", "")
                for action in supported_actions
            ]
            if not any(
                "generatecontent" in action and "bidi" not in action
                for action in action_names
            ):
                continue
        usable.append(name)

    preferred_names = [
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3-flash-preview",
    ]
    selected_model = str(selected_model or "").replace("models/", "")
    selected = [selected_model] if selected_model in usable else []
    preferred = [name for name in preferred_names if name in usable]
    ordered = selected + preferred + [m for m in usable if "flash" in m.lower()] + usable
    seen = set()
    return [m for m in ordered if m and m not in seen and not seen.add(m)]


def resolve_selected_gemini_model(model_name):
    """Use the UI-selected model verbatim, with a default only when empty."""
    selected = str(model_name or "").replace("models/", "").strip()
    return selected or "gemini-3.1-flash-lite"


def is_transient_gemini_error(error):
    """Return True only for temporary server/capacity failures, never quota errors."""
    message = str(error or "").lower()
    if any(marker in message for marker in ("429", "resource_exhausted", "quota")):
        return False
    return any(marker in message for marker in (
        "503", "unavailable", "high demand", "temporarily", "internal",
        "500", "502", "504", "deadline exceeded", "remoteprotocolerror",
        "server disconnected", "connection reset", "connection aborted",
        "readerror", "connecterror", "timeout",
    ))


def is_per_minute_gemini_quota_error(error):
    """Retry only rolling per-minute quota, never daily/project exhaustion."""
    message = str(error or "").lower().replace("_", "")
    if any(marker in message for marker in (
        "perdayperproject", "requestsperdayperproject", "per day per project",
    )):
        return False
    return (
        ("429" in message or "resourceexhausted" in message)
        and any(marker in message for marker in (
            "perminute", "per minute", "retrydelay", "inputtokencount",
        ))
    )


def is_daily_project_gemini_quota_error(error):
    """Detect only daily per-project exhaustion, which can use the next key."""
    message = str(error or "").lower().replace("_", "")
    if not ("429" in message or "resourceexhausted" in message):
        return False
    return any(marker in message for marker in (
        "perdayperproject",
        "requestsperdayperproject",
        "per day per project",
        "generatedrequestsperdayperprojectpermodel",
        "generaterequestsperdayperprojectpermodel",
    ))


def gemini_retry_delay_seconds(error, attempt):
    """Honor Google's RetryInfo when present, otherwise use bounded backoff."""
    message = str(error or "")
    matches = re.findall(
        r"(?:retry(?:Delay| in)?[^0-9]{0,20})(\d+(?:\.\d+)?)\s*s",
        message,
        flags=re.IGNORECASE,
    )
    if matches:
        return min(90.0, max(1.0, float(matches[-1]) + 1.0))
    return min(45.0, 5.0 * (2 ** max(0, int(attempt))))


def upload_gemini_file_with_recovery(
    client, file_path, label="video", update_progress=None,
    max_attempts=5, sleep_fn=time.sleep,
):
    """Upload a Gemini file with the same transport recovery as generation."""
    last_error = None
    for attempt in range(max(1, int(max_attempts))):
        try:
            return client.files.upload(file=file_path)
        except Exception as exc:
            last_error = exc
            retryable = (
                is_transient_gemini_error(exc)
                or is_per_minute_gemini_quota_error(exc)
            )
            if not retryable or attempt + 1 >= max_attempts:
                raise
            delay = gemini_retry_delay_seconds(exc, attempt)
            if update_progress is not None:
                update_progress(
                    f"Ket noi upload {label} bi ngat; thu lai "
                    f"{attempt + 2}/{max_attempts} sau {int(math.ceil(delay))} giay..."
                )
            sleep_fn(delay)
    raise last_error or RuntimeError(f"Upload {label} that bai.")


def generate_gemini_text_with_recovery(
    client, model, contents, config, label, update_progress=None,
    stream=False, max_attempts=3, sleep_fn=time.sleep,
):
    """Generate text with streaming and bounded recovery for transport/TPM errors."""
    last_error = None
    for attempt in range(max(1, int(max_attempts))):
        try:
            if stream:
                parts = []
                for chunk in client.models.generate_content_stream(
                    model=model, contents=contents, config=config
                ):
                    chunk_text = str(getattr(chunk, "text", "") or "")
                    if chunk_text:
                        parts.append(chunk_text)
                result_text = "".join(parts).strip()
            else:
                response = client.models.generate_content(
                    model=model, contents=contents, config=config
                )
                result_text = str(getattr(response, "text", "") or "").strip()
            if not result_text:
                raise RuntimeError("Gemini tra ve noi dung rong.")
            return result_text
        except Exception as exc:
            last_error = exc
            retryable = (
                is_transient_gemini_error(exc)
                or is_per_minute_gemini_quota_error(exc)
            )
            if not retryable or attempt + 1 >= max_attempts:
                raise
            delay = gemini_retry_delay_seconds(exc, attempt)
            remaining = delay
            while remaining > 0:
                if update_progress is not None:
                    update_progress(
                        f"{label} tam gian doan; tu thu lai sau {int(math.ceil(remaining))} giay..."
                    )
                step = min(5.0, remaining)
                sleep_fn(step)
                remaining -= step
    raise last_error or RuntimeError(f"{label} that bai.")


def is_gemini_auth_error(error):
    """Return True for credentials/permission failures that no model can repair."""
    message = str(error or "").lower()
    return any(marker in message for marker in (
        "api_key_invalid", "api key not valid", "invalid api key",
        "unauthenticated", "permission_denied", "permission denied",
    )) or re.search(r"(?:^|\D)(?:401|403)(?:\D|$)", message) is not None


def wait_for_gemini_file(
    client, file_name, max_wait_seconds=900, poll_interval=5,
    sleep_fn=time.sleep, on_wait=None,
):
    """Wait for an uploaded Gemini file; never treat a 404 as ACTIVE."""
    waited = 0
    while waited < max_wait_seconds:
        try:
            info = client.files.get(name=file_name)
            state = getattr(info, "state", "")
            state_name = str(getattr(state, "name", state) or "").upper()
            if state_name == "ACTIVE":
                return info
            if state_name == "FAILED":
                raise RuntimeError("Gemini khong the xu ly file video.")
        except Exception as exc:
            error_text = str(exc)
            if is_gemini_auth_error(exc):
                raise
            if not (
                "404" in error_text
                or "not found" in error_text.lower()
                or is_transient_gemini_error(exc)
            ):
                raise
        sleep_fn(poll_interval)
        waited += poll_interval
        if on_wait is not None:
            on_wait(waited, max_wait_seconds)
    raise RuntimeError(
        f"Qua {max_wait_seconds} giay nhung file video Gemini van chua ACTIVE."
    )


def validate_gemini_api_key(api_key):
    """Reject only values that cannot safely be placed in an HTTP header."""
    key = str(api_key or "").strip()
    if not key:
        return "API Key khong duoc bo trong."
    try:
        key.encode("ascii")
    except UnicodeEncodeError:
        return (
            "API Key co ky tu Unicode (vi du chu tieng Viet hoac dau ngoac la). "
            "Hay xoa key hien tai va dan lai key goc tu Google AI Studio."
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in key):
        return "API Key co ky tu dieu khien khong hop le. Hay dan lai key goc."
    return ""


def ascii_upload_filename(filename):
    """Convert an uploaded basename to a safe, readable ASCII filename."""
    basename = os.path.basename(str(filename or "").replace("\\", "/"))
    stem, extension = os.path.splitext(basename)
    stem = stem.replace("đ", "d").replace("Đ", "D")
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_") or "upload"
    extension = re.sub(r"[^A-Za-z0-9.]", "", extension).lower()
    return f"{stem}{extension}"


def available_upload_path(directory, filename):
    """Return a non-existing destination so an upload never overwrites a file."""
    stem, extension = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}_{counter}{extension}")
        counter += 1
    return candidate


def normalize_video_file(directory, filename):
    """Rename one existing video to ASCII and return its current basename."""
    original = os.path.basename(str(filename or "").replace("\\", "/"))
    source = os.path.join(directory, original)
    if not original or not os.path.isfile(source):
        return original
    normalized = ascii_upload_filename(original)
    if normalized == original:
        return original
    target = available_upload_path(directory, normalized)
    os.replace(source, target)
    return os.path.basename(target)


def normalize_existing_video_filenames(directory):
    """Normalize every supported video already present in the tool directory."""
    normalized = []
    for filename in os.listdir(directory):
        if os.path.splitext(filename)[1].lower() not in ALLOWED_VIDEO_EXT:
            continue
        normalized.append(normalize_video_file(directory, filename))
    return sorted(normalized, key=str.casefold)


def stage_gemini_upload(file_path, staging_dir, safe_stem):
    """Expose an upload through an ASCII-only path for strict HTTP clients."""
    source = os.path.abspath(file_path)
    extension = os.path.splitext(source)[1].lower() or ".bin"
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", str(safe_stem or "upload")).strip("_")
    target = os.path.abspath(os.path.join(staging_dir, f"{safe_stem or 'upload'}{extension}"))
    try:
        target.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError(
            "Thu muc tam upload Gemini van chua ky tu Unicode; hay dat TEMP/TMP vao duong dan ASCII."
        ) from exc
    if os.path.normcase(source) != os.path.normcase(target):
        shutil.copy2(source, target)
    return target


def split_video_into_three_parts(source_path, output_dir, duration_sec, ffmpeg_exe):
    """Create three exact, clip-local MP4 files whose durations cover the source."""
    duration_sec = max(0.01, float(duration_sec))
    boundaries = [0.0, duration_sec / 3.0, duration_sec * 2.0 / 3.0, duration_sec]
    proxy_fps = analysis_proxy_fps(duration_sec)
    outputs = []
    for index in range(3):
        start = boundaries[index]
        clip_duration = boundaries[index + 1] - start
        output_path = os.path.join(output_dir, f"source_part_{index + 1}.mp4")
        result = subprocess.run(
            [
                ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{start:.3f}", "-i", source_path,
                "-t", f"{clip_duration:.3f}",
                "-vf", f"scale=-2:480,fps={proxy_fps}",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "48k", "-ac", "1", "-ar", "24000",
                "-movflags", "+faststart", output_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3600,
        )
        if result.returncode != 0 or not os.path.isfile(output_path):
            raise RuntimeError(f"Khong the cat video Gemini phan {index + 1}.")
        outputs.append(output_path)
    return boundaries, outputs


def _bg_cleanup_loop():
    """Tu dong don dep: preview TTS cu + TASKS da hoan thanh."""
    import time
    preview_dir = os.path.join(WORKSPACE_DIR, "static", "temp_preview")
    while True:
        try:
            now = time.time()
            # Don dep preview TTS cu hon 5 phut
            for filename in os.listdir(preview_dir):
                filepath = os.path.join(preview_dir, filename)
                if os.path.isfile(filepath) and now - os.path.getmtime(filepath) > 300:
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
            # Don dep TASKS da hoan thanh/that bai hon 30 phut
            with task_lock:
                expired = [k for k, v in TASKS.items()
                          if v["status"] in ("success", "failed")
                          and now - v.get("created_at", 0) > 1800]
                for k in expired:
                    del TASKS[k]
        except Exception:
            pass
        time.sleep(600)

threading.Thread(target=_bg_cleanup_loop, daemon=True).start()

# =====================================================================
# BACKGROUND TASK RUNNERS
# =====================================================================
def normalize_simple_gemini_timeline(script_text, video_duration_sec):
    """Apply only the minimum formatting and timestamp safety needed by rendering."""
    timeline = str(script_text or "").strip()
    timeline = format_timeline_rows(timeline)
    timeline = remove_non_story_timeline_rows(timeline)
    timeline = clamp_final_timeline_end(timeline, video_duration_sec)
    timeline = repair_nonpositive_timeline_ranges(timeline, video_duration_sec)
    timeline = trim_timeline_ends_to_locked_starts(timeline)
    validate_timeline_ranges(timeline, video_duration_sec)
    if not timeline.strip():
        raise ValueError("Gemini khong tra ve dong TIME hop le.")
    return timeline.strip()


def _analyze_video_bg_once(
    task_id, api_key, model_name, video_filename, style, custom_prompt,
    enable_multi_voice=False, review_minutes="auto",
):
    def update_prog(prog, msg):
        with task_lock:
            TASKS[task_id]["progress"] = prog
            TASKS[task_id]["message"] = msg

    compressed_path = ""
    upload_staging_dir = ""
    remote_files = []
    client = None

    def cleanup_analysis_artifacts():
        if client is not None:
            for remote_file in list(remote_files):
                try:
                    client.files.delete(name=remote_file.name)
                except Exception:
                    pass
            remote_files.clear()
        if compressed_path and os.path.isfile(compressed_path):
            try:
                os.remove(compressed_path)
            except OSError:
                pass
        if upload_staging_dir and os.path.isdir(upload_staging_dir):
            shutil.rmtree(upload_staging_dir, ignore_errors=True)

    try:
        video_path = os.path.join(WORKSPACE_DIR, video_filename)
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Khong tim thay video: {video_filename}")

        update_prog(10, "Dang cau hinh ket noi API Gemini...")
        from google.genai import types as genai_types
        generation_http_opts = genai_types.HttpOptions(
            # One stalled generation must not hold the UI for 15 minutes per
            # attempt. Two bounded attempts below still recover transient
            # failures without turning one request into a 45-minute wait.
            timeout=8 * 60 * 1000,
            retry_options=genai_types.HttpRetryOptions(
                # Recovery is owned by generate_gemini_text_with_recovery.
                # Keep the SDK from multiplying the same request internally.
                attempts=1,
                initial_delay=1.0,
                max_delay=30.0,
                exp_base=2.0,
                jitter=1.0,
                http_status_codes=[408, 429, 500, 502, 503, 504],
            ),
        )
        client = genai.Client(api_key=api_key, http_options=generation_http_opts)
        upload_staging_dir = tempfile.mkdtemp(prefix=f"gemini_{task_id[:8]}_")

        # One analysis format for every source duration. This copy preserves
        # the complete timeline; only spatial/temporal bitrate is reduced.
        update_prog(15, "Dang tao ban nen Gemini 480p voi FPS thich ung...")
        from video_renderer import get_ffmpeg_exe, get_video_info
        ffmpeg_exe = get_ffmpeg_exe()
        
        compressed_path = os.path.join(upload_staging_dir, "source_proxy.mp4")
        try:
            video_duration_sec, _, _ = get_video_info(video_path)
            proxy_fps = analysis_proxy_fps(video_duration_sec)
            orig_size_mb = os.path.getsize(video_path) / (1024 * 1024)
            update_prog(12, f"Dang nen video {orig_size_mb:.0f}MB o {proxy_fps} FPS, giu nguyen timeline...")
            cmd_compress = [
                ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
                "-i", video_path,
                "-vf", f"scale=-2:480,fps={proxy_fps}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "48k", "-ac", "1", "-ar", "24000",
                "-movflags", "+faststart",
                compressed_path,
            ]
            res = subprocess.run(
                cmd_compress,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3600,
            )
            if res.returncode == 0 and os.path.exists(compressed_path):
                compressed_mb = os.path.getsize(compressed_path) / (1024 * 1024)
                update_prog(18, f"Da nen Gemini: {orig_size_mb:.0f}MB -> {compressed_mb:.0f}MB")
                upload_path = compressed_path
            else:
                upload_path = video_path
        except subprocess.TimeoutExpired:
            update_prog(18, "Nen trung gian qua lau; se tai file goc len Gemini...")
            upload_path = video_path
        except Exception:
            upload_path = video_path

        video_duration_sec, _, _ = get_video_info(video_path)
        import time

        # Use the bounded review budget selected by the user. Auto mode is
        # intentionally capped at 20 minutes.
        (
            target_recap_minutes,
            target_recap_words,
            min_recap_words,
            max_recap_words,
        ) = calculate_script_budget(
            video_duration_sec / 60.0,
            review_minutes=review_minutes,
            style=style,
        )
        part_word_targets = three_part_targets(target_recap_words)
        update_prog(20, "Dang cat ban nen thanh 3 phan bang nhau...")
        boundaries, part_paths = split_video_into_three_parts(
            upload_path, upload_staging_dir, video_duration_sec, ffmpeg_exe
        )

        selected_model = resolve_selected_gemini_model(model_name)
        pass1_config = genai_types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=32768,
            response_mime_type="application/json",
        )
        part_responses = []
        pass1_calls = 0
        for part_index, part_path in enumerate(part_paths):
            part_number = part_index + 1
            upload_size_mb = os.path.getsize(part_path) / (1024 * 1024)
            upload_timeout = max(120, int(upload_size_mb * 3))
            client_upload = genai.Client(
                api_key=api_key,
                http_options=genai_types.HttpOptions(timeout=upload_timeout * 1000),
            )
            progress_base = 25 + part_index * 14
            update_prog(
                progress_base,
                f"Dang tai phan {part_number}/3 ({upload_size_mb:.0f}MB) len Gemini...",
            )
            part_file = upload_gemini_file_with_recovery(
                client_upload,
                part_path,
                label=f"video phan {part_number}/3",
                update_progress=lambda message, p=progress_base: update_prog(p, message),
                max_attempts=5,
            )
            remote_files.append(part_file)
            wait_for_gemini_file(
                client,
                part_file.name,
                max_wait_seconds=900,
                poll_interval=5,
                sleep_fn=time.sleep,
                on_wait=lambda waited, limit, n=part_number, p=progress_base: update_prog(
                    p + 3, f"Gemini dang xu ly phan {n}/3... ({waited}s/{limit}s)"
                ),
            )

            part_uri = str(getattr(part_file, "uri", "") or "").strip()
            part_mime = str(getattr(part_file, "mime_type", "") or "video/mp4")
            if part_uri:
                part_content = genai_types.Part.from_uri(
                    file_uri=part_uri,
                    mime_type=part_mime,
                    media_resolution=(
                        genai_types.PartMediaResolutionLevel.MEDIA_RESOLUTION_LOW
                    ),
                )
                part_content.video_metadata = genai_types.VideoMetadata(fps=1.0)
            else:
                part_content = part_file

            prompt = build_single_part_video_prompt(
                part_number,
                boundaries[part_number] - boundaries[part_index],
                boundaries[part_index],
                boundaries[part_number],
                part_word_targets[part_index],
                style=style,
                custom_prompt=custom_prompt,
                enable_multi_voice=enable_multi_voice,
            )
            part_error = None
            accepted_response = None
            # The first response may be retried once only for malformed JSON.
            # Missing coverage is repaired incrementally below without
            # discarding valid scenes that Gemini already produced.
            for part_attempt in range(2):
                update_prog(
                    progress_base + 7,
                    f"Gemini dang viet phan {part_number}/3"
                    + (" (thu lai 1/1)..." if part_attempt else "..."),
                )
                attempt_prompt = prompt
                if part_attempt and part_error is not None:
                    attempt_prompt += (
                        "\n\nLẦN TRƯỚC KHÔNG ĐẠT: " + str(part_error)
                        + "\nBẮT BUỘC điều chỉnh đúng số cảnh và khoảng từ đã nêu, rồi trả lại JSON hoàn chỉnh của phần này."
                    )
                try:
                    pass1_calls += 1
                    response_text = generate_gemini_text_with_recovery(
                        client,
                        selected_model,
                        [part_content, attempt_prompt],
                        pass1_config,
                        f"Gemini phan {part_number}/3",
                        update_progress=lambda message, p=progress_base + 7: update_prog(p, message),
                        stream=True,
                        max_attempts=3,
                    )
                    single_part_missing_requirements(
                        response_text,
                        boundaries[part_number] - boundaries[part_index],
                        part_word_targets[part_index],
                    )
                    accepted_response = response_text
                    break
                except Exception as exc:
                    part_error = exc
                    if part_attempt:
                        raise

            clip_duration = boundaries[part_number] - boundaries[part_index]
            continuation_json_fallback = False
            continuation_incomplete_fallback = False
            for continuation_round in range(MAX_CONTINUATION_ROUNDS):
                requirements = single_part_missing_requirements(
                    accepted_response, clip_duration, part_word_targets[part_index]
                )
                if not requirements["regions"]:
                    break
                update_prog(
                    progress_base + 9,
                    f"Gemini dang viet tiep vung thieu cua phan {part_number}/3 "
                    f"(lan {continuation_round + 1}/{MAX_CONTINUATION_ROUNDS})...",
                )
                continuation_prompt = build_single_part_continuation_prompt(
                    part_number,
                    clip_duration,
                    part_word_targets[part_index],
                    accepted_response,
                    requirements,
                )
                if continuation_round:
                    continuation_prompt += (
                        "\n\nLuot bo sung truoc khong them duoc scene hop le. "
                        "Hay chi tra cac scene moi dung region va scene_id duoc yeu cau."
                    )
                addition_error = None
                added_count = 0
                merged_response = accepted_response
                for json_attempt in range(2):
                    attempt_prompt = continuation_prompt
                    if json_attempt and addition_error is not None:
                        attempt_prompt += (
                            "\n\nPHẢN HỒI TRƯỚC KHÔNG PHẢI JSON HỢP LỆ: "
                            + str(addition_error)
                            + "\nChỉ trả một JSON object hoàn chỉnh, không thêm bất kỳ chữ nào bên ngoài JSON."
                        )
                        update_prog(
                            progress_base + 9,
                            f"Gemini dang tra lai JSON bo sung phan {part_number}/3...",
                        )
                    pass1_calls += 1
                    additions_text = generate_gemini_text_with_recovery(
                        client,
                        selected_model,
                        [part_content, attempt_prompt],
                        pass1_config,
                        f"Gemini bo sung phan {part_number}/3",
                        update_progress=lambda message, p=progress_base + 9: update_prog(p, message),
                        stream=True,
                        max_attempts=3,
                    )
                    try:
                        merged_response, added_count = merge_single_part_additions(
                            accepted_response,
                            additions_text,
                            requirements["regions"],
                        )
                        addition_error = None
                        break
                    except ValueError as exc:
                        addition_error = exc

                if addition_error is not None:
                    print(
                        f"[!] JSON bo sung phan {part_number} hong sau 2 lan; "
                        "giu ket qua hien tai.",
                        flush=True,
                    )
                    continuation_json_fallback = True
                    break
                if added_count == 0:
                    continuation_incomplete_fallback = True
                    print(
                        f"[!] Bo sung phan {part_number} lan "
                        f"{continuation_round + 1}/{MAX_CONTINUATION_ROUNDS} "
                        "khong co scene hop le; thu luot tiep theo.",
                        flush=True,
                    )
                    continue
                accepted_response = merged_response
                continuation_incomplete_fallback = False

            remaining_requirements = single_part_missing_requirements(
                accepted_response, clip_duration, part_word_targets[part_index]
            )
            if remaining_requirements["regions"]:
                continuation_incomplete_fallback = True
                print(
                    f"[!] Phan {part_number} van thieu mot so region sau "
                    f"{MAX_CONTINUATION_ROUNDS} luot; giu ket qua tot nhat va tiep tuc.",
                    flush=True,
                )
            validate_single_part_response(
                accepted_response,
                part_number,
                part_word_targets[part_index],
                require_coverage=not (
                    continuation_json_fallback or continuation_incomplete_fallback
                ),
            )
            part_responses.append(accepted_response)

        pass1_payload = merge_single_part_responses(
            part_responses, boundaries, target_recap_words
        )

        issues_before = inspect_three_part_payload(pass1_payload)
        final_payload = pass1_payload
        repair_calls = 0
        repair_fallback_used = False

        if issues_before:
            update_prog(
                78,
                f"Dang tao clip cho {len(issue_scene_ids(pass1_payload, issues_before))} "
                "canh can sua...",
            )
            try:
                repair_ids = issue_scene_ids(pass1_payload, issues_before)
                repair_scenes = scenes_by_ids(pass1_payload, repair_ids)
                reel_events = []
                for scene in repair_scenes:
                    start, end, _ = extract_time_range(
                        f"[TIME={scene['start']}-{scene['end']}] x"
                    )
                    if start is not None and end is not None and end > start:
                        reel_events.append({
                            "id": scene["scene_id"],
                            "start": float(start),
                            "end": float(end),
                        })
                repair_reel = build_timeline_verification_reel(
                    video_path,
                    reel_events,
                    upload_staging_dir,
                    ffmpeg_exe=ffmpeg_exe,
                    max_workers=2,
                    output_fps=3.0,
                )
                repair_upload_path = stage_gemini_upload(
                    repair_reel, upload_staging_dir, "repair_scenes"
                )
                repair_file = upload_gemini_file_with_recovery(
                    client_upload,
                    repair_upload_path,
                    label="clip sua canh",
                    update_progress=lambda message: update_prog(82, message),
                    max_attempts=5,
                )
                remote_files.append(repair_file)
                wait_for_gemini_file(
                    client,
                    repair_file.name,
                    max_wait_seconds=900,
                    poll_interval=5,
                    sleep_fn=time.sleep,
                    on_wait=lambda waited, limit: update_prog(
                        82, f"Gemini dang xu ly clip loi... ({waited}s/{limit}s)"
                    ),
                )
                repair_uri = str(getattr(repair_file, "uri", "") or "").strip()
                repair_mime = str(
                    getattr(repair_file, "mime_type", "") or "video/mp4"
                )
                if repair_uri:
                    repair_content = genai_types.Part.from_uri(
                        file_uri=repair_uri,
                        mime_type=repair_mime,
                        media_resolution=(
                            genai_types.PartMediaResolutionLevel.MEDIA_RESOLUTION_LOW
                        ),
                    )
                    repair_content.video_metadata = genai_types.VideoMetadata(fps=2.0)
                else:
                    repair_content = repair_file

                best_payload = pass1_payload
                best_issue_count = len(issues_before)
                last_repair_error = None
                for repair_attempt in range(2):
                    update_prog(
                        86,
                        "Gemini dang sua dung cac scene_id bi loi"
                        + (" (thu lai 1/1)..." if repair_attempt else "..."),
                    )
                    remaining_issues = inspect_three_part_payload(best_payload)
                    retry_prompt = build_selective_repair_prompt(
                        best_payload, remaining_issues
                    )
                    if repair_attempt and last_repair_error is not None:
                        retry_prompt += (
                            "\n\nLAN SUA TRUOC KHONG HOP LE: "
                            + str(last_repair_error)
                            + "\nChi tra JSON repairs/additions/drop_scene_ids."
                        )
                    try:
                        repair_calls += 1
                        repaired_text = generate_gemini_text_with_recovery(
                            client,
                            selected_model,
                            [repair_content, retry_prompt],
                            genai_types.GenerateContentConfig(temperature=0.2),
                            "Gemini luot 2",
                            update_progress=lambda message: update_prog(86, message),
                            stream=True,
                            max_attempts=3,
                        )
                        candidate = apply_selective_repairs(
                            best_payload, repaired_text, repair_ids
                        )
                        candidate_issue_count = len(
                            inspect_three_part_payload(candidate)
                        )
                        # A retry that does not strictly improve validation is
                        # discarded so it cannot overwrite the better version.
                        if candidate_issue_count < best_issue_count:
                            best_payload = candidate
                            best_issue_count = candidate_issue_count
                        if candidate_issue_count == 0:
                            break
                        last_repair_error = ValueError(
                            f"Con {candidate_issue_count} loi sau khi sua."
                        )
                    except Exception as exc:
                        last_repair_error = exc
                final_payload = best_payload
                repair_fallback_used = best_payload is pass1_payload
            except Exception as repair_error:
                # Pass 2 is optional. A bad repair must never destroy the usable
                # scene map and narration already produced by pass 1.
                print(
                    f"[!] Luot 2 that bai; giu ket qua luot 1: {repair_error}",
                    flush=True,
                )
                repair_fallback_used = True

        final_payload = clamp_payload_narrations(final_payload)
        script_text = payload_to_timeline(final_payload)
        script_text = normalize_simple_gemini_timeline(
            script_text, video_duration_sec
        )
        word_count, event_count = measure_script_narration(script_text)

        length_within_range = min_recap_words <= word_count <= max_recap_words
        update_prog(95, "Dang don dep...")
        cleanup_analysis_artifacts()
        with task_lock:
            TASKS[task_id]["status"] = "success"
            TASKS[task_id]["progress"] = 100
            TASKS[task_id]["message"] = "Tao kich ban thanh cong!"
            TASKS[task_id]["result"] = script_text
            TASKS[task_id]["analysis_method"] = "three-part-selective-repair"
            TASKS[task_id]["generation_calls"] = pass1_calls + repair_calls
            TASKS[task_id]["pass1_calls"] = pass1_calls
            TASKS[task_id]["repair_calls"] = repair_calls
            TASKS[task_id]["repair_fallback_used"] = repair_fallback_used
            TASKS[task_id]["issues_before_repair"] = len(issues_before)
            TASKS[task_id]["issues_after_repair"] = len(
                inspect_three_part_payload(final_payload)
            )
            TASKS[task_id]["story_parts"] = final_payload["parts"]
            TASKS[task_id]["characters"] = final_payload["characters"]
            TASKS[task_id]["target_minutes"] = target_recap_minutes
            TASKS[task_id]["target_words"] = target_recap_words
            TASKS[task_id]["min_words"] = min_recap_words
            TASKS[task_id]["max_words"] = max_recap_words
            TASKS[task_id]["actual_words"] = word_count
            TASKS[task_id]["actual_scenes"] = event_count
            TASKS[task_id]["length_within_range"] = length_within_range
            TASKS[task_id]["model_used"] = selected_model
        return

    except Exception as e:
        cleanup_analysis_artifacts()
        if is_daily_project_gemini_quota_error(e):
            raise
        traceback.print_exc()
        err_msg = str(e)
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower():
            user_msg = "Đã hết quota API miễn phí hôm nay! Quota sẽ tự động reset sau 24h. Vui lòng thử lại sau."
        elif is_gemini_auth_error(e):
            user_msg = "API Key không hợp lệ! Vui lòng kiểm tra lại key."
        elif "WinError 10013" in err_msg or "forbidden by its access permissions" in err_msg:
            user_msg = (
                "Windows đang chặn kết nối HTTPS của tiến trình tool tới Gemini. "
                "Hãy chạy tool ngoài sandbox và cho phép Python qua Firewall/VPN."
            )
        else:
            user_msg = f"Lỗi: {err_msg[:300]}"
        with task_lock:
            TASKS[task_id]["status"] = "failed"
            TASKS[task_id]["message"] = user_msg


def analyze_video_bg(
    task_id, api_keys, model_name, video_filename, style, custom_prompt,
    enable_multi_voice=False, review_minutes="auto",
):
    """Run analysis by API priority and rotate only on daily project quota."""
    keys = [str(key or "").strip() for key in (api_keys or []) if str(key or "").strip()]
    if not keys:
        with task_lock:
            TASKS[task_id]["status"] = "failed"
            TASKS[task_id]["message"] = "Khong co API Key Gemini hop le."
        return

    exhausted = []
    for index, api_key in enumerate(keys, start=1):
        with task_lock:
            TASKS[task_id]["active_api_priority"] = index
            TASKS[task_id]["api_key_count"] = len(keys)
            TASKS[task_id]["message"] = (
                f"Dang dung Gemini API {index}/{len(keys)}..."
            )
        try:
            _analyze_video_bg_once(
                task_id,
                api_key,
                model_name,
                video_filename,
                style,
                custom_prompt,
                enable_multi_voice,
                review_minutes,
            )
            return
        except Exception as error:
            if not is_daily_project_gemini_quota_error(error):
                with task_lock:
                    TASKS[task_id]["status"] = "failed"
                    TASKS[task_id]["message"] = f"Loi: {str(error)[:300]}"
                return
            exhausted.append(index)
            if index < len(keys):
                with task_lock:
                    TASKS[task_id]["progress"] = 5
                    TASKS[task_id]["message"] = (
                        f"API {index} da het quota ngay; dang khoi dong lai bang "
                        f"API {index + 1}..."
                    )

    with task_lock:
        TASKS[task_id]["status"] = "failed"
        TASKS[task_id]["message"] = (
            "Tat ca Gemini API da het quota ngay: "
            + ", ".join(f"API {index}" for index in exhausted)
            + ". Hay cho quota reset hoac thay API Key khac."
        )

def render_video_bg(task_id, input_video_name, bgm_name, bgm_volume, voice_name, voice_rate, sentences, options, export_mode):
    def update_prog(prog, msg):
        update_task_fields(task_id, progress=prog, message=msg, heartbeat_at=time.time())
        append_task_log(task_id, "STEP", f"{prog}% - {msg}")

    def diagnostic_log(level, message, details=""):
        append_task_log(task_id, level, message, details)

    try:
        diagnostic_log("INFO", f"Bat dau render: mode={export_mode}, video={input_video_name}, so_dong={len(sentences)}, voice={voice_name}, rate={voice_rate}, pid={os.getpid()}")
        diagnostic_log("INFO", f"Nhat ky day du: {_task_log_path(task_id)}")
        input_video_path = os.path.join(WORKSPACE_DIR, input_video_name)
        
        bgm_path = ""
        if bgm_name:
            bgm_path = os.path.join(WORKSPACE_DIR, "static", "bgm", bgm_name)

        if export_mode == "capcut_zip":
            res_url = export_capcut_package(
                task_id,
                input_video_path,
                bgm_path,
                bgm_volume,
                voice_name,
                voice_rate,
                sentences,
                options,
                update_prog,
                diagnostic_log
            )
        else:
            res_url = render_review_video_ffmpeg(
                task_id,
                input_video_path,
                bgm_path,
                bgm_volume,
                voice_name,
                voice_rate,
                sentences,
                options,
                update_prog,
                diagnostic_log
            )

        export_path, export_error = copy_result_to_export_directory(res_url)
        update_task_fields(
            task_id,
            status="success",
            progress=100,
            message="Xuất video thành công!",
            result=res_url,
            export_path=export_path,
            export_error=export_error,
            heartbeat_at=time.time(),
        )

    except Exception as e:
        error_trace = traceback.format_exc()
        diagnostic_log("ERROR", f"Render that bai: {type(e).__name__}: {e}", error_trace)
        # Tao thong bao loi than thien cho user
        err_msg = str(e)
        if "ffmpeg" in err_msg.lower() or "returncode" in err_msg.lower():
            err_msg = "Loi xu ly video (FFmpeg). Vui long thu lai hoac kiem tra video goc."
        elif "edge_tts" in err_msg.lower() or "tts" in err_msg.lower():
            err_msg = "Loi tao giong doc AI. Kiem tra ket noi internet va thu lai."
        elif "genai" in err_msg.lower() or "api" in err_msg.lower():
            err_msg = "Loi ket noi API Gemini. Kiem tra API Key va thu lai."
        update_task_fields(
            task_id,
            status="failed",
            message=f"Loi: {err_msg}",
            heartbeat_at=time.time(),
        )
    finally:
        diagnostic_log("INFO", "Tac vu render da ket thuc khoi worker (thanh cong hoac that bai).")

# =====================================================================
# FLASK ROUTING & API ENDPOINTS
# =====================================================================
@app.route("/")
def index():
    return render_template("index.html", default_key=DEFAULT_API_KEY)

@app.route("/source-video/<path:filename>")
def source_video(filename):
    """Serve an uploaded source video stored at the workspace root."""
    if os.path.splitext(filename)[1].lower() not in ALLOWED_VIDEO_EXT:
        return jsonify({"success": False, "message": "Dinh dang video khong hop le."}), 404
    return send_from_directory(WORKSPACE_DIR, filename)

@app.route("/api/export-directory", methods=["GET", "POST"])
def export_directory():
    if request.method == "GET":
        configured_dir = get_configured_export_directory()
        valid = bool(configured_dir and os.path.isdir(configured_dir))
        return jsonify({
            "directory": configured_dir,
            "valid": valid,
            "message": "" if valid or not configured_dir else "Thu muc da luu khong con ton tai. Vui long chon lai."
        })

    # The tool runs locally, so this opens the Windows folder picker.
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected_dir = filedialog.askdirectory(
            parent=root,
            initialdir=get_export_directory() or WORKSPACE_DIR,
            title="Chon thu muc xuat video"
        )
        root.destroy()
    except Exception as exc:
        return jsonify({"success": False, "message": f"Khong mo duoc hop chon thu muc: {exc}"}), 500

    if not selected_dir:
        return jsonify({"success": False, "cancelled": True, "message": "Ban chua chon thu muc."})

    selected_dir = os.path.abspath(os.path.normpath(str(selected_dir).strip()))
    if not os.path.isdir(selected_dir):
        return jsonify({"success": False, "message": "Thu muc vua chon khong ton tai."}), 400

    # Check write permission before a long render starts.
    probe_path = os.path.join(selected_dir, f".review_tool_write_test_{uuid.uuid4().hex}.tmp")
    try:
        with open(probe_path, "xb") as probe:
            probe.write(b"ok")
            probe.flush()
            os.fsync(probe.fileno())
        os.remove(probe_path)
    except OSError as exc:
        try:
            if os.path.exists(probe_path):
                os.remove(probe_path)
        except OSError:
            pass
        return jsonify({"success": False, "message": f"Khong co quyen ghi vao thu muc: {exc}"}), 400

    settings = _load_settings()
    settings["export_directory"] = selected_dir
    try:
        _save_settings(settings)
    except OSError as exc:
        return jsonify({"success": False, "message": f"Khong luu duoc cai dat: {exc}"}), 500
    return jsonify({"success": True, "directory": selected_dir})

@app.route("/api/check-key", methods=["POST"])
def check_key():
    data = request.json or {}
    api_key = data.get("api_key", "").strip()
    key_error = validate_gemini_api_key(api_key)
    if key_error:
        return jsonify({"valid": False, "message": key_error}), 400
        
    try:
        client = genai.Client(api_key=api_key)
        
        # Chi dung models.list() de xac thuc key (MIEN PHI, khong ton quota)
        models_list = list(client.models.list())
        available = []
        for m in models_list:
            name = m.name.replace("models/", "") if hasattr(m, 'name') else str(m)
            if name in build_model_candidates(name, [m]):
                available.append(name)
        return jsonify({"valid": True, "message": "Kết nối API Gemini thành công!", "models": available})
    except Exception as e:
        err_msg = str(e)
        if is_gemini_auth_error(e):
            return jsonify({"valid": False, "message": "API Key không hợp lệ! Kiểm tra lại tại https://aistudio.google.com/apikey"})
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower():
            return jsonify({"valid": True, "message": "API Key hợp lệ! Nhưng đã hết quota miễn phí hôm nay. Quota sẽ reset sau 24h.", "models": ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]})
        if "WinError 10013" in err_msg or "forbidden by its access permissions" in err_msg:
            return jsonify({
                "valid": False,
                "message": (
                    "Windows đang chặn kết nối HTTPS tới Gemini. Hãy chạy tool ngoài sandbox "
                    "và cho phép python.exe qua Firewall/VPN."
                ),
            })
        return jsonify({"valid": False, "message": f"Lỗi kết nối: {err_msg[:200]}"})

@app.route("/api/list-videos", methods=["GET"])
def list_videos():
    try:
        return jsonify(normalize_existing_video_filenames(WORKSPACE_DIR))
    except OSError as exc:
        return jsonify({
            "success": False,
            "message": f"Khong the doi ten video sang ASCII: {exc}",
        }), 500

@app.route('/api/list-bgm', methods=['GET'])
def list_bgm():
    bgm_dir = os.path.join(app.static_folder, 'bgm')
    os.makedirs(bgm_dir, exist_ok=True)
    files = [f for f in os.listdir(bgm_dir) if f.lower().endswith(('.mp3', '.wav', '.m4a', '.aac'))]
    return jsonify(files)

@app.route('/api/list-sfx', methods=['GET'])
def list_sfx():
    sfx_dir = os.path.join(app.static_folder, 'sfx')
    os.makedirs(sfx_dir, exist_ok=True)
    files = [f for f in os.listdir(sfx_dir) if f.lower().endswith(('.mp3', '.wav', '.m4a', '.aac', '.ogg'))]
    return jsonify(files)

@app.route("/api/upload-file", methods=["POST"])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"success": False, "message": "Khong tim thay tep."})
        
    file = request.files['file']
    file_type = request.form.get("type", "video")
    
    if file.filename == '':
        return jsonify({"success": False, "message": "Ten tep khong hop le."})
        
    if file:
        original_filename = os.path.basename(file.filename.replace("\\", "/"))
        filename = ascii_upload_filename(original_filename)
        ext = os.path.splitext(filename)[1].lower()
        
        # Kiem tra dinh dang file
        if file_type == "video":
            if ext not in ALLOWED_VIDEO_EXT:
                return jsonify({"success": False, "message": f"Dinh dang {ext} khong ho tro. Chi chap nhan: {', '.join(ALLOWED_VIDEO_EXT)}"})
            target_path = available_upload_path(WORKSPACE_DIR, filename)
        else:
            if ext not in ALLOWED_AUDIO_EXT:
                return jsonify({"success": False, "message": f"Dinh dang {ext} khong ho tro. Chi chap nhan: {', '.join(ALLOWED_AUDIO_EXT)}"})
            target_path = available_upload_path(
                os.path.join(WORKSPACE_DIR, "static", "bgm"), filename
            )

        filename = os.path.basename(target_path)
            
        file.save(target_path)
        return jsonify({
            "success": True,
            "filename": filename,
            "original_filename": original_filename,
            "renamed": filename != original_filename,
        })
        
    return jsonify({"success": False, "message": "Loi khong xac dinh khi luu file."})

@app.route("/api/analyze-video", methods=["POST"])
def analyze_video():
    data = request.json or {}
    raw_api_keys = data.get("api_keys", [])
    if not isinstance(raw_api_keys, list):
        raw_api_keys = []
    legacy_key = str(data.get("api_key", "") or "").strip()
    candidates = raw_api_keys if raw_api_keys else [legacy_key or DEFAULT_API_KEY]
    api_keys = []
    for slot_index, raw_key in enumerate(candidates[:3], start=1):
        key = str(raw_key or "").strip()
        if not key:
            continue
        if key in api_keys:
            return jsonify({
                "success": False,
                "message": f"API {slot_index} bi trung voi API truoc do.",
            }), 400
        api_keys.append(key)
    model_name = data.get("model_name", "gemini-3.1-flash-lite")
    video_filename = os.path.basename(
        str(data.get("video_name", "")).replace("\\", "/")
    )
    style = data.get("style", "dramatic")
    custom_prompt = data.get("custom_prompt", "").strip()
    enable_multi_voice = data.get("enable_multi_voice", False)
    project_id = str(data.get("project_id", "")).strip() or str(uuid.uuid4())
    review_minutes_raw = data.get("review_minutes", "auto")
    review_minutes = "auto" if str(review_minutes_raw).lower() == "auto" else max(3, min(20, int(review_minutes_raw)))

    if not api_keys:
        return jsonify({"success": False, "message": "Chua nhap Gemini API Key."}), 400
    for index, api_key in enumerate(api_keys, start=1):
        key_error = validate_gemini_api_key(api_key)
        if key_error:
            return jsonify({
                "success": False,
                "message": f"API {index}: {key_error}",
            }), 400
    
    if not video_filename:
        return jsonify({"success": False, "message": "Chưa chọn video gốc."})

    try:
        video_filename = normalize_video_file(WORKSPACE_DIR, video_filename)
    except OSError as exc:
        return jsonify({
            "success": False,
            "message": f"Khong the doi ten video sang ASCII: {exc}",
        }), 500
        
    task_id = create_task(project_id, video_filename)
    with task_lock:
        PROJECTS[project_id] = {
            "video_name": video_filename,
            "analysis_task_id": task_id,
            "created_at": time.time(),
        }
    
    # Khởi chạy tiểu trình phân tích nền
    thread = threading.Thread(
        target=analyze_video_bg,
        args=(
            task_id, api_keys, model_name, video_filename, style, custom_prompt,
            enable_multi_voice, review_minutes,
        )
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({"success": True, "task_id": task_id})

@app.route("/api/split-sentences", methods=["POST"])
def split_sentences():
    data = request.json or {}
    raw_text = data.get("text", "")
    text = (
        "\n".join(str(item) for item in raw_text)
        if isinstance(raw_text, list)
        else str(raw_text or "")
    ).strip()
    if not text:
        return jsonify([])

    try:
        sentences = split_text_into_sentences(text)
    except Exception as exc:
        # A completed Gemini script must remain editable even if an unusual
        # token trips the richer TTS parser. Preserve each TIME row verbatim.
        print(f"[!] Loi parser phan doan; dung fallback theo dong: {exc}", flush=True)
        sentences = [
            row.strip() for row in format_timeline_rows(text).splitlines()
            if row.strip()
        ]
    sentences = [str(item).strip() for item in sentences if str(item).strip()]
    if not sentences:
        sentences = [text]
    return jsonify(sentences)

@app.route("/api/preview-tts", methods=["POST"])
def preview_tts():
    data = request.json or {}
    text = data.get("text", "")
    voice = data.get("voice", "vi-VN-HoaiMyNeural")
    rate = data.get("rate", "+10%")
    
    if not text:
        return jsonify({"success": False, "message": "Nội dung câu trống."})
        
    filename = f"preview_{uuid.uuid4()}.mp3"
    filepath = os.path.join(WORKSPACE_DIR, "static", "temp_preview", filename)
    
    async def run_tts():
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await asyncio.wait_for(communicate.save(filepath), timeout=15.0)
        
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_tts())
    except asyncio.TimeoutError:
        return jsonify({"success": False, "message": "TTS qua thoi gian cho (15s). Thu lai."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        loop.close()
        
    return jsonify({"success": True, "url": f"/static/temp_preview/{filename}"})

@app.route("/api/render-video", methods=["POST"])
def render_video():
    data = request.json or {}
    video_name = data.get("video_name", "")
    bgm_name = data.get("bgm_name", "")
    bgm_volume = float(data.get("bgm_volume", 12)) / 100.0
    voice_name = data.get("voice_name", "vi-VN-HoaiMyNeural")
    voice_rate = data.get("voice_rate", "-10%")
    sentences = data.get("sentences", [])
    raw_sentence_rows = (
        [str(item) for item in sentences]
        if isinstance(sentences, list) else [str(sentences or "")]
    )

    def is_timeline_control_row(row):
        """VOICE/SFX rows control audio and must not count as visual ranges."""
        _, _, body = extract_time_range(str(row or ""))
        return bool(re.fullmatch(
            r"\s*(?:<|\[)(?:VOICE|SFX)=[^>\]]+(?:>|\])\s*",
            body,
            flags=re.IGNORECASE,
        ))

    raw_timeline_text = "\n".join(
        row for row in raw_sentence_rows if not is_timeline_control_row(row)
    )
    # Repair stale/browser timelines before persisting a render payload. Older
    # parsers collapsed timestamps beyond 99:59 into one oversized TTS item.
    if isinstance(sentences, list) and sentences:
        sentences = split_text_into_sentences("\n".join(str(item) for item in sentences))
    project_id = str(data.get("project_id", "")).strip()
    review_minutes_raw = data.get("review_minutes", "auto")
    
    # Cấu hình video
    options = {
        "mirror": data.get("mirror", True),
        "zoom_factor": float(data.get("zoom_factor", 1.08)),
        "speed_factor": float(data.get("speed_factor", 1.0)),
        "contrast": float(data.get("contrast", 1.05)),
        "brightness": float(data.get("brightness", 1.0)),
        "font_size": int(data.get("font_size", 42)),
        "font_color": data.get("font_color", "#FFFFFF"),
        "bg_opacity": int(data.get("bg_opacity", 150)),
        "font_path": "C:\\Windows\\Fonts\\arialbd.ttf",
        "aspect_ratio": data.get("aspect_ratio", "9:16"),
        "enable_subtitles": data.get("enable_subtitles", True),
        "enable_zoom_crop": data.get("enable_zoom_crop", True),
        "enable_ken_burns": data.get("enable_ken_burns", False)
    }
    if str(review_minutes_raw).lower() != "auto":
        review_minutes = max(3, min(20, int(review_minutes_raw)))
        options["review_minutes"] = review_minutes
        spoken_text = re.sub(r'\[(?:TIME|BEAT|VOICE|SFX)=[^\]]+\]|<(?:VOICE|SFX)=[^>]+>', ' ', ' '.join(sentences), flags=re.IGNORECASE)
        spoken_words = len(spoken_text.split())
        max_render_words = int(review_minutes * 150 * 1.20)
        if spoken_words > max_render_words:
            return jsonify({
                "success": False,
                "message": (
                    f"Kich ban co {spoken_words} tu, vuot muc {review_minutes} phut "
                    f"(toi da {max_render_words} tu). Hay rut gon kich ban truoc khi xuat."
                )
            }), 400
    
    if not video_name:
        return jsonify({"success": False, "message": "Chưa chọn video gốc."})
    if not sentences:
        return jsonify({"success": False, "message": "Kịch bản trống."})

    # Validate raw editor ranges before older parsing code can clamp, split or
    # interpolate a malformed timestamp into a plausible-looking wrong scene.
    try:
        from video_renderer import get_video_info
        source_duration, _, _ = get_video_info(os.path.join(WORKSPACE_DIR, video_name))
        validate_timeline_ranges(raw_timeline_text, source_duration)
    except (OSError, ValueError, RuntimeError) as exc:
        return jsonify({
            "success": False,
            "message": f"Timeline khóa không hợp lệ: {exc}"
        }), 400

    clean_timeline, range_starts, range_ends, visual_groups = parse_sentences_and_visual_beats(sentences)
    previous_end = -1.0
    validation_beats = []
    for index, (text, groups) in enumerate(zip(clean_timeline, visual_groups), start=1):
        if text.startswith(("<VOICE=", "<SFX=")):
            continue
        if groups:
            validation_beats.extend(
                (index, beat["text"], beat.get("start"), beat.get("end"))
                for beat in groups
            )
        else:
            validation_beats.append((index, text, range_starts[index - 1], range_ends[index - 1]))

    for index, text, start, end in validation_beats:
        if start is None or end is None:
            return jsonify({
                "success": False,
                "message": (
                    f"Dòng {index} chưa có TIME_END. Hãy tạo lại kịch bản Gemini theo dạng "
                    "[TIME=mm:ss-mm:ss] trước khi xuất."
                )
            }), 400
        if end <= start:
            return jsonify({
                "success": False,
                "message": f"Dòng {index} có TIME_END không lớn hơn TIME_START."
            }), 400
        if start < previous_end:
            return jsonify({
                "success": False,
                "message": f"Dòng {index} bị đi lùi hoặc chồng lên khoảng thời gian trước."
            }), 400
        previous_end = end
        
    if not project_id:
        return jsonify({
            "success": False,
            "message": "Phien du an khong hop le. Hay tai lai trang va tao lai kich ban Gemini."
        }), 409
    with task_lock:
        project = PROJECTS.get(project_id)
        if project is None and os.path.isfile(os.path.join(WORKSPACE_DIR, video_name)):
            # A server restart clears RAM-only project metadata. The browser
            # still owns the validated script/project id, so restore the local
            # project instead of forcing an expensive Gemini regeneration.
            project = {
                "video_name": video_name,
                "analysis_task_id": "restored-for-render",
                "created_at": time.time(),
            }
            PROJECTS[project_id] = project
    if not project or project.get("video_name") != video_name:
        return jsonify({
            "success": False,
            "message": "Video va kich ban khong cung mot phien. Hay tao lai kich ban cho video dang chon."
        }), 409

    task_id = create_task(project_id, video_name, persistent=True)

    export_mode = data.get("export_mode", "full_mp4")

    # Save inputs first, then launch a detached worker. Rendering is no longer
    # owned by Flask's lifetime, so a web-server restart cannot kill the job.
    save_task_payload(task_id, {
        "video_name": video_name,
        "bgm_name": bgm_name,
        "bgm_volume": bgm_volume,
        "voice_name": voice_name,
        "voice_rate": voice_rate,
        "sentences": sentences,
        "options": options,
        "export_mode": export_mode,
    })
    worker_script = os.path.join(WORKSPACE_DIR, "render_worker.py")
    worker_log_path = os.path.join(LOG_DIR, f"worker_{task_id}.log")
    worker_log = open(worker_log_path, "a", encoding="utf-8", buffering=1)
    popen_kwargs = {
        "cwd": WORKSPACE_DIR,
        "stdin": subprocess.DEVNULL,
        "stdout": worker_log,
        "stderr": worker_log,
        "close_fds": True,
        "env": {**os.environ, "PYTHONUTF8": "1"},
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        popen_kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, worker_script, task_id], **popen_kwargs)
    except Exception as exc:
        update_task_fields(
            task_id,
            status="failed",
            message=f"Khong khoi dong duoc worker render: {exc}",
        )
        return jsonify({"success": False, "message": str(exc)}), 500
    finally:
        worker_log.close()
    
    return jsonify({"success": True, "task_id": task_id})

@app.route("/api/task-status/<task_id>", methods=["GET"])
def task_status(task_id):
    persisted = load_task_state(task_id)
    if persisted is not None:
        heartbeat_at = float(persisted.get("heartbeat_at") or persisted.get("created_at") or 0)
        if persisted.get("status") in ("queued", "running") and time.time() - heartbeat_at > 30:
            worker_pid = int(persisted.get("worker_pid") or 0)
            worker_alive = False
            if worker_pid > 0:
                try:
                    os.kill(worker_pid, 0)
                    worker_alive = True
                except (OSError, ProcessLookupError, PermissionError):
                    worker_alive = False
            if not worker_alive:
                persisted.update({
                    "status": "failed",
                    "message": "Worker render da dung dot ngot. Bam Thu lai de tiep tuc tu cache TTS.",
                })
                save_task_state(task_id, persisted)
        with task_lock:
            TASKS[task_id] = persisted
        return jsonify(persisted)

    with task_lock:
        task = TASKS.get(task_id)
        
    if not task:
        return jsonify({"status": "not_found", "message": "Không tìm thấy tác vụ."})
        
    return jsonify(task)



# =====================================================================
# BACKGROUND TASK: AI VIDEO GENERATOR
# =====================================================================
def generate_ai_video_bg(task_id, api_key, script_text, style_prompt, bgm_name, bgm_volume, voice_name, voice_rate, options):
    def update_prog(prog, msg):
        with task_lock:
            TASKS[task_id]["progress"] = prog
            TASKS[task_id]["message"] = msg

    try:
        from tts_engine import split_text_into_sentences, generate_all_tts
        
        update_prog(5, "Đang phân tích kịch bản...")
        sentences = split_text_into_sentences(script_text)
        if not sentences:
            raise Exception("Kịch bản trống hoặc quá ngắn.")
            
        temp_dir = os.path.join(WORKSPACE_DIR, f"temp_{task_id}")
        os.makedirs(temp_dir, exist_ok=True)
        
        # 1. Generate Images
        image_paths = generate_all_images(api_key, sentences, style_prompt, temp_dir, options.get("aspect_ratio", "16:9"), update_prog)
        
        # 2. Generate Audio TTS
        update_prog(40, "Đang tổng hợp giọng nói AI (TTS)...")
        audio_paths, durations = generate_all_tts(sentences, voice_name, voice_rate, temp_dir)
        
        # 3. Resolve BGM Path
        bgm_path = ""
        if bgm_name:
            bgm_path = os.path.join(WORKSPACE_DIR, "static", "bgm", bgm_name)
            
        # 4. Render Final Video
        res_url = render_ai_image_video_ffmpeg(
            task_id,
            sentences,
            durations,
            audio_paths,
            image_paths,
            bgm_path,
            bgm_volume,
            options,
            update_prog
        )
        export_path, export_error = copy_result_to_export_directory(res_url)
        with task_lock:
            TASKS[task_id]["status"] = "success"
            TASKS[task_id]["progress"] = 100
            TASKS[task_id]["message"] = "Tạo Video Hoạt Hình AI thành công!"
            TASKS[task_id]["result"] = res_url
            TASKS[task_id]["export_path"] = export_path
            TASKS[task_id]["export_error"] = export_error

    except Exception as e:
        import traceback
        traceback.print_exc()
        with task_lock:
            TASKS[task_id]["status"] = "error"
            TASKS[task_id]["message"] = f"Lỗi: {str(e)}"


@app.route("/ai-video")
def ai_video():
    return render_template("ai_video.html", default_key=DEFAULT_API_KEY)

@app.route("/api/generate-ai-video", methods=["POST"])
def api_generate_ai_video():
    data = request.json or {}
    api_key = data.get("api_key", "").strip() or DEFAULT_API_KEY
    script_text = data.get("script_text", "").strip()
    style_prompt = data.get("style_prompt", "Vẽ nét đơn giản, màu sắc tươi sáng").strip()
    bgm_name = data.get("bgm_name", "")
    bgm_volume = float(data.get("bgm_volume", 12)) / 100.0
    voice_name = data.get("voice_name", "vi-VN-HoaiMyNeural")
    voice_rate = data.get("voice_rate", "-10%")
    
    options = {
        "aspect_ratio": data.get("aspect_ratio", "16:9"),
        "font_size": int(data.get("font_size", 42)),
        "font_color": data.get("font_color", "#FFFFFF"),
        "bg_opacity": int(data.get("bg_opacity", 150))
    }
    
    if not script_text:
        return jsonify({"success": False, "message": "Chưa nhập kịch bản."})
    if not api_key:
        return jsonify({"success": False, "message": "Yêu cầu phải có API Key Gemini."})
        
    task_id = create_task()
    
    thread = threading.Thread(
        target=generate_ai_video_bg,
        args=(task_id, api_key, script_text, style_prompt, bgm_name, bgm_volume, voice_name, voice_rate, options)
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({"success": True, "task_id": task_id})

if __name__ == "__main__":
    print("[*] Server is running at http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
