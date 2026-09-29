import os
import re
import subprocess
import shutil
import zipfile
import pathlib
import concurrent.futures
from tts_engine import (
    create_srt,
    expand_visual_beats,
    generate_all_tts,
    get_audio_duration,
    parse_sentences_and_visual_beats,
    parse_sentences_and_time_ranges,
    split_text_into_sentences,
)
def get_ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"
def get_video_info(input_video_path):
    """Lay thong tin video (duration, width, height) bang FFmpeg thay vi MoviePy."""
    ffmpeg_exe = get_ffmpeg_exe()
    result = subprocess.run(
        [ffmpeg_exe, "-i", input_video_path, "-hide_banner"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    stderr = result.stderr
    
    # Parse duration
    duration = 0.0
    dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", stderr)
    if dur_match:
        h, m, s, cs = dur_match.groups()
        duration = int(h)*3600 + int(m)*60 + int(s) + int(cs)/100.0
    
    # Parse resolution (tranh match sai voi SAR/DAR)
    w_vid, h_vid = 1920, 1080
    res_match = re.search(r"(\d{2,5})x(\d{2,5})[\s,\[]", stderr)
    if res_match:
        w_vid, h_vid = int(res_match.group(1)), int(res_match.group(2))
    
    return duration, w_vid, h_vid


def _remove_voice_control_rows(sentences, starts, ends):
    """Voice tags control TTS but must never occupy a visual timeline slot."""
    kept = [
        index for index, sentence in enumerate(sentences)
        if not sentence.strip().upper().startswith("<VOICE=")
    ]
    return (
        [sentences[index] for index in kept],
        [starts[index] for index in kept],
        [ends[index] for index in kept],
    )


def _require_exact_audio_alignment(sentences, audio_paths, durations):
    if not (len(sentences) == len(audio_paths) == len(durations)):
        raise RuntimeError(
            "So luong audio khong khop timeline "
            f"(canh={len(sentences)}, audio={len(audio_paths)}, duration={len(durations)})."
        )
    for index, (path, duration) in enumerate(zip(audio_paths, durations), start=1):
        if not path or not os.path.isfile(path) or float(duration) <= 0.05:
            raise RuntimeError(f"Audio cau {index} khong hop le; dung render de tranh lech canh.")


def _pause_duration_for_text(text):
    """Return a small, controlled pause without creating robotic one-second gaps."""
    text = (text or "").rstrip()
    if text.endswith(("...", "…")):
        return 0.28
    if text.endswith((".", "!", "?", ";")):
        return 0.22
    return 0.12


def build_visual_edit_plan(
    durations,
    starts,
    ends,
    video_duration,
    min_playback_speed=0.90,
    fallback_starts=None,
):
    """Build an audio-led edit decision list for explicit Gemini ranges.

    Every narration item owns an independent source anchor. A long previous
    sentence must never push the next item past its declared action. When a
    visual range is slightly shorter than its narration, use at most a subtle
    slowdown and then continue into neighbouring footage instead of cloning a
    still frame.
    """
    plan = []
    video_duration = max(0.0, float(video_duration))
    min_playback_speed = min(1.0, max(0.50, float(min_playback_speed)))

    for index, raw_duration in enumerate(durations):
        output_duration = max(0.05, float(raw_duration))
        raw_start = starts[index] if starts and index < len(starts) else None
        if raw_start is None and fallback_starts and index < len(fallback_starts):
            raw_start = fallback_starts[index]
        raw_end = ends[index] if ends and index < len(ends) else None
        has_range = raw_start is not None and raw_end is not None and float(raw_end) > float(raw_start)

        if has_range:
            range_start = min(max(float(raw_start), 0.0), video_duration)
            range_end = min(max(float(raw_end), range_start), video_duration)
            range_duration = range_end - range_start
            if output_duration > range_duration:
                # Keep the complete declared action and add only the minimum
                # neighbouring footage needed to avoid an obvious slow motion.
                source_duration = min(output_duration, max(range_duration, output_duration * min_playback_speed))
            else:
                source_duration = output_duration
            source_start = range_start
        else:
            source_start = min(max(float(raw_start or 0.0), 0.0), video_duration)
            source_duration = output_duration

        source_duration = min(source_duration, video_duration) if video_duration else 0.0
        # Close to the physical end there is no footage to extend into. Shift
        # backwards so the output still consists entirely of real frames.
        if video_duration and source_start + source_duration > video_duration:
            source_start = max(0.0, video_duration - source_duration)

        playback_speed = source_duration / output_duration if output_duration else 1.0
        plan.append({
            "index": index,
            "source_start": source_start,
            "source_duration": source_duration,
            "output_duration": output_duration,
            "playback_speed": playback_speed,
            "has_range": has_range,
        })

    # Do not let one long spoken beat consume frames owned by the next action;
    # otherwise the next segment starts earlier and visibly replays footage.
    for index in range(len(plan) - 1):
        next_start = starts[index + 1] if starts and index + 1 < len(starts) else None
        if next_start is None:
            continue
        available = float(next_start) - float(plan[index]["source_start"])
        if available > 0.05 and plan[index]["source_duration"] > available:
            plan[index]["source_duration"] = available
            plan[index]["playback_speed"] = available / plan[index]["output_duration"]
    return plan


def trim_silence_from_audio(input_path, output_path, pause_duration=0.15):
    """Preserve speech exactly; grouped TTS already owns its natural pauses."""
    ffmpeg_exe = get_ffmpeg_exe()
    # Amplitude-based trimming used to remove quiet Vietnamese final consonants.
    # Service padding is bounded from the final SubMaker cue during generation.
    result = subprocess.run([
        ffmpeg_exe, "-y", "-i", input_path,
        "-c:a", "pcm_s16le", "-ar", "24000", "-ac", "1",
        output_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) < 100:
        # Safe fallback: keep the speech even if this FFmpeg build lacks a filter.
        subprocess.run([
            ffmpeg_exe, "-y", "-i", input_path,
            "-c:a", "pcm_s16le", "-ar", "24000", "-ac", "1",
            output_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return get_audio_duration(output_path)

def create_master_audio(audio_paths, bgm_path, bgm_volume, output_path, temp_dir, sentences=None):
    ffmpeg_exe = get_ffmpeg_exe()
    
    # Kiem tra: Neu co marker _USE_MASTER_TTS_, dung truc tiep master file (lien mach)
    marker_path = os.path.join(temp_dir, "_USE_MASTER_TTS_")
    if os.path.exists(marker_path):
        with open(marker_path, 'r') as mf:
            master_tts_path = mf.read().strip()
        
        if os.path.exists(master_tts_path):
            print("[+] Su dung master TTS truc tiep (giong doc lien mach).")
            tts_master_wav = os.path.join(temp_dir, "master_tts.wav")
            # Convert MP3 to WAV cho chat luong tot nhat
            subprocess.run([
                ffmpeg_exe, "-y", "-i", master_tts_path, "-c:a", "pcm_s16le", tts_master_wav
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            
            if bgm_path and os.path.exists(bgm_path):
                ducking_filter = (
                    f"[0:a]asplit=2[voice_out][voice_ctrl];"
                    f"[1:a]volume={bgm_volume}[bgm_vol];"
                    f"[bgm_vol][voice_ctrl]sidechaincompress=threshold=0.08:ratio=5:attack=50:release=1000[bgm_ducked];"
                    f"[voice_out][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=2[aout]"
                )
                subprocess.run([
                    ffmpeg_exe, "-y",
                    "-i", tts_master_wav,
                    "-stream_loop", "-1", "-i", bgm_path,
                    "-filter_complex", ducking_filter,
                    "-map", "[aout]",
                    output_path
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            else:
                shutil.copy2(tts_master_wav, output_path)
            
            return None  # Khong can trimmed_durations vi da dung single-file
    
    # Fallback: Trim + Concat tung file (cach cu)
    trimmed_dir = os.path.join(temp_dir, "trimmed")
    os.makedirs(trimmed_dir, exist_ok=True)
    
    trimmed_paths = []
    trimmed_durations = []
    for i, ap in enumerate(audio_paths):
        trimmed_path = os.path.join(trimmed_dir, f"trimmed_{i:04d}.wav")
        sentence_text = sentences[i] if sentences and i < len(sentences) else ""
        pause_duration = _pause_duration_for_text(sentence_text)
        trimmed_duration = trim_silence_from_audio(ap, trimmed_path, pause_duration)
        trimmed_paths.append(trimmed_path)
        trimmed_durations.append(max(trimmed_duration, 0.3))
    
    concat_list_path = os.path.join(temp_dir, "concat_list.txt")
    with open(concat_list_path, 'w', encoding='utf-8') as f:
        for tp in trimmed_paths:
            safe_path = tp.replace("\\", "/")
            f.write(f"file '{safe_path}'\n")
            
    tts_master_path = os.path.join(temp_dir, "master_tts.wav")
    subprocess.run([
        ffmpeg_exe, "-y", "-f", "concat", "-safe", "0", 
        "-i", concat_list_path, "-c:a", "pcm_s16le", tts_master_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    
    if bgm_path and os.path.exists(bgm_path):
        ducking_filter = (
            f"[0:a]asplit=2[voice_out][voice_ctrl];"
            f"[1:a]volume={bgm_volume}[bgm_vol];"
            f"[bgm_vol][voice_ctrl]sidechaincompress=threshold=0.08:ratio=5:attack=50:release=1000[bgm_ducked];"
            f"[voice_out][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        subprocess.run([
            ffmpeg_exe, "-y",
            "-i", tts_master_path,
            "-stream_loop", "-1", "-i", bgm_path,
            "-filter_complex", ducking_filter,
            "-map", "[aout]",
            output_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    else:
        shutil.copy2(tts_master_path, output_path)
    
    return trimmed_durations
def _step1_prefilter_video(input_video_path, filtered_path, options, w_vid, h_vid):
    """Buoc 1: Ap dung bo loc hinh anh (hflip, crop, zoom, contrast) cho TOAN BO video chi 1 lan."""
    ffmpeg_exe = get_ffmpeg_exe()
    
    mirror = options.get("mirror", True)
    zoom_factor = float(options.get("zoom_factor", 1.08))
    contrast = float(options.get("contrast", 1.05))
    
    enable_zoom_crop = options.get("enable_zoom_crop", True)
    enable_ken_burns = options.get("enable_ken_burns", False)
    
    vf_parts = []
    
    # Neu enable_zoom_crop = True -> thuc hien cat bo watermark 10% (5% moi canh)
    if enable_zoom_crop:
        vf_parts.append("crop=in_w*0.9:in_h*0.9:in_w*0.05:in_h*0.05")
        
    if mirror:
        vf_parts.append("hflip")
        
    if enable_ken_burns:
        print("[INFO] Đã bật chế độ Khung hình Biết thở (Dynamic Ken Burns).")
        # Sử dụng biểu thức toán học thời gian t trong lệnh crop để tối ưu hóa CPU/GPU thay vì zoompan
        # Zoom mượt mà từ 1.0 lên 1.15 dựa trên thời gian (t)
        vf_parts.append("crop='iw/min(1.0+0.015*t,1.15)':'ih/min(1.0+0.015*t,1.15)':'(iw-iw/min(1.0+0.015*t,1.15))/2':'(ih-ih/min(1.0+0.015*t,1.15))/2'")
    elif enable_zoom_crop and zoom_factor != 1.0:
        print("[INFO] Chế độ Khung hình Biết thở đang TẮT (Render tốc độ cao).")
        # Zoom-in chi duoc thuc hien neu co zoom_factor va dc phep crop
        vf_parts.append(f"crop=in_w/{zoom_factor}:in_h/{zoom_factor}:(in_w-in_w/{zoom_factor})/2:(in_h-in_h/{zoom_factor})/2")
    
    # Khuon hinh tro ve do phan giai cua video goc, truoc khi vao buoc 2 cat ty le chuan (chỉ scale nếu đã crop/zoom)
    if enable_zoom_crop:
        vf_parts.append(f"scale={w_vid}:{h_vid}")
    
    if contrast != 1.0:
        vf_parts.append(f"eq=contrast={contrast}")
    
    vf_str = ",".join(vf_parts) if vf_parts else "copy"
    
    # Force keyframes every 1s for frame-accurate cuts later
    # NVENC (GPU - nhanh nhat) + Chat luong cao 12M va lanczos scaler
    cmd = [
        ffmpeg_exe, "-y", "-hwaccel", "auto",
        "-i", input_video_path,
        "-vf", vf_str,
        "-sws_flags", "lanczos",
        "-an",
        "-c:v", "h264_nvenc", "-preset", "p1", "-b:v", "12M",
        "-force_key_frames", "expr:gte(t,n_forced*1)",
        filtered_path
    ]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        # Fallback CPU
        cmd = [
            ffmpeg_exe, "-y",
            "-i", input_video_path,
            "-vf", vf_str,
            "-sws_flags", "lanczos",
            "-an",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-force_key_frames", "expr:gte(t,n_forced*1)",
            filtered_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def _step2_cut_segments(input_video_path, temp_dir, sentences, durations, D_video, speed_factor, timestamps=None, end_timestamps=None, scenes=None, assigned_scenes=None, options=None, audio_paths=None, diagnostic_log=None):
    """
    Buoc 2: Cat tung phan canh.
    
    PHASE 1.5: Neu co assigned_scenes (CLIP matching), cat tu scene duoc match.
    FALLBACK: Su dung piecewise linear interpolation giua cac timestamp.
    
    UPGRADE: Per-Segment Audio Muxing - nhung audio vao tung segment
    truoc concat de tranh drift tich luy.
    """
    ffmpeg_exe = get_ffmpeg_exe()
    N = len(sentences)
    total_audio_duration = sum(durations)
    
    options = options or {}
    aspect_ratio = options.get("aspect_ratio", "16:9")
    enable_ken_burns = options.get("enable_ken_burns", False)
    
    use_smart = (
        scenes is not None and assigned_scenes is not None and
        len(assigned_scenes) == N and len(scenes) > 0
    )
    use_ranges = bool(
        timestamps and end_timestamps
        and len(timestamps) == N and len(end_timestamps) == N
        and any(start is not None and end is not None for start, end in zip(timestamps, end_timestamps))
    )
    
    if use_ranges:
        print(f"[+] Step2: Su dung truc tiep timeline START-END cua Gemini")
    elif use_smart:
        print(f"[+] Step2: Su dung Adaptive Scene Matching 15-30-60s")
    else:
        print(f"[+] Step2: Su dung Linear Interpolation (fallback)")
    
    # === Xay dung MAP: sentence_index -> video_position (giay) ===
    # HYBRID: AI timestamps lam anchor chinh, audio duration lam tham chieu phu
    known_points = []  # list of (sentence_index, video_time_seconds)
    
    # Thu thap tat ca cac diem da biet (co timestamp AI)
    ai_anchors = {}
    last_valid_ts = -1.0
    if timestamps:
        for i in range(N):
            if i < len(timestamps) and timestamps[i] is not None:
                ts_val = float(timestamps[i])
                # Chi chap nhan timestamp tang dan de tranh AI hallucinate (di lui thoi gian)
                if last_valid_ts <= ts_val < D_video:
                    ai_anchors[i] = ts_val
                    known_points.append((i, ts_val))
                    last_valid_ts = ts_val
                else:
                    print(f"[-] Bo qua timestamp loi/di lui: {ts_val} tai cau {i}")
    
    # Tinh tong audio duration tich luy (de dung khi khong co AI timestamp)
    cumulative_audio = [0.0]
    for d in durations:
        cumulative_audio.append(cumulative_audio[-1] + d)
    total_audio = cumulative_audio[-1]
    
    # LUON bo sung timestamps cho cac cau khong co AI timestamp
    # (vi giờ timestamp chi moi 2-3 cau, can noi suy cho cau con lai)
    ai_coverage = len(ai_anchors) / max(1, N)
    if ai_coverage < 1.0:
        missing = N - len(ai_anchors)
        print(f"[+] Bo sung {missing}/{N} cau thieu timestamp bang interpolation (AI coverage={ai_coverage:.0%})")
        for i in range(N):
            if i not in ai_anchors:
                # Tim 2 AI anchor gan nhat (truoc va sau) de noi suy
                nearest_before = None
                nearest_after = None
                for ai_idx, ai_time in sorted(ai_anchors.items()):
                    if ai_idx <= i:
                        nearest_before = (ai_idx, ai_time)
                    if ai_idx >= i and nearest_after is None:
                        nearest_after = (ai_idx, ai_time)
                
                if nearest_before and nearest_after and nearest_before[0] != nearest_after[0]:
                    # Noi suy tuyen tinh dua tren ty le thoi luong audio thuc te
                    audio_range = cumulative_audio[nearest_after[0]] - cumulative_audio[nearest_before[0]]
                    time_range = nearest_after[1] - nearest_before[1]
                    if audio_range > 0:
                        audio_progress = cumulative_audio[i] - cumulative_audio[nearest_before[0]]
                        ratio = audio_progress / audio_range
                    else:
                        idx_range = nearest_after[0] - nearest_before[0]
                        ratio = (i - nearest_before[0]) / max(1, idx_range)
                    estimated_video_pos = nearest_before[1] + ratio * time_range
                elif nearest_before:
                    # Chi co anchor truoc -> uoc luong bang audio duration
                    audio_gap = cumulative_audio[i] - cumulative_audio[nearest_before[0]]
                    estimated_video_pos = nearest_before[1] + audio_gap
                elif nearest_after:
                    # Chi co anchor sau -> uoc luong nguoc lai
                    audio_gap = cumulative_audio[nearest_after[0]] - cumulative_audio[i]
                    estimated_video_pos = max(0, nearest_after[1] - audio_gap)
                else:
                    # Khong co anchor nao -> dung audio ratio
                    audio_pos_ratio = cumulative_audio[i] / max(0.1, total_audio)
                    estimated_video_pos = audio_pos_ratio * D_video
                
                known_points.append((i, estimated_video_pos))
    
    # Dam bao co it nhat 2 diem neo (dau va cuoi)
    if not known_points or known_points[0][0] != 0:
        known_points.insert(0, (0, 0.0))
    if known_points[-1][0] != N - 1:
        known_points.append((N - 1, D_video - 1.0))
    
    # Loai bo duplicates va sap xep
    seen_idx = set()
    unique_points = []
    for p in known_points:
        if p[0] not in seen_idx:
            seen_idx.add(p[0])
            unique_points.append(p)
    known_points = sorted(unique_points, key=lambda x: x[0])
    
    # Dam bao timestamps tang dan (khong lui)
    for j in range(1, len(known_points)):
        if known_points[j][1] <= known_points[j-1][1]:
            known_points[j] = (known_points[j][0], known_points[j-1][1] + 0.5)
    
    print(f"[+] Sync anchors: {len(known_points)}/{N} points (AI={len(ai_anchors)}, coverage={ai_coverage:.0%})")
    
    def get_video_position_for_sentence(idx):
        """Tinh video position cho sentence idx bang noi suy tuyen tinh giua 2 diem neo gan nhat."""
        if use_ranges and timestamps[idx] is not None:
            # START is the action anchor. Do not move it backwards merely because
            # narration is longer than the declared visual range.
            return min(max(float(timestamps[idx]), 0.0), max(0.0, D_video - 0.05))
        if use_smart:
            scene_idx = assigned_scenes[idx]
            if 0 <= scene_idx < len(scenes):
                scene = scenes[scene_idx]
                # Start at the matched source-shot boundary. The caller lets
                # playback continue naturally for the complete audio duration,
                # even when it crosses into following detected shots.
                return scene["start"]

        # Tim chính xác
        for ki, kv in known_points:
            if ki == idx:
                return kv
        
        # Noi suy giua 2 diem neo
        for j in range(len(known_points) - 1):
            i1, v1 = known_points[j]
            i2, v2 = known_points[j + 1]
            if i1 <= idx <= i2:
                if i2 == i1:
                    return v1
                
                # Uu tien dung audio duration de ty le chuan xac hon
                audio_range = cumulative_audio[i2] - cumulative_audio[i1]
                if audio_range > 0:
                    audio_progress = cumulative_audio[idx] - cumulative_audio[i1]
                    ratio = audio_progress / audio_range
                else:
                    ratio = (idx - i1) / (i2 - i1)
                    
                return v1 + ratio * (v2 - v1)
        
        # Fallback: dung ty le audio tren tong the
        audio_pos_ratio = cumulative_audio[idx] / max(0.1, total_audio)
        return audio_pos_ratio * D_video
    
    # === Lap ke hoach cat tung segment ===
    segment_jobs = []
    prev_video_end = 0.0  # Theo doi vi tri cuoi cua segment truoc
    prev_scene_idx = None

    source_h = int(options.get("_source_height", 1080))
    use_720p = source_h <= 720 and not options.get("force_1080p", False)
    if aspect_ratio == "9:16":
        target_w, target_h = (720, 1280) if use_720p else (1080, 1920)
    elif aspect_ratio == "1:1":
        target_w = target_h = 720 if use_720p else 1080
    else:
        target_w, target_h = (1280, 720) if use_720p else (1920, 1080)
    
    ranged_plan = None
    if use_ranges:
        fallback_starts = [get_video_position_for_sentence(index) for index in range(N)]
        ranged_plan = build_visual_edit_plan(
            durations,
            timestamps,
            end_timestamps,
            D_video,
            fallback_starts=fallback_starts,
        )

    for i in range(N):
        d_i = durations[i]

        if ranged_plan:
            edit = ranged_plan[i]
            S_i = edit["source_start"]
            source_duration = edit["source_duration"]
            output_duration = edit["output_duration"]
        else:
            # Lay video position truc tiep (KHONG dung audio time)
            S_i = get_video_position_for_sentence(i)
            source_duration = d_i
            output_duration = d_i

        # Consecutive narration may legitimately use the same long shot. Continue
        # from the previous endpoint instead of replaying the same frames.
        current_scene_idx = assigned_scenes[i] if use_smart else None
        if use_smart and i > 0 and current_scene_idx == prev_scene_idx:
            # This is one continuous use of the source shot, not a replay.
            S_i = prev_video_end
        elif use_smart and i > 0:
            # Never emit source frames which were already used before.
            S_i = max(S_i, prev_video_end)
        
        # PRE-ROLL: dich ve truoc de canh xuat hien truoc khi giong doc
        # NHUNG khong duoc trung lap voi segment truoc (gay giat hinh)
        if i > 0 and not use_smart and not use_ranges:
            pre_roll = 0.8 if N > 30 else 0.5
            proposed_start = max(0, S_i - pre_roll)
            
            # Chi ap dung pre-roll neu khong trung voi doan truoc
            if proposed_start >= prev_video_end:
                S_i = proposed_start
            else:
                # Bat dau ngay sau doan truoc (khong lap lai khung canh)
                S_i = max(prev_video_end, S_i)
        
        # Ranged clips were already bounded by the EDL. Fallback modes still
        # need enough real source footage and must never manufacture a freeze.
        if not ranged_plan:
            source_duration = min(source_duration, D_video)
            S_i = min(max(0.0, S_i), max(0.0, D_video - source_duration))
        
        # Cap nhat vi tri cuoi cua segment nay (do -shortest, chi dung d_i giay video)
        prev_video_end = min(D_video, S_i + source_duration)
        prev_scene_idx = current_scene_idx
        
        seg_path = os.path.join(temp_dir, f"seg_{i:04d}.mp4")
        
        scale_vf = f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,crop={target_w}:{target_h},setsar=1"
        
        if enable_ken_burns:
            combined_vf = (
                "crop='iw/min(1.0+0.01*t,1.12)':'ih/min(1.0+0.01*t,1.12)'"
                ":'(iw-iw/min(1.0+0.01*t,1.12))/2':'(ih-ih/min(1.0+0.01*t,1.12))/2',"
                f"{scale_vf}"
            )
        else:
            combined_vf = scale_vf

        segment_jobs.append((i, S_i, source_duration, output_duration, combined_vf, seg_path))

    # Merge consecutive narration items only when their selected source ranges
    # are truly continuous. Audio/subtitle timing remains the sum of durations.
    grouped_jobs = []
    for job in segment_jobs:
        index, start, source_duration, output_duration, video_filter, path = job
        if grouped_jobs and not ranged_plan:
            prev_index, prev_start, prev_source_duration, prev_output_duration, prev_filter, prev_path = grouped_jobs[-1]
            source_gap = abs(start - (prev_start + prev_source_duration))
            # Only merge truly contiguous source playback. A larger tolerance
            # silently includes frames between two distinct action ranges.
            normal_speed = (
                abs(source_duration - output_duration) <= 0.01
                and abs(prev_source_duration - prev_output_duration) <= 0.01
            )
            if source_gap <= 0.08 and normal_speed and prev_filter == video_filter and prev_output_duration + output_duration <= 30.0:
                grouped_jobs[-1] = (
                    prev_index,
                    prev_start,
                    prev_source_duration + source_duration,
                    prev_output_duration + output_duration,
                    prev_filter,
                    prev_path,
                )
                continue
        grouped_jobs.append(job)
    if len(grouped_jobs) < len(segment_jobs):
        print(f"[+] Da gom {len(segment_jobs)} cau thanh {len(grouped_jobs)} nhom canh lien tuc.")
    segment_jobs = grouped_jobs

    def render_segment(job):
        index, start, source_duration, output_duration, video_filter, path = job
        # Give the fps/trim filters two extra source frames. Reading exactly to
        # the requested boundary makes FFmpeg omit the final output frame.
        read_duration = min(max(0.0, D_video - start), source_duration + 2.0 / 30.0)
        timing_ratio = output_duration / max(source_duration, 0.001)
        timing_filter = (
            f"trim=duration={source_duration:.6f},"
            f"setpts={timing_ratio:.9f}*(PTS-STARTPTS),fps=30,"
            # FFmpeg represents duration by the timestamp after the last frame.
            # One bounded CFR guard frame prevents a 1/30s audio cut at EOF;
            # unlike the removed 3600s tpad, this can never create a visible hold.
            "tpad=stop_mode=clone:stop_duration=0.034,"
            f"trim=duration={output_duration:.6f},setpts=PTS-STARTPTS"
        )
        full_filter = f"{video_filter},{timing_filter}"
        common = [
            ffmpeg_exe, "-y", "-ss", f"{start:.3f}", "-t", f"{read_duration:.3f}",
            "-i", input_video_path, "-map", "0:v:0", "-an",
            "-vf", full_filter, "-r", "30", "-pix_fmt", "yuv420p",
        ]
        nvenc = common + [
            "-c:v", "h264_nvenc", "-preset", "p1", "-b:v", "10M", path,
        ]
        result = subprocess.run(nvenc, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
        if result.returncode != 0:
            if diagnostic_log:
                diagnostic_log("WARNING", f"FFmpeg NVENC segment {index} that bai (code {result.returncode}); chuyen sang CPU.", (result.stderr or "")[-8000:])
            cpu = common + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", path]
            cpu_result = subprocess.run(cpu, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
            if cpu_result.returncode != 0:
                details = (cpu_result.stderr or "")[-12000:]
                if diagnostic_log:
                    diagnostic_log("ERROR", f"FFmpeg CPU segment {index} that bai (code {cpu_result.returncode}).", details)
                raise RuntimeError(f"FFmpeg khong render duoc segment {index}; exit code {cpu_result.returncode}. Chi tiet trong task log.")
        if not os.path.exists(path) or os.path.getsize(path) < 500:
            raise RuntimeError(f"Khong the render segment {index}")
        return path

    # Bound concurrency for laptop GPUs with 4 GB VRAM.
    workers = max(1, min(int(options.get("render_workers", 3)), 3, len(segment_jobs)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        segment_paths = list(pool.map(render_segment, segment_jobs))
    return segment_paths

def _step3_concat_segments(segment_paths, output_path, temp_dir, diagnostic_log=None):
    """Buoc 3: Noi tat ca phan canh (concat demuxer)."""
    ffmpeg_exe = get_ffmpeg_exe()
    
    concat_file = os.path.join(temp_dir, "video_concat.txt")
    with open(concat_file, 'w', encoding='utf-8') as f:
        for sp in segment_paths:
            safe = sp.replace("\\", "/")
            f.write(f"file '{safe}'\n")
    
    cmd = [
        ffmpeg_exe, "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_file,
        "-c", "copy",
        output_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
    if result.returncode != 0:
        if diagnostic_log:
            diagnostic_log("ERROR", f"FFmpeg noi video that bai (code {result.returncode}).", (result.stderr or "")[-12000:])
        raise RuntimeError(f"FFmpeg noi video that bai; exit code {result.returncode}. Chi tiet trong task log.")

def create_master_video_raw(input_video_path, output_path, sentences, durations, options, update_fn=None, timestamps=None, end_timestamps=None, audio_paths=None, diagnostic_log=None):
    """Pipeline: Gemini time ranges -> cut continuous source segments -> concat."""
    D_video, w_vid, h_vid = get_video_info(input_video_path)
    speed_factor = float(options.get("speed_factor", 1.0))
    temp_dir = os.path.dirname(output_path)
    render_options = dict(options)
    render_options["_source_width"] = w_vid
    render_options["_source_height"] = h_vid
    
    # Gemini START-END ranges are authoritative. Semantic scene search used to
    # override correct anchors on cartoons, so it is intentionally bypassed.
    scenes = None
    assigned_scenes = None
    
    # Buoc 1: Gộp bộ lọc (Combined Filtergraph) - Tiết kiệm I/O
    if update_fn:
        update_fn(60, "Buoc 1/3: Dang khoi tao bo loc Combined Filtergraph...")
    
    # Buoc 2: Cat tung phan canh (voi Smart Scene Selection hoac fallback linear)
    if update_fn:
        mode = "Gemini START-END" if timestamps and end_timestamps else "Linear"
        update_fn(70, f"Buoc 2/3: Dang cat {len(sentences)} phan canh ({mode})...")
    segment_paths = _step2_cut_segments(
        input_video_path, temp_dir, sentences, durations, D_video, speed_factor, 
        timestamps, end_timestamps, scenes, assigned_scenes, render_options, None, diagnostic_log
    )
    
    # Buoc 3: Noi cac phan canh
    if update_fn:
        update_fn(80, "Buoc 3/3: Dang noi cac phan canh thanh video hoan chinh...")
    _step3_concat_segments(segment_paths, output_path, temp_dir, diagnostic_log)
    
    # Don dep file trung gian
    try:
        for sp in segment_paths:
            if os.path.exists(sp):
                os.remove(sp)
    except Exception:
        pass

def cleanup_temp_files(temp_dir):
    """Xóa bỏ toàn bộ file rác trung gian (master_audio.mp3, master_video_silent.mp4, các chunk...)."""
    try:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
            print(f"[+] Da don dep sach file rac trong: {temp_dir}")
    except Exception as e:
        print(f"[!] Loi khi don dep file rac {temp_dir}: {e}")

def _cleanup_old_outputs(output_dir, max_files=3):
    """Giu lai toi da max_files file moi nhat, xoa cu."""
    try:
        files = sorted(
            [os.path.join(output_dir, f) for f in os.listdir(output_dir) if os.path.isfile(os.path.join(output_dir, f))],
            key=os.path.getmtime, reverse=True
        )
        for old_file in files[max_files:]:
            os.remove(old_file)
    except Exception:
        pass


def fit_tts_to_time_ranges(audio_paths, durations, starts, ends, temp_dir, max_speed=1.0):
    """Keep narration cadence stable; visual ranges adapt to the spoken audio."""
    if not starts or not ends:
        return list(audio_paths), list(durations), False
    ffmpeg_exe = get_ffmpeg_exe()
    fitted_paths = list(audio_paths)
    fitted_durations = list(durations)
    adjusted = False

    for index in range(min(len(fitted_paths), len(fitted_durations), len(starts), len(ends))):
        start, end = starts[index], ends[index]
        if start is None or end is None or float(end) <= float(start):
            continue
        available = float(end) - float(start)
        duration = float(fitted_durations[index])
        if duration <= available + 0.05:
            continue

        factor = min(float(max_speed), duration / max(available - 0.08, 0.1))
        if factor <= 1.001:
            continue
        output_path = os.path.join(temp_dir, f"fitted_tts_{index:04d}.mp3")
        result = subprocess.run(
            [
                ffmpeg_exe, "-y", "-i", fitted_paths[index],
                "-filter:a", f"atempo={factor:.6f}",
                "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame", "-q:a", "3",
                output_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            fitted_paths[index] = output_path
            fitted_durations[index] = get_audio_duration(output_path)
            adjusted = True
            print(
                f"[+] TTS cau {index + 1}: tang toc nhe {factor:.3f}x "
                f"({duration:.2f}s -> {fitted_durations[index]:.2f}s)."
            )
    if adjusted:
        # The continuous master has the original tempo. Rebuild from adjusted
        # chunks whenever at least one beat needed a speed correction.
        marker_path = os.path.join(temp_dir, "_USE_MASTER_TTS_")
        try:
            if os.path.exists(marker_path):
                os.remove(marker_path)
        except OSError:
            pass
    return fitted_paths, fitted_durations, adjusted

def export_capcut_package(task_id, input_video_path, bgm_path, bgm_volume, voice_name, voice_rate, sentences, options, update_status_fn, diagnostic_log=None):
    temp_dir = os.path.join(os.path.dirname(input_video_path), f"temp_{task_id}")
    os.makedirs(temp_dir, exist_ok=True)
    
    clean_sentences, timestamps, end_timestamps, visual_groups = parse_sentences_and_visual_beats(sentences)
    
    try:
        # 1. TTS (SubMaker: giong doc lien mach + timing chinh xac)
        update_status_fn(10, "Đang tổng hợp giọng nói AI (TTS SubMaker)...")
        audio_paths, durations, word_cues = generate_all_tts(
            clean_sentences, voice_name, voice_rate, temp_dir, options, diagnostic_log,
            return_word_cues=True,
        )
        visual_groups = [
            group for sentence, group in zip(clean_sentences, visual_groups)
            if not sentence.startswith("<VOICE=")
        ]
        clean_sentences, timestamps, end_timestamps = _remove_voice_control_rows(
            clean_sentences, timestamps, end_timestamps
        )
        _require_exact_audio_alignment(clean_sentences, audio_paths, durations)
        audio_paths, durations, tts_adjusted = fit_tts_to_time_ranges(
            audio_paths, durations, timestamps, end_timestamps, temp_dir
        )
        
        # 2. Master Audio
        update_status_fn(30, "Đang phối hợp âm thanh và BGM...")
        master_audio_path = os.path.join(temp_dir, "master_audio.mp3")
        natural_durations = create_master_audio(
            audio_paths, bgm_path, bgm_volume, master_audio_path, temp_dir, clean_sentences
        )
        if natural_durations and len(natural_durations) == len(durations):
            tts_adjusted = tts_adjusted or any(
                abs(a - b) > 0.04 for a, b in zip(natural_durations, durations)
            )
            durations = natural_durations
        
        # 3. ASS - dung SubMaker ASS (chinh xac tung tu, bao gom style)
        update_status_fn(40, "Đang tạo phụ đề động chuẩn ASS (Karaoke/Hormozi)...")
        submaker_ass = os.path.join(temp_dir, "submaker_subtitles.ass")
        ass_path = os.path.join(temp_dir, "subtitles.ass")
        if os.path.exists(submaker_ass) and not tts_adjusted:
            shutil.copy2(submaker_ass, ass_path)
        else:
            # Fallback nếu không có submaker
            create_srt(clean_sentences, durations, ass_path)
        
        # 4. Master Video Raw (Pipeline 3 buoc sieu toc)
        update_status_fn(50, "Dang xu ly hinh anh lach ban quyen (Pipeline 3 buoc)...")
        master_video_path = os.path.join(temp_dir, "master_video_raw.mp4")
        visual_sentences, visual_durations, visual_starts, visual_ends = expand_visual_beats(
            clean_sentences, durations, visual_groups, word_cues
        )
        create_master_video_raw(input_video_path, master_video_path, visual_sentences, visual_durations, options, update_fn=update_status_fn, timestamps=visual_starts, end_timestamps=visual_ends, audio_paths=None, diagnostic_log=diagnostic_log)
        
        # 5. Zip it up
        update_status_fn(90, "Đang đóng gói ZIP cho CapCut...")
        output_filename = f"capcut_package_{task_id}.zip"
        output_dir = os.path.join(os.path.dirname(input_video_path), "static", "output")
        os.makedirs(output_dir, exist_ok=True)
        zip_path = os.path.join(output_dir, output_filename)
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(master_video_path, "1_Video_Goc.mp4")
            zipf.write(master_audio_path, "2_Am_Thanh.mp3")
            zipf.write(ass_path, "3_Phu_De.ass")
            
        update_status_fn(100, f"Dong goi hoan tat! San sang tai ve.")
        _cleanup_old_outputs(output_dir)
        return f"/static/output/{output_filename}"
        
    finally:
        cleanup_temp_files(temp_dir)

def render_review_video_ffmpeg(task_id, input_video_path, bgm_path, bgm_volume, voice_name, voice_rate, sentences, options, update_status_fn, diagnostic_log=None):
    temp_dir = os.path.join(os.path.dirname(input_video_path), f"temp_{task_id}")
    os.makedirs(temp_dir, exist_ok=True)
    
    clean_sentences, timestamps, end_timestamps, visual_groups = parse_sentences_and_visual_beats(sentences)
    
    try:
        update_status_fn(10, "Đang tổng hợp giọng nói AI (TTS SubMaker)...")
        audio_paths, durations, word_cues = generate_all_tts(
            clean_sentences, voice_name, voice_rate, temp_dir, options, diagnostic_log,
            return_word_cues=True,
        )
        visual_groups = [
            group for sentence, group in zip(clean_sentences, visual_groups)
            if not sentence.startswith("<VOICE=")
        ]
        clean_sentences, timestamps, end_timestamps = _remove_voice_control_rows(
            clean_sentences, timestamps, end_timestamps
        )
        _require_exact_audio_alignment(clean_sentences, audio_paths, durations)
        audio_paths, durations, tts_adjusted = fit_tts_to_time_ranges(
            audio_paths, durations, timestamps, end_timestamps, temp_dir
        )
        
        # Master Audio
        update_status_fn(30, "Đang phối hợp âm thanh và BGM...")
        master_audio_path = os.path.join(temp_dir, "master_audio.mp3")
        natural_durations = create_master_audio(
            audio_paths, bgm_path, bgm_volume, master_audio_path, temp_dir, clean_sentences
        )
        if natural_durations and len(natural_durations) == len(durations):
            tts_adjusted = tts_adjusted or any(
                abs(a - b) > 0.04 for a, b in zip(natural_durations, durations)
            )
            durations = natural_durations
        
        # ASS - dung SubMaker ASS (chinh xac tung tu, bao gom style)
        update_status_fn(40, "Đang tạo phụ đề động chuẩn ASS (Karaoke/Hormozi)...")
        submaker_ass = os.path.join(temp_dir, "submaker_subtitles.ass")
        ass_path = os.path.join(temp_dir, "subtitles.ass")
        if os.path.exists(submaker_ass) and not tts_adjusted:
            shutil.copy2(submaker_ass, ass_path)
        else:
            create_srt(clean_sentences, durations, ass_path)
        
        update_status_fn(50, "Dang xu ly hinh anh lach ban quyen (Pipeline 3 buoc)...")
        master_video_path = os.path.join(temp_dir, "master_video_raw.mp4")
        visual_sentences, visual_durations, visual_starts, visual_ends = expand_visual_beats(
            clean_sentences, durations, visual_groups, word_cues
        )
        if diagnostic_log:
            diagnostic_log(
                "INFO",
                f"Audio-led visual beats: {len(clean_sentences)} cau noi -> {len(visual_sentences)} canh hanh dong.",
            )
        create_master_video_raw(input_video_path, master_video_path, visual_sentences, visual_durations, options, update_fn=update_status_fn, timestamps=visual_starts, end_timestamps=visual_ends, audio_paths=None, diagnostic_log=diagnostic_log)
        
        update_status_fn(80, "Dang nung phu de vao video (Hardsub)...")
        output_filename = f"video_review_{task_id}.mp4"
        output_dir = os.path.join(os.path.dirname(input_video_path), "static", "output")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, output_filename)
        
        font_size_user = int(options.get("font_size", 42))
        font_color_hex = options.get("font_color", "#FFFFFF").lstrip('#')
        # Dam bao hex luon la 6 ky tu
        if len(font_color_hex) == 3:
            font_color_hex = ''.join([c*2 for c in font_color_hex])
        elif len(font_color_hex) != 6:
            font_color_hex = "FFFFFF"
        bg_opacity = int(options.get("bg_opacity", 150))
        bg_alpha_hex = f"{bg_opacity:02X}"
        
        # ASS Fontsize dung PlayResY mac dinh = 384
        # De chuyen tu pixel that tren video sang ASS Fontsize:
        # ass_size = user_px * 384 / video_height
        _, _, h_vid = get_video_info(master_video_path)
        ass_font_size = max(12, int(font_size_user * 384 / h_vid))
        
        # ASS PrimaryColour format: &H00BBGGRR (BGR order)
        r = font_color_hex[0:2]
        g = font_color_hex[2:4]
        b = font_color_hex[4:6]
        primary_color = f"&H00{b}{g}{r}"
        
        style = (
            f"Fontname=Arial,Fontsize={ass_font_size},Bold=1,"
            f"PrimaryColour={primary_color},"
            f"OutlineColour=&H00000000,"
            f"BackColour=&H80000000,"
            f"BorderStyle=1,"
            f"Outline=2.5,Shadow=1.5,"
            f"MarginL=60,MarginR=60,MarginV=25,"
            f"WrapStyle=0,Spacing=0.5"
        )
        
        ffmpeg_exe = get_ffmpeg_exe()
        
        # Copy SRT vao cung thu muc output de tranh loi escape duong dan Windows
        ass_copy_path = os.path.join(output_dir, f"temp_sub_{task_id}.ass")
        shutil.copy2(ass_path, ass_copy_path)
        
        # Su dung duong path tuong doi
        ass_rel = os.path.relpath(ass_copy_path, os.path.dirname(input_video_path))
        ass_ffmpeg = ass_rel.replace("\\", "/").replace(":", "\\:")
        
        enable_subtitles = options.get("enable_subtitles", True)
        
        if enable_subtitles:
            # Dùng thẳng bộ lọc ass hoặc subtitles mà KHÔNG chèn thêm force_style
            # vì file .ass đã bao gồm toàn bộ style (đậm, viền, font, căn giữa)
            vf_filter = f"ass='{ass_ffmpeg}'"
            cmd_sub = [
                ffmpeg_exe, "-y",
                "-i", master_video_path,
                "-i", master_audio_path,
                "-vf", vf_filter,
                "-sws_flags", "lanczos",
                "-map", "0:v", "-map", "1:a",
                "-c:v", "h264_nvenc", "-preset", "p1", "-b:v", "12M",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                output_path
            ]
            cmd_sub_cpu = [
                ffmpeg_exe, "-y",
                "-i", master_video_path,
                "-i", master_audio_path,
                "-vf", vf_filter,
                "-sws_flags", "lanczos",
                "-map", "0:v", "-map", "1:a",
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                output_path
            ]
        else:
            cmd_sub = [
                ffmpeg_exe, "-y",
                "-i", master_video_path,
                "-i", master_audio_path,
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                output_path
            ]
            cmd_sub_cpu = cmd_sub
        
        res = subprocess.run(cmd_sub, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace", cwd=os.path.dirname(input_video_path))
        if res.returncode != 0:
            if diagnostic_log:
                diagnostic_log("WARNING", f"FFmpeg xuat video lan dau that bai (code {res.returncode}); dang thu CPU.", (res.stderr or "")[-12000:])
            # This command really is CPU fallback (the previous implementation
            # accidentally retried NVENC and failed again on machines without it).
            cpu_res = subprocess.run(cmd_sub_cpu, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace", cwd=os.path.dirname(input_video_path))
            if cpu_res.returncode != 0:
                if diagnostic_log:
                    diagnostic_log("ERROR", f"FFmpeg xuat video CPU that bai (code {cpu_res.returncode}).", (cpu_res.stderr or "")[-12000:])
                raise RuntimeError(f"FFmpeg xuat video that bai; exit code {cpu_res.returncode}. Chi tiet trong task log.")
        
        # Don dep file SRT tam
        try:
            os.remove(ass_copy_path)
        except Exception:
            pass
        
        update_status_fn(100, f"Hoan thanh xuat video MP4!")
        _cleanup_old_outputs(output_dir)
        return f"/static/output/{output_filename}"
        
    finally:
        cleanup_temp_files(temp_dir)

def _generate_single_image(client, prompt, output_path, aspect_ratio):
    try:
        result = client.models.generate_images(
            model='imagen-3.0-generate-001',
            prompt=prompt,
            config=dict(
                number_of_images=1,
                output_mime_type="image/jpeg",
                aspect_ratio=aspect_ratio,
            )
        )
        if result.generated_images:
            image_bytes = result.generated_images[0].image.image_bytes
            with open(output_path, 'wb') as img_f:
                img_f.write(image_bytes)
            return True
    except Exception as e:
        print(f"Error generating image: {e}")
        return False
    return False

def generate_all_images(api_key, sentences, custom_style_prompt, temp_dir, aspect_ratio="16:9", update_prog=None):
    from google import genai
    client = genai.Client(api_key=api_key)
    image_paths = []
    
    if update_prog: update_prog(15, "Đang vẽ ảnh cho từng câu (Imagen 3)...")
    
    total = len(sentences)
    for i, text in enumerate(sentences):
        img_filename = f"img_{i:03d}.jpg"
        img_path = os.path.join(temp_dir, img_filename)
        
        prompt = f"{text}. {custom_style_prompt}"
        
        if update_prog: update_prog(15 + int((i/total)*20), f"Đang vẽ ảnh {i+1}/{total} (Imagen 3)...")
        
        success = _generate_single_image(client, prompt, img_path, aspect_ratio)
        if not success:
            ffmpeg_exe = get_ffmpeg_exe()
            w, h = (1920, 1080) if aspect_ratio == "16:9" else (1080, 1920)
            subprocess.run([
                ffmpeg_exe, "-y", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}", 
                "-vframes", "1", img_path
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
        image_paths.append(img_path)
        
    return image_paths

def render_ai_image_video_ffmpeg(task_id, sentences, durations, audio_paths, image_paths, bgm_path, bgm_volume, options, update_status_fn):
    ffmpeg_exe = get_ffmpeg_exe()
    temp_dir = os.path.join(os.path.dirname(bgm_path) if bgm_path else os.path.dirname(audio_paths[0]), f"temp_render_{task_id}")
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        update_status_fn(50, "Đang xử lý hình ảnh (Slideshow)...")
        concat_img_path = os.path.join(temp_dir, "concat_images.txt")
        with open(concat_img_path, 'w', encoding='utf-8') as f2:
            for i, img_path in enumerate(image_paths):
                safe_img = img_path.replace("\\", "/")
                f2.write(f"file '{safe_img}'\n")
                f2.write(f"duration {durations[i]}\n")
            # Python does not allow a backslash inside an f-string expression.
            last_safe_img = image_paths[-1].replace("\\", "/")
            f2.write(f"file '{last_safe_img}'\n")
            
        raw_video_path = os.path.join(temp_dir, "raw_video.mp4")
        w_res, h_res = (1920, 1080) if options.get("aspect_ratio", "16:9") == "16:9" else (1080, 1920)
        
        cmd_img = [
            ffmpeg_exe, "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_img_path,
            "-vf", f"scale={w_res}:{h_res}:force_original_aspect_ratio=decrease,pad={w_res}:{h_res}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
            "-c:v", "h264_nvenc", "-preset", "p6", "-cq", "23",
            "-pix_fmt", "yuv420p",
            raw_video_path
        ]
        import subprocess
        subprocess.run(cmd_img, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        update_status_fn(65, "Đang phối hợp âm thanh và BGM...")
        master_audio_path = os.path.join(temp_dir, "master_audio.mp3")
        create_master_audio(audio_paths, bgm_path, bgm_volume, master_audio_path, temp_dir)
        
        update_status_fn(75, "Đang tạo phụ đề động chuẩn ASS (Karaoke/Hormozi)...")
        ass_path = os.path.join(temp_dir, "subtitles.ass")
        # Generate ASS for AI Video
        submaker_ass = os.path.join(temp_dir, "submaker_subtitles.ass")
        if os.path.exists(submaker_ass):
            import shutil
            shutil.copy2(submaker_ass, ass_path)
        else:
            create_srt(sentences, durations, ass_path)
        
        update_status_fn(85, "Đang ghép video và nung phụ đề (Hardsub)...")
        
        output_filename = f"ai_video_{task_id}.mp4"
        import os
        output_dir = os.path.join(os.path.dirname(os.path.dirname(temp_dir)), "static", "output")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, output_filename)
        
        font_size_user = int(options.get("font_size", 42))
        font_color_hex = options.get("font_color", "#FFFFFF").lstrip('#')
        if len(font_color_hex) == 3: font_color_hex = ''.join([c*2 for c in font_color_hex])
        elif len(font_color_hex) != 6: font_color_hex = "FFFFFF"
        bg_opacity = int(options.get("bg_opacity", 150))
        bg_alpha_hex = f"{bg_opacity:02X}"
        
        ass_font_size = max(12, int(font_size_user * 384 / h_res))
        r = font_color_hex[0:2]
        g = font_color_hex[2:4]
        b = font_color_hex[4:6]
        primary_color = f"&H00{b}{g}{r}"
        style = (
            f"Fontname=Arial,Fontsize={ass_font_size},Bold=1,"
            f"PrimaryColour={primary_color},"
            f"OutlineColour=&H00000000,"
            f"BackColour=&H80000000,"
            f"BorderStyle=1,"
            f"Outline=2.5,Shadow=1.5,"
            f"MarginL=60,MarginR=60,MarginV=25,"
            f"WrapStyle=0,Spacing=0.5"
        )
        
        import shutil
        ass_copy_path = os.path.join(output_dir, f"temp_sub_{task_id}.ass")
        shutil.copy2(ass_path, ass_copy_path) # Assuming ass_path is passed or available
        
        import pathlib
        ass_ffmpeg = str(pathlib.Path(ass_copy_path).absolute()).replace('\\', '/').replace(':', '\\:')
        # Task 3: Hủy bỏ 2 dải đen Cinematic
        vf_filter = f"ass='{ass_ffmpeg}'"
        
        cmd_final = [
            ffmpeg_exe, "-y",
            "-i", raw_video_path,
            "-i", master_audio_path,
            "-vf", vf_filter,
            "-c:v", "h264_nvenc", "-preset", "p6", "-cq", "23",
            "-c:a", "aac",
            "-shortest",
            output_path
        ]
        subprocess.run(cmd_final, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        try: os.remove(ass_copy_path)
        except: pass
        
        update_status_fn(100, f"Hoàn thành xuất AI Video MP4!")
        _cleanup_old_outputs(output_dir)
        return f"/static/output/{output_filename}"
        
    finally:
        cleanup_temp_files(temp_dir)
