import hashlib
import json
import os
import re
import shutil
import subprocess


EVENT_RE = re.compile(
    r"\[EVENT\s*=\s*(\d{1,3}:\d{2}(?::\d{2})?)\s*-\s*"
    r"(\d{1,3}:\d{2}(?::\d{2})?)\s*\]",
    re.IGNORECASE,
)
SHOT_RE = re.compile(r"\[SHOT\s*=\s*(S\d{5})\s*\]", re.IGNORECASE)


def _to_seconds(value):
    parts = str(value).split(":")
    if len(parts) == 2:
        minutes, seconds = int(parts[0]), float(parts[1])
        if seconds >= 60:
            raise ValueError(f"Moc thoi gian khong hop le: {value}")
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
        if minutes >= 60 or seconds >= 60:
            raise ValueError(f"Moc thoi gian khong hop le: {value}")
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"Moc thoi gian khong hop le: {value}")


def _format_time(value):
    total = max(0.0, float(value))
    minutes = int(total // 60)
    seconds = total - minutes * 60
    return f"{minutes:02d}:{seconds:05.2f}"


def parse_story_event_ranges(story_map, video_duration):
    duration = max(0.0, float(video_duration))
    ranges = []
    for match in EVENT_RE.finditer(story_map or ""):
        try:
            start = float(_to_seconds(match.group(1)))
            end = float(_to_seconds(match.group(2)))
        except ValueError:
            continue
        if 0 <= start < end <= duration + 0.05:
            ranges.append((start, min(end, duration)))
    return ranges


def _scene_cache_path(source_path, cache_root):
    stat = os.stat(source_path)
    identity = f"{os.path.abspath(source_path)}|{stat.st_size}|{stat.st_mtime_ns}|shot-v1"
    key = hashlib.sha1(identity.encode("utf-8", errors="ignore")).hexdigest()[:20]
    folder = os.path.join(cache_root, key)
    return folder, os.path.join(folder, "locked_shots.json")


def detect_locked_shots(source_path, analysis_video_path, cache_root):
    """Detect real source cuts locally and cache them by source identity."""
    cache_dir, cache_path = _scene_cache_path(source_path, cache_root)
    try:
        with open(cache_path, "r", encoding="utf-8") as handle:
            cached = json.load(handle)
        if isinstance(cached, list) and cached:
            return cached
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    try:
        from scenedetect import ContentDetector, SceneManager, open_video

        video = open_video(analysis_video_path)
        manager = SceneManager()
        manager.auto_downscale = False
        manager.downscale = 2
        min_scene_len = max(6, int(round(float(video.frame_rate))))
        manager.add_detector(ContentDetector(threshold=27.0, min_scene_len=min_scene_len))
        manager.detect_scenes(video=video, frame_skip=1, show_progress=False)
        scene_list = manager.get_scene_list()
        shots = []
        for index, (start, end) in enumerate(scene_list):
            start_sec = float(start.get_seconds())
            end_sec = float(end.get_seconds())
            if end_sec - start_sec < 0.20:
                continue
            shots.append({
                "id": f"S{index + 1:05d}",
                "start": start_sec,
                "end": end_sec,
                "mid": (start_sec + end_sec) / 2.0,
            })
    except Exception as exc:
        print(f"[!] Khong phat hien duoc shot: {exc}", flush=True)
        shots = []

    if not shots:
        raise RuntimeError("Khong phat hien duoc ranh gioi shot trong video nguon.")
    os.makedirs(cache_dir, exist_ok=True)
    temp_path = cache_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(shots, handle, ensure_ascii=False)
    os.replace(temp_path, cache_path)
    return shots


def _even_sample(values, limit):
    if len(values) <= limit:
        return list(values)
    if limit <= 1:
        return [values[len(values) // 2]]
    positions = [round(i * (len(values) - 1) / (limit - 1)) for i in range(limit)]
    return [values[position] for position in positions]


def choose_candidate_shots(
    shots,
    event_ranges,
    video_duration,
    padding=5.0,
    max_per_event=12,
    max_total=900,
):
    """Keep chronological real shots near pass-1 events without semantic search."""
    if not shots:
        return []
    if not event_ranges:
        return _even_sample(list(range(len(shots))), min(max_total, len(shots)))

    chosen = set()
    duration = max(0.0, float(video_duration))
    for event_start, event_end in event_ranges:
        low = max(0.0, float(event_start) - float(padding))
        high = min(duration, float(event_end) + float(padding))
        overlapping = [
            index for index, shot in enumerate(shots)
            if float(shot["end"]) > low and float(shot["start"]) < high
        ]
        chosen.update(_even_sample(overlapping, max_per_event))
    ordered = sorted(chosen)
    return _even_sample(ordered, max_total)


def _nearest_sample_path(sample_paths, timestamp):
    if not sample_paths:
        raise RuntimeError("Khong co frame mau de tao storyboard.")
    # FFmpeg names the first emitted sample as frame_000001.jpg.
    index = max(0, min(len(sample_paths) - 1, int(round(float(timestamp)))))
    return sample_paths[index]


def build_storyboard_reel(
    analysis_video_path,
    shots,
    candidate_indices,
    output_dir,
    ffmpeg_exe,
    update_fn=None,
):
    """Create a compact 1-panel-per-shot evidence reel with immutable SHOT_IDs."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    if not candidate_indices:
        raise RuntimeError("Khong co shot ung vien de tao storyboard.")
    frame_dir = os.path.join(output_dir, "sampled_frames")
    panel_dir = os.path.join(output_dir, "storyboard_panels")
    os.makedirs(frame_dir, exist_ok=True)
    os.makedirs(panel_dir, exist_ok=True)
    if update_fn:
        update_fn(76, "Dang lay khung hinh dai dien cho storyboard...")

    frame_pattern = os.path.join(frame_dir, "frame_%06d.jpg")
    sample_fps = 2.0
    extract = subprocess.run([
        ffmpeg_exe, "-y", "-i", analysis_video_path,
        "-vf", f"fps={sample_fps:g},scale=320:-2", "-q:v", "6", frame_pattern,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace", timeout=1800)
    if extract.returncode != 0:
        raise RuntimeError(f"Khong trich duoc frame storyboard: {(extract.stderr or '')[-500:]}")
    sample_paths = sorted(
        os.path.join(frame_dir, name) for name in os.listdir(frame_dir)
        if name.lower().endswith(".jpg")
    )
    if not sample_paths:
        raise RuntimeError("FFmpeg khong tao duoc frame storyboard.")

    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 24)
        small_font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
        small_font = ImageFont.load_default()

    selected = []
    for panel_number, scene_index in enumerate(candidate_indices, start=1):
        shot = dict(shots[scene_index])
        shot_duration = max(0.2, float(shot["end"]) - float(shot["start"]))
        sample_times = (
            float(shot["start"]) + min(0.5, shot_duration * 0.20),
            float(shot["mid"]),
            float(shot["end"]) - min(0.5, shot_duration * 0.20),
        )
        images = []
        for timestamp in sample_times:
            with Image.open(_nearest_sample_path(sample_paths, timestamp * sample_fps)) as source:
                images.append(ImageOps.fit(source.convert("RGB"), (320, 180)))
        panel = Image.new("RGB", (960, 226), "black")
        for image_index, image in enumerate(images):
            panel.paste(image, (image_index * 320, 46))
        draw = ImageDraw.Draw(panel)
        draw.rectangle((0, 0, 960, 46), fill=(10, 10, 10))
        draw.text((12, 8), shot["id"], font=font, fill=(255, 220, 40))
        draw.text(
            (180, 12),
            f"SOURCE {_format_time(shot['start'])}-{_format_time(shot['end'])} | BEFORE / MID / AFTER",
            font=small_font,
            fill="white",
        )
        panel_path = os.path.join(panel_dir, f"panel_{panel_number:05d}.jpg")
        panel.save(panel_path, quality=86, optimize=True)
        selected.append(shot)
        if update_fn and panel_number % 100 == 0:
            update_fn(77, f"Da tao {panel_number}/{len(candidate_indices)} shot storyboard...")

    shutil.rmtree(frame_dir, ignore_errors=True)
    reel_path = os.path.join(output_dir, "locked_shot_storyboard.mp4")
    encode = subprocess.run([
        ffmpeg_exe, "-y", "-framerate", "1", "-i",
        os.path.join(panel_dir, "panel_%05d.jpg"),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
        "-pix_fmt", "yuv420p", "-r", "1", reel_path,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace", timeout=1800)
    shutil.rmtree(panel_dir, ignore_errors=True)
    if encode.returncode != 0 or not os.path.isfile(reel_path):
        raise RuntimeError(f"Khong tao duoc storyboard reel: {(encode.stderr or '')[-500:]}")
    return reel_path, selected


def shot_script_to_timeline(script_text, allowed_shots, minimum_shots=1):
    """Resolve Gemini SHOT markers without allowing timestamps or reordering."""
    shot_by_id = {str(shot["id"]).upper(): shot for shot in allowed_shots}
    matches = list(SHOT_RE.finditer(script_text or ""))
    if len(matches) < int(minimum_shots):
        raise ValueError(
            f"Gemini chi chon {len(matches)} SHOT; can toi thieu {int(minimum_shots)} SHOT."
        )
    prefix = (script_text or "")[:matches[0].start()].strip(" \r\n*-#")
    if prefix:
        raise ValueError("Kich ban co noi dung khong gan SHOT_ID o dau van ban.")

    rows = []
    used = set()
    previous_start = -1.0
    for index, match in enumerate(matches):
        shot_id = match.group(1).upper()
        shot = shot_by_id.get(shot_id)
        if shot is None:
            raise ValueError(f"Gemini tham chieu SHOT_ID khong ton tai: {shot_id}.")
        if shot_id in used or float(shot["start"]) <= previous_start:
            raise ValueError(f"SHOT_ID bi lap hoac di lui: {shot_id}.")
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(script_text)
        body = re.sub(r"\s+", " ", script_text[match.end():body_end]).strip(" -*\t\r\n")
        if not body:
            raise ValueError(f"{shot_id} khong co loi ke.")
        rows.append(
            f"[TIME={_format_time(shot['start'])}-{_format_time(shot['end'])}] {body}"
        )
        used.add(shot_id)
        previous_start = float(shot["start"])
    return "\n".join(rows)


def parse_shot_selection(selection_text, allowed_shots, minimum_shots=1):
    """Parse pass-2 selection rows for optional three-pass narration."""
    timeline = shot_script_to_timeline(selection_text, allowed_shots, minimum_shots)
    matches = list(SHOT_RE.finditer(selection_text or ""))
    shot_by_id = {str(shot["id"]).upper(): shot for shot in allowed_shots}
    selected = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(selection_text)
        description = re.sub(r"\s+", " ", selection_text[match.end():end]).strip(" -*\t\r\n")
        item = dict(shot_by_id[match.group(1).upper()])
        item["description"] = description
        selected.append(item)
    return selected, timeline


def validate_timeline_ranges(text, video_duration):
    """Reject malformed ranges before legacy parsing can repair or interpolate."""
    marker_re = re.compile(
        r"\[(TIME|BEAT)\s*=\s*(\d{1,3}:\d{2}(?::\d{2})?(?:\.\d{1,3})?)\s*-\s*"
        r"(\d{1,3}:\d{2}(?::\d{2})?(?:\.\d{1,3})?)\s*\]",
        re.IGNORECASE,
    )
    matches = list(marker_re.finditer(text or ""))
    if not matches:
        raise ValueError("Kich ban khong co TIME range hop le.")
    duration = max(0.0, float(video_duration))
    previous_end = -1.0
    for index, match in enumerate(matches, start=1):
        kind = match.group(1).upper()
        start = float(_to_seconds(match.group(2)))
        end = float(_to_seconds(match.group(3)))
        if end <= start:
            raise ValueError(f"Dong {index} co TIME_END khong lon hon TIME_START.")
        if end > duration + 0.05:
            raise ValueError(
                f"Dong {index} vuot thoi luong video: {_format_time(end)} > "
                f"{_format_time(duration)}."
            )
        if kind == "TIME" and start < previous_end - 0.05:
            raise ValueError(f"Dong {index} bi di lui hoac chong len khoang truoc.")
        if kind == "TIME":
            previous_end = end
    return True
