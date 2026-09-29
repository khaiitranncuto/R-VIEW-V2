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
from flask import Flask, request, jsonify, render_template, send_from_directory
from google import genai
import asyncio
import edge_tts
from tts_engine import (
    extract_time_range,
    parse_sentences_and_visual_beats,
    parse_sentences_and_time_ranges,
    remove_non_story_timeline_rows,
    split_text_into_sentences,
)
from video_renderer import render_review_video_ffmpeg, export_capcut_package, generate_all_images, render_ai_image_video_ffmpeg
from task_store import (
    load_task_state,
    save_task_payload,
    save_task_state,
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

# Khóa bảo mật mặc định
DEFAULT_API_KEY = ""

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

def _hard_limit_timestamp_script(script_text, max_words):
    """Last-resort duration guard which preserves coverage through the ending."""
    words = script_text.split()
    if len(words) <= max_words:
        return script_text
    blocks = [part.strip() for part in re.split(r'(?=\[TIME=[^\]]+\])', script_text) if part.strip()]
    if len(blocks) < 2:
        head_count = int(max_words * 0.7)
        return " ".join(words[:head_count] + words[-(max_words - head_count):])

    block_words = [block.split() for block in blocks]
    total = sum(len(items) for items in block_words)
    budgets = [max(6, int(max_words * len(items) / max(total, 1))) for items in block_words]
    while sum(budgets) > max_words:
        largest = max(range(len(budgets)), key=lambda i: budgets[i])
        if budgets[largest] <= 6:
            break
        budgets[largest] -= 1
    while sum(budgets) < max_words:
        largest = max(range(len(budgets)), key=lambda i: len(block_words[i]) - budgets[i])
        budgets[largest] += 1

    compacted = []
    for index, (items, budget) in enumerate(zip(block_words, budgets)):
        if len(items) <= budget:
            compacted.extend(items)
        elif index == len(block_words) - 1:
            # Keep the timestamp plus the actual conclusion at the end.
            compacted.append(items[0])
            compacted.extend(items[-max(0, budget - 1):])
        else:
            compacted.extend(items[:budget])
    return " ".join(compacted[:max_words])


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
def analyze_video_bg(task_id, api_key, model_name, video_filename, style, custom_prompt, enable_multi_voice=False, review_minutes="auto"):
    def update_prog(prog, msg):
        with task_lock:
            TASKS[task_id]["progress"] = prog
            TASKS[task_id]["message"] = msg

    try:
        video_path = os.path.join(WORKSPACE_DIR, video_filename)
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Khong tim thay video: {video_filename}")

        update_prog(10, "Dang cau hinh ket noi API Gemini...")
        client = genai.Client(api_key=api_key)

        # === Nen video truoc khi upload ===
        update_prog(15, "Dang nen video de upload nhanh hon...")
        from video_renderer import get_ffmpeg_exe, get_video_info
        ffmpeg_exe = get_ffmpeg_exe()
        
        compressed_path = video_path.rsplit('.', 1)[0] + "_gemini_temp.mp4"
        try:
            orig_dur, orig_w, orig_h = get_video_info(video_path)
            orig_size_mb = os.path.getsize(video_path) / (1024 * 1024)
            need_compress = orig_size_mb > 50 or orig_h > 720
            
            if need_compress:
                update_prog(12, f"Dang nen video ({orig_size_mb:.0f}MB -> nho hon)... Co the mat vai phut.")
                
                # Tinh CRF phu hop theo do dai video de dam bao < 2GB sau nen
                # Target: ~500MB cho video 1h, ~200MB cho video 30p
                if orig_dur > 1800:  # > 30 phut
                    crf_val = "32"
                    audio_br = "48k"
                    target_fps = "15"
                    scale_h = 480
                elif orig_dur > 600:  # > 10 phut
                    crf_val = "30"
                    audio_br = "56k"
                    target_fps = "20"
                    scale_h = 540
                else:
                    crf_val = "28"
                    audio_br = "64k"
                    target_fps = "24"
                    scale_h = 720
                
                scale_filter = f"scale=-2:{scale_h}" if orig_h > scale_h else ""
                vf_parts = [f for f in [scale_filter] if f]
                vf_arg = ["-vf", ",".join(vf_parts)] if vf_parts else []
                
                cmd_compress = [
                    ffmpeg_exe, "-y",
                    "-i", video_path,
                    *vf_arg,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", crf_val,
                    "-c:a", "aac", "-b:a", audio_br,
                    "-r", target_fps,
                    compressed_path
                ]
                res = subprocess.run(cmd_compress, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     timeout=1800)  # Timeout 30 phut cho nen
                
                if res.returncode == 0 and os.path.exists(compressed_path):
                    compressed_mb = os.path.getsize(compressed_path) / (1024 * 1024)
                    update_prog(18, f"Da nen: {orig_size_mb:.0f}MB -> {compressed_mb:.0f}MB")
                    upload_path = compressed_path
                else:
                    upload_path = video_path
            else:
                upload_path = video_path
        except subprocess.TimeoutExpired:
            update_prog(18, "Nen video qua lau, dung file goc...")
            upload_path = video_path
        except Exception:
            upload_path = video_path

        update_prog(20, "Dang tai video len Gemini...")
        import time
        
        # Cau hinh timeout theo dung luong file
        upload_size_mb = os.path.getsize(upload_path) / (1024 * 1024)
        upload_timeout = max(120, int(upload_size_mb * 3))  # ~3s/MB, toi thieu 2 phut
        
        from google.genai import types as genai_types
        http_opts = genai_types.HttpOptions(timeout=upload_timeout * 1000)  # ms
        client_upload = genai.Client(api_key=api_key, http_options=http_opts)
        
        update_prog(20, f"Dang tai video ({upload_size_mb:.0f}MB) len Gemini...")
        
        # Upload voi retry cho 429 + connection errors
        video_file = None
        last_upload_error = None
        for upload_attempt in range(5):
            try:
                video_file = client_upload.files.upload(file=upload_path)
                break
            except Exception as upload_err:
                last_upload_error = upload_err
                err_str = str(upload_err)
                print(f"[!] Upload lan {upload_attempt+1}/5 that bai: {err_str[:200]}")
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                    wait_sec = 30 * (upload_attempt + 1)
                    update_prog(20, f"Het quota tam thoi, cho {wait_sec}s roi thu lai...")
                    time.sleep(wait_sec)
                elif any(k in err_str for k in ["10053", "ReadError", "ConnectionError", "ConnectionReset"]) or "aborted" in err_str.lower() or "timeout" in err_str.lower():
                    wait_sec = 10 * (upload_attempt + 1)
                    update_prog(20, f"Ket noi bi ngat, thu lai lan {upload_attempt+2}/5... ({wait_sec}s)")
                    time.sleep(wait_sec)
                else:
                    raise
        if video_file is None:
            err_detail = str(last_upload_error)[:200] if last_upload_error else "Unknown"
            if "429" in err_detail or "quota" in err_detail.lower():
                raise Exception("Da het quota API mien phi! Quota tu reset sau 24h.")
            else:
                raise Exception(f"Upload that bai sau 5 lan. Loi: {err_detail}")
        
        # Xoa file nen tam
        if upload_path != video_path and os.path.exists(compressed_path):
            try:
                os.remove(compressed_path)
            except Exception:
                pass

        # Doi file ACTIVE - voi retry cho files.get 404
        update_prog(40, "Dang cho Gemini xu ly video...")
        max_wait = 900  # 15 phut cho video dai
        waited = 0
        file_ready = False
        while waited < max_wait:
            try:
                file_info = client.files.get(name=video_file.name)
                state_name = file_info.state.name if hasattr(file_info.state, 'name') else str(file_info.state)
                if state_name == "ACTIVE":
                    file_ready = True
                    break
                elif state_name == "FAILED":
                    raise Exception("Gemini khong the xu ly video.")
            except Exception as poll_err:
                if "404" in str(poll_err) or "Not Found" in str(poll_err):
                    if waited >= 30:
                        file_ready = True  # Skip polling, try generate directly
                        break
                elif "xu ly video" in str(poll_err):
                    raise
            time.sleep(5)
            waited += 5
            pct = 40 + min(waited // 15, 25)
            update_prog(pct, f"Dang xu ly video... ({waited}s/{max_wait}s)")
        
        if not file_ready and waited >= max_wait:
            pass  # Try generate anyway

        update_prog(70, "Video da san sang! Dang tao kich ban...")
        
        # TINH TOAN DO DAI KICH BAN — Tang do dai de khong bo sot chi tiet
        from video_renderer import get_video_info
        video_duration_sec, _, _ = get_video_info(video_path)
        video_duration_min = max(1, video_duration_sec / 60)
        
        if review_minutes == "auto":
            recap_ratio = 0.30 if video_duration_min > 30 else (0.40 if video_duration_min > 10 else 0.50)
            target_review_min = max(1.5, video_duration_min * recap_ratio)
        else:
            target_review_min = min(float(review_minutes), video_duration_min)

        # Vietnamese Edge TTS averages roughly 150 words/minute at normal rates.
        target_word_count = max(250, int(target_review_min * 150))
        min_word_count = max(200, int(target_word_count * 0.8))
        # Video dai can nhieu tu hon de bao phu toan bo cau chuyen
        max_word_count = int(target_word_count * 1.12)
        
        if style == "shorts":
            target_word_count = max(150, int(target_review_min * 130))
            min_word_count = max(100, int(target_word_count * 0.7))
            max_word_count = int(target_word_count * 1.12)
            
        style_choice = str(style).lower()
        if style_choice == "humorous" or "hài hước" in style_choice or "giải trí" in style_choice:
            pacing_instruction = (
                "=== ĐỊNH HƯỚNG TẤU HÀI (BẮT BUỘC) ===\n"
                "- Văn phong: Vui nhộn, bắt trend, nhịp nhanh, đầy năng lượng.\n"
                "- Sau mỗi pha hài, dùng câu bình luận mỉa mai hoặc cà khịa ngay lập tức. Giọng đọc LIÊN TỤC, không ngắt quãng.\n"
            )
        elif style_choice == "action" or "hành động" in style_choice or "thử thách" in style_choice or "100 ngày" in style_choice:
            pacing_instruction = (
                "=== ĐỊNH HƯỚNG HÀNH ĐỘNG & SINH TỒN (BẮT BUỘC) ===\n"
                "- Câu CỰC NGẮN (8-15 từ), dứt khoát, động từ mạnh.\n"
                "- Giọng đọc LIÊN TỤC không nghỉ, tạo cảm giác nghẹt thở. Dùng dấu phẩy và dấu chấm để tạo nhịp thay vì khoảng lặng.\n"
            )
        elif style_choice == "news" or "tin tức" in style_choice or "bản tin" in style_choice or "cập nhật" in style_choice:
            pacing_instruction = (
                "=== ĐỊNH HƯỚNG BẢN TIN (BẮT BUỘC) ===\n"
                "- Nhịp đọc đĩnh đạc, rõ ràng, thông tin cao. KHÔNG cường điệu.\n"
                "- Câu khúc chiết 12-18 từ, thuật ngữ chuẩn. Giọng đọc LIÊN TỤC mượt mà.\n"
            )
        elif style_choice == "tutorial" or "hướng dẫn" in style_choice or "review" in style_choice or "đánh giá" in style_choice or "cách làm" in style_choice:
            pacing_instruction = (
                "=== ĐỊNH HƯỚNG HƯỚNG DẪN (BẮT BUỘC) ===\n"
                "- Trình bày theo trình tự logic, mạch lạc.\n"
                "- Văn phong sư phạm, tận tình, dễ hiểu. Giọng đọc LIÊN TỤC, dùng dấu chấm câu tạo nhịp.\n"
            )
        else: # Mặc định cho dramatic / Kịch tính / Kinh dị
            pacing_instruction = (
                "=== NHỊP ĐIỆU KỂ CHUYỆN (BẮT BUỘC) ===\n"
                "- Giọng đọc LIÊN TỤC như YouTuber recap chuyên nghiệp. KHÔNG có khoảng lặng dài.\n"
                "- Tạo kịch tính bằng TỪ NGỮ và CÂU VĂN, không phải bằng im lặng.\n"
                "- Dùng dấu ba chấm '...' khi muốn tạo hồi hộp, dùng dấu chấm than '!' khi muốn nhấn mạnh.\n"
                "- TUYỆT ĐỐI KHÔNG chèn thẻ <BREAK>, <break>, hay bất kỳ thẻ ngắt nghỉ nào. Chỉ viết văn bản thuần túy.\n"
            )
        multi_voice_instruction = ""
        if enable_multi_voice:
            multi_voice_instruction = (
                "=== ĐỊNH HƯỚNG ĐA GIỌNG ĐỌC (MULTI-VOICE) (BẮT BUỘC) ===\n"
                "Kịch bản này phải được viết dưới dạng một cuộc ĐỐI THOẠI HẤP DẪN giữa 2 người (1 Nam, 1 Nữ) cùng nhau bình luận/kể lại bộ phim.\n"
                "Hãy sử dụng chính xác thẻ lệnh đặt ở đầu mỗi câu nói để phân vai:\n"
                "- Dùng thẻ [VOICE=vi-VN-NamMinhNeural] cho giọng Nam.\n"
                "- Dùng thẻ [VOICE=vi-VN-HoaiMyNeural] cho giọng Nữ.\n"
                "Đan xen 2 giọng nhịp nhàng. Ví dụ:\n"
                "[VOICE=vi-VN-NamMinhNeural] Thật không thể tin nổi, anh ta đã nhảy xuống vách đá!\n"
                "[VOICE=vi-VN-HoaiMyNeural] Đúng vậy, nhưng hãy nhìn kỹ xem phía dưới có gì kìa...\n\n"
            )
            
        custom_section = f"\n=== YÊU CẦU BỔ SUNG ===\n{custom_prompt}\n" if custom_prompt.strip() else ""
        
        # Tinh ngan sach tu/phut
        words_per_min = int(target_word_count / video_duration_min)
        
        prompt = (
            f"=== NHIỆM VỤ ===\n"
            f"Bạn là YouTuber recap chuyên nghiệp. Video gốc {video_duration_min:.0f} phút.\n"
            f"Viết kịch bản khoảng {min_word_count}-{max_word_count} từ kể trọn phần CÂU CHUYỆN thực tế.\n"
            f"Mục tiêu: Người xem HIỂU TRỌN VẸN câu chuyện mà KHÔNG CẦN xem video gốc.\n"
            f"Viết LÔI CUỐN, HẤP DẪN — không cần kể chi tiết từng cảnh, chỉ cần kể ĐÚNG và HAY.\n\n"
            
            f"=== NGÂN SÁCH TỪ (CỰC KỲ QUAN TRỌNG) ===\n"
            f"Ngân sách tối đa khoảng {target_word_count} từ cho phần CÂU CHUYỆN.\n"
            f"Phân bổ lời kể theo các sự kiện, không phân bổ theo độ dài toàn file.\n"
            f"Logo và credit không được nhận bất kỳ ngân sách từ nào.\n\n"
            
            f"=== CÁCH VIẾT ===\n"
            f"1. XEM toàn bộ video để xác định chính xác lúc CÂU CHUYỆN bắt đầu và kết thúc.\n"
            f"2. Chọn ra các SỰ KIỆN CHÍNH (bước ngoặt, nguy hiểm, thành tựu, kết quả).\n"
            f"3. Kể lại TUẦN TỰ theo thời gian, BỎ QUA những phần nhàm chán/lặp lại.\n"
            f"4. Tập trung vào DIỄN BIẾN và CẢM XÚC, không liệt kê chi tiết.\n"
            f"5. Kể đến KẾT THÚC CÂU CHUYỆN, rồi DỪNG ngay tại đó.\n"
            f"6. BỎ HOÀN TOÀN logo, tiêu đề mở đầu, màn hình đen/trắng, credit, tên đạo diễn, diễn viên, đoàn làm phim, hãng sản xuất và website.\n"
            f"7. KHÔNG viết lời mô tả cho phần credit dù nó nằm ở cuối file video.\n\n"
            
            f"=== CẤU TRÚC ===\n"
            f"Vào thẳng SỰ KIỆN ĐẦU TIÊN của câu chuyện tại timestamp thật của nó. KHÔNG bắt buộc bắt đầu từ 00:00.\n"
            f"Kể chuyện LIÊN TỤC như đang nói cho bạn bè nghe — tự nhiên, cuốn hút.\n\n"
            
            f"{multi_voice_instruction}"
            
            f"=== ĐỘ CHÍNH XÁC (TỐI QUAN TRỌNG) ===\n"
            f"Mỗi câu văn sẽ được GHÉP VỚI KHUNG HÌNH VIDEO tại đúng mốc timestamp đó.\n"
            f"Nếu câu văn nói về 'chiến đấu với zombie' nhưng khung hình cho thấy 'đang xây nhà' → SAI!\n\n"
            f"QUY TẮC VÀNG:\n"
            f"- Mỗi câu PHẢI mô tả đúng những gì ĐANG XẢY RA TẠI TIMESTAMP ĐÓ.\n"
            f"- KHÔNG được bịa: KHÔNG thêm đồ vật, hành động không có trong video.\n"
            f"- Nếu không rõ → MIÊU TẢ CHUNG ('một cấu trúc', 'một sinh vật'), KHÔNG đoán bừa.\n"
            f"- SAI: 'Xây một cái nhà' khi thực tế đang đào hầm.\n"
            f"- ĐÚNG: Chỉ viết những gì bạn NHÌN THẤY RÕ RÀNG tại mốc thời gian đó.\n\n"
            
            f"=== NHẬN DẠNG MINECRAFT (THAM KHẢO) ===\n"
            f"Nếu video là Minecraft, hãy gọi đúng tên các entity phổ biến:\n"
            f"- Sinh vật mũi dài, tay chắp, mặc áo nâu/trắng/tím = DÂN LÀNG (Villager), KHÔNG phải 'thực thể kỳ lạ'\n"
            f"- Sinh vật cao, đen, mắt tím, dịch chuyển = ENDERMAN\n"
            f"- Sinh vật xanh lá, 4 chân, phát nổ = CREEPER\n"
            f"- Sinh vật xương trắng, cầm cung = SKELETON\n"
            f"- Sinh vật xanh, thối rữa, chậm chạp = ZOMBIE\n"
            f"- Sinh vật khổng lồ, sắt, bảo vệ làng = IRON GOLEM\n"
            f"- Sinh vật khổng lồ, tối, ở Deep Dark, gầm rú = WARDEN\n"
            f"- Sinh vật bay, trắng, bắn lửa = GHAST\n"
            f"- Rồng đen, ở The End = ENDER DRAGON\n"
            f"- Sinh vật 3 đầu, bay, bắn sọ = WITHER\n"
            f"- Nếu là mod/custom thì miêu tả hình dáng: 'sinh vật mang mặt nạ', 'quái vật biến dạng'\n\n"
            
            f"=== TIMELINE CÂU CHUYỆN ===\n"
            f"Video nguồn dài {video_duration_min:.0f} phút nhưng có thể chứa logo và credit.\n"
            f"- Chỉ phân bổ lời kể trong khoảng có diễn biến câu chuyện.\n"
            f"- Timestamp đầu là sự kiện đầu tiên; timestamp cuối là kết quả cuối cùng của câu chuyện.\n"
            f"- Không kéo timeline tới cuối file nếu sau câu chuyện chỉ còn credit hoặc màn hình kết.\n\n"
            
            f"=== TIMESTAMP (CHÍNH XÁC) ===\n"
            f"- Giữ mỗi câu kể tự nhiên trong MỘT dòng để TTS đọc liền mạch.\n"
            f"- Cảnh đầu câu dùng [TIME=START-END]. Nếu cùng câu xuất hiện hành động mới, chèn [BEAT=START-END] ngay trước cụm từ mô tả hành động đó.\n"
            f"- Ví dụ: [TIME=01:20-01:24] Luke nhặt mảnh xương lên, [BEAT=01:27-01:31] rồi phát hiện dấu chân gấu trên cát.\n"
            f"- [BEAT] chỉ điều khiển hình ảnh, không được làm cắt vụn câu văn hoặc lặp lại lời kể.\n"
            f"- MỖI CỤM HÀNH ĐỘNG phải có [TIME=mm:ss-mm:ss] hoặc [BEAT=mm:ss-mm:ss]: vùng hình ảnh chứng minh đúng hành động đang kể.\n"
            f"- Mỗi [TIME=] bắt đầu MỘT câu kể mới; [BEAT=] nằm trong câu đó tại đúng chỗ lời kể chuyển sang hành động mới.\n"
            f"- Một cụm được phép có 1-3 câu tự nhiên nếu chúng cùng nhân vật, cùng sự kiện và diễn ra liên tiếp.\n"
            f"- Một range chỉ nên chứa MỘT hành động hình ảnh chính. Nếu lời kể có hai hành động liên tiếp, phải tách thành hai range START-END liên tiếp.\n"
            f"- Không chia quá 2 range cho một câu và không tạo range ngắn hơn 1,5 giây, trừ montage hành động nhanh.\n"
            f"- Không tách một ý đang liền mạch thành nhiều câu cụt chỉ để ép vừa số giây.\n"
            f"- Không dùng chung một timestamp cho nhiều hành động khác nhau nếu chúng xảy ra ở thời điểm khác nhau.\n"
            f"- START phải nhỏ hơn END; range sau không được đi lùi hoặc chồng lên range trước.\n"
            f"- TIME_END là biên tham chiếu của hình ảnh, KHÔNG phải giới hạn cứng để cắt hoặc tăng tốc lời đọc.\n"
            f"- Viết đủ ý bằng câu hoàn chỉnh; hệ thống sẽ cho cảnh tiếp tục chạy nếu lời đọc dài hơn range.\n"
            f"- Range phải CHÍNH XÁC với lúc hành động diễn ra, KHÔNG đoán và dừng trước credit cuối phim.\n"
            
            f"=== CHẤT LƯỢNG VIẾT ===\n"
            f"- Văn bản thuần túy. KHÔNG markdown. KHÔNG thẻ BREAK.\n"
            f"- Câu 8-20 từ. Ngắn gọn, dứt khoát.\n"
            f"- Viết như đang KỂ CHUYỆN cho bạn bè: tự nhiên, cuốn hút, liền mạch.\n"
            f"- Dùng CHI TIẾT CỤ THỂ thay vì mô tả chung: 'rơi xuống hố dung nham sâu thẳm' thay vì 'gặp nguy hiểm'.\n"
            f"- Chuyển cảnh MƯỢT: 'Nhưng đó mới chỉ là khởi đầu...', 'Tưởng đã an toàn, nhưng...'\n"
            f"- Giọng đọc LIÊN TỤC. Dùng dấu ba chấm '...' tạo hồi hộp, dấu '!' nhấn mạnh.\n\n"
            
            f"{pacing_instruction}\n"
            
            f"=== CẤM ===\n"
            f"- CẤM in từ khóa cấu trúc (HOOK, BUILD-UP, PAYOFF).\n"
            f"- CẤM chèn thẻ <BREAK>, <break time>, hay bất kỳ thẻ ngắt nghỉ.\n"
            f"- CẤM bịa sự kiện không có trong video.\n"
            f"- CẤM gọi sai tên mob/vật phẩm.\n\n"
            
            f"NHẮC LẠI: Mỗi câu dùng [TIME=START-END] cho cảnh đầu và [BEAT=START-END] cho các hành động tiếp theo; lời kể vẫn phải đầy đủ, tự nhiên. Tuyệt đối không kể logo/credit.\n"
            f"{custom_section}"
            f"Bây giờ hãy viết kịch bản:"
        )

        # GOI GEMINI API - fallback models
        import time as _time
        try:
            available_models = [m.name.replace("models/", "") if hasattr(m, 'name') else str(m) for m in client.models.list() if "gemini" in (m.name.replace("models/", "") if hasattr(m, 'name') else str(m)).lower()]
        except Exception:
            available_models = ["gemini-3-flash-preview", "gemini-2.0-flash-lite", "gemini-3.1-flash-lite"]
            
        fallback_models = [model_name] + [m for m in available_models if ("flash" in m or "lite" in m) and "preview" not in m] + available_models
        seen = set()
        unique_models = [m for m in fallback_models if m not in seen and not seen.add(m)]
        
        response = None
        last_error = None
        used_model = model_name
        
        for mi, try_model in enumerate(unique_models):
            for attempt in range(3):  # Giam xuong 3 lan de tranh treo UI
                try:
                    update_prog(75 + mi * 3, f"Dang tao kich ban voi {try_model}... (lan {attempt+1})")
                    from google.genai import types as _gen_types
                    _gen_config = _gen_types.GenerateContentConfig(
                        max_output_tokens=65536,
                        temperature=0.4,
                    )
                    response = client.models.generate_content(
                        model=try_model,
                        contents=[video_file, prompt],
                        config=_gen_config,
                    )
                    if response and response.text:
                        used_model = try_model
                        break
                except Exception as api_err:
                    last_error = api_err
                    err_msg = str(api_err)
                    
                    if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower():
                        if "limit" in err_msg and "0" in err_msg:
                            print(f"[!] Model {try_model} co limit=0 (khong ho tro). Bo qua...")
                            break # Chuyen sang model khac
                            
                        wait_sec = 5 * (attempt + 1)  # Chi doi 5s, 10s, 15s
                        update_prog(75 + mi * 3, f"Het quota ({try_model}), doi {wait_sec}s...")
                        _time.sleep(wait_sec)
                    elif "503" in err_msg or "UNAVAILABLE" in err_msg or "overloaded" in err_msg.lower():
                        wait_sec = 5 * (attempt + 1)
                        update_prog(75 + mi * 3, f"Server ban ({try_model}), doi {wait_sec}s...")
                        _time.sleep(wait_sec)
                    else:
                        break  # Loi 404 hoac loi khac, thu model tiep theo luon
            
            if response and response.text:
                break
        
        if not response or not response.text:
            with open(os.path.join(WORKSPACE_DIR, "debug_api_error.log"), "w", encoding="utf-8") as f:
                f.write(f"Model: {model_name}\nLast Error:\n{str(last_error)}\n")
            raise Exception(f"Tat ca models that bai. Loi cuoi: {str(last_error)[:200]}")
        script_text = response.text
        word_count_raw = len(script_text.split())
        print(f"[+] Kich ban lan 1: {word_count_raw} tu", flush=True)
        
        # === AUTO-CONTINUE: Neu kich ban chua den cuoi video, goi API lan 2 ===
        import re as _re_check
        all_ts = _re_check.findall(r'\[TIME=(\d+):(\d+)(?:-(\d+):(\d+))?\]', script_text)
        last_ts_sec = 0
        if all_ts:
            start_mm, start_ss, end_mm, end_ss = all_ts[-1]
            last_ts_sec = (
                int(end_mm) * 60 + int(end_ss)
                if end_mm and end_ss else int(start_mm) * 60 + int(start_ss)
            )
        
        print(f"[+] Timestamps tim thay: {len(all_ts)}, cuoi cung: {last_ts_sec}s/{video_duration_sec:.0f}s ({last_ts_sec/video_duration_sec*100:.0f}%)", flush=True)
        
        # Do not auto-continue based on percentage of the source file. The tail
        # can be end credits; Gemini must stop at the actual story conclusion.
        print(f"[+] Khong ep kich ban ke qua logo/credit cuoi file (moc cuoi {last_ts_sec}s).", flush=True)
        
        # POST-PROCESSING
        import re as re_mod
        
        script_text = re_mod.sub(r'\*\*(.+?)\*\*', r'\1', script_text)
        script_text = re_mod.sub(r'\*(.+?)\*', r'\1', script_text)
        script_text = re_mod.sub(r'__(.+?)__', r'\1', script_text)
        script_text = re_mod.sub(r'_(.+?)_', r'\1', script_text)
        script_text = re_mod.sub(r'~~(.+?)~~', r'\1', script_text)
        script_text = re_mod.sub(r'^#{1,6}\s*', '', script_text, flags=re_mod.MULTILINE)
        script_text = re_mod.sub(r'^[\-\*\•]\s+', '', script_text, flags=re_mod.MULTILINE)
        script_text = re_mod.sub(r'^\d+\.\s+', '', script_text, flags=re_mod.MULTILINE)
        script_text = re_mod.sub(r'`([^`]+)`', r'\1', script_text)
        script_text = re_mod.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', script_text)
        script_text = re_mod.sub(r'\n{3,}', '\n\n', script_text)
        
        # === LOC SACH BREAK TAGS — Xoa toan bo the BREAK/break du AI co chen ===
        script_text = re_mod.sub(r'<\s*BREAK[^>]*>', '', script_text, flags=re_mod.IGNORECASE)
        script_text = re_mod.sub(r'\[\s*BREAK[^\]]*\]', '', script_text, flags=re_mod.IGNORECASE)
        script_text = re_mod.sub(r'<\s*break\s+time[^>]*/?\s*>', '', script_text, flags=re_mod.IGNORECASE)
        script_text = re_mod.sub(r'\s{2,}', ' ', script_text)
        
        script_text = script_text.strip()
        
        # Report the generated length, but never rewrite/truncate completed
        # narration after Gemini has established a coherent story.
        word_count = len(script_text.split())
        print(f"[+] Kich ban sau xu ly: {word_count} tu (target={target_word_count}, max={max_word_count})", flush=True)

        # The editor and parser share one stable representation: one timestamp
        # block per row. This also repairs Gemini responses returned as one blob.
        script_text = remove_non_story_timeline_rows(script_text)

        # Don dep file tren Gemini
        update_prog(95, "Dang don dep...")
        try:
            client.files.delete(name=video_file.name)
        except Exception:
            pass

        with task_lock:
            TASKS[task_id]["status"] = "success"
            TASKS[task_id]["progress"] = 100
            TASKS[task_id]["message"] = "Tao kich ban thanh cong!"
            TASKS[task_id]["result"] = script_text

    except Exception as e:
        import traceback
        traceback.print_exc()
        err_msg = str(e)
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower():
            user_msg = "Đã hết quota API miễn phí hôm nay! Quota sẽ tự động reset sau 24h. Vui lòng thử lại sau."
        elif "API_KEY_INVALID" in err_msg or "API key not valid" in err_msg:
            user_msg = "API Key không hợp lệ! Vui lòng kiểm tra lại key."
        else:
            user_msg = f"Lỗi: {err_msg[:300]}"
        with task_lock:
            TASKS[task_id]["status"] = "failed"
            TASKS[task_id]["message"] = user_msg

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
    if not api_key:
        return jsonify({"valid": False, "message": "API Key không được bỏ trống."})
        
    try:
        client = genai.Client(api_key=api_key)
        
        # Chi dung models.list() de xac thuc key (MIEN PHI, khong ton quota)
        models_list = list(client.models.list())
        available = []
        for m in models_list:
            name = m.name.replace("models/", "") if hasattr(m, 'name') else str(m)
            if "gemini" in name.lower():
                available.append(name)
        return jsonify({"valid": True, "message": "Kết nối API Gemini thành công!", "models": available})
    except Exception as e:
        err_msg = str(e)
        if "API_KEY_INVALID" in err_msg or "API key not valid" in err_msg:
            return jsonify({"valid": False, "message": "API Key không hợp lệ! Kiểm tra lại tại https://aistudio.google.com/apikey"})
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower():
            return jsonify({"valid": True, "message": "API Key hợp lệ! Nhưng đã hết quota miễn phí hôm nay. Quota sẽ reset sau 24h.", "models": ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.0-flash-lite"]})
        return jsonify({"valid": False, "message": f"Lỗi kết nối: {err_msg[:200]}"})

@app.route("/api/list-videos", methods=["GET"])
def list_videos():
    files = os.listdir(WORKSPACE_DIR)
    videos = [f for f in files if f.lower().endswith(('.mp4', '.mkv', '.avi', '.mov'))]
    return jsonify(videos)

@app.route("/api/debug-models", methods=["POST"])
def debug_models():
    data = request.json or {}
    ak = data.get("api_key", "").strip()
    if not ak:
        return jsonify({"error": "No API key"})
    try:
        client = genai.Client(api_key=ak)
        results = []
        for m in client.models.list():
            results.append(m.name if hasattr(m, 'name') else str(m))
        
        test_models = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.0-flash-lite"]
        working = []
        for tm in test_models:
            try:
                resp = client.models.generate_content(model=tm, contents=["Nói 'xin chào'."])
                if resp and resp.text:
                    working.append({"model": tm, "response": resp.text[:50]})
            except Exception as e:
                working.append({"model": tm, "error": str(e)[:100]})
        
        return jsonify({"all_models": results[:30], "test_results": working})
    except Exception as e:
        return jsonify({"error": str(e)[:300]})

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
        filename = file.filename
        ext = os.path.splitext(filename)[1].lower()
        
        # Kiem tra dinh dang file
        if file_type == "video":
            if ext not in ALLOWED_VIDEO_EXT:
                return jsonify({"success": False, "message": f"Dinh dang {ext} khong ho tro. Chi chap nhan: {', '.join(ALLOWED_VIDEO_EXT)}"})
            target_path = os.path.join(WORKSPACE_DIR, filename)
        else:
            if ext not in ALLOWED_AUDIO_EXT:
                return jsonify({"success": False, "message": f"Dinh dang {ext} khong ho tro. Chi chap nhan: {', '.join(ALLOWED_AUDIO_EXT)}"})
            target_path = os.path.join(WORKSPACE_DIR, "static", "bgm", filename)
            
        file.save(target_path)
        return jsonify({"success": True, "filename": filename})
        
    return jsonify({"success": False, "message": "Loi khong xac dinh khi luu file."})

@app.route("/api/analyze-video", methods=["POST"])
def analyze_video():
    data = request.json or {}
    api_key = data.get("api_key", "").strip() or DEFAULT_API_KEY
    model_name = data.get("model_name", "gemini-1.5-flash")
    video_filename = data.get("video_name", "")
    style = data.get("style", "dramatic")
    custom_prompt = data.get("custom_prompt", "").strip()
    enable_multi_voice = data.get("enable_multi_voice", False)
    project_id = str(data.get("project_id", "")).strip() or str(uuid.uuid4())
    review_minutes_raw = data.get("review_minutes", "auto")
    review_minutes = "auto" if str(review_minutes_raw).lower() == "auto" else max(3, min(20, int(review_minutes_raw)))
    
    if not video_filename:
        return jsonify({"success": False, "message": "Chưa chọn video gốc."})
        
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
        args=(task_id, api_key, model_name, video_filename, style, custom_prompt, enable_multi_voice, review_minutes)
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({"success": True, "task_id": task_id})

@app.route("/api/split-sentences", methods=["POST"])
def split_sentences():
    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify([])
        
    sentences = split_text_into_sentences(text)
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
