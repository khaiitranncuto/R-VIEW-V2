import concurrent.futures
import hashlib
import json
import os
import re
import subprocess

import numpy as np

from tts_engine import get_ffmpeg_exe


CACHE_VERSION = 7
_CLIP_MODEL = None
_CLIP_PROCESSOR = None


def _cache_paths(video_path):
    """Build a cache key which changes whenever the source file changes."""
    stat = os.stat(video_path)
    identity = f"{os.path.abspath(video_path)}|{stat.st_size}|{stat.st_mtime_ns}|{CACHE_VERSION}"
    key = hashlib.sha1(identity.encode("utf-8", errors="ignore")).hexdigest()[:20]
    cache_dir = os.path.join(os.path.dirname(video_path), ".scene_cache", key)
    return (
        cache_dir,
        os.path.join(cache_dir, "scenes.json"),
        os.path.join(cache_dir, "image_embeddings.npz"),
    )


def _detect_scenes(video_path, threshold=27.0, min_scene_len_frames=30):
    """Detect shot boundaries with bounded downscaling/frame skipping."""
    try:
        from scenedetect import ContentDetector, SceneManager, open_video

        video = open_video(video_path)
        manager = SceneManager()
        manager.auto_downscale = False
        manager.downscale = 4
        manager.add_detector(ContentDetector(threshold=threshold, min_scene_len=min_scene_len_frames))
        # On 60 FPS sources process every third frame. This keeps ~20 samples/s,
        # enough for shot boundaries while avoiding the previous full-frame scan.
        frame_skip = 2 if float(video.frame_rate) >= 50 else 1
        manager.detect_scenes(video=video, frame_skip=frame_skip, show_progress=False)
        scene_list = manager.get_scene_list()
        scenes = []
        for start, end in scene_list:
            start_sec = start.get_seconds()
            end_sec = end.get_seconds()
            scenes.append(
                {
                    "start": start_sec,
                    "end": end_sec,
                    "duration": end_sec - start_sec,
                    "mid": (start_sec + end_sec) / 2,
                }
            )
        print(f"[+] PySceneDetect: tim thay {len(scenes)} canh trong video.")
        return scenes
    except ImportError:
        print("[!] PySceneDetect chua cai dat. Dung fallback linear.")
        return None
    except Exception as exc:
        print(f"[!] Scene detection error: {exc}. Dung fallback linear.")
        return None


def _extract_keyframes(video_path, scenes, temp_dir):
    """Extract one representative frame from the middle of each scene."""
    ffmpeg_exe = get_ffmpeg_exe()
    keyframe_dir = os.path.join(temp_dir, "keyframes")
    os.makedirs(keyframe_dir, exist_ok=True)

    def extract_one(item):
        index, scene = item
        output_path = os.path.join(keyframe_dir, f"kf_{index:04d}.jpg")
        command = [
            ffmpeg_exe,
            "-y",
            "-ss",
            f"{scene['mid']:.3f}",
            "-i",
            video_path,
            "-vframes",
            "1",
            "-vf",
            "scale=224:224:force_original_aspect_ratio=increase,crop=224:224",
            "-q:v",
            "5",
            output_path,
        ]
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return index, output_path if result.returncode == 0 else None

    paths = [None] * len(scenes)
    workers = max(1, min(4, len(scenes)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for index, path in pool.map(extract_one, enumerate(scenes)):
            paths[index] = path
    return paths


def _extract_scene_keyframes(video_path, scenes, temp_dir):
    """Extract start/middle/end evidence for every detected source scene."""
    ffmpeg_exe = get_ffmpeg_exe()
    keyframe_dir = os.path.join(temp_dir, "keyframes_v3")
    os.makedirs(keyframe_dir, exist_ok=True)
    samples = []
    for scene_index, scene in enumerate(scenes):
        start = float(scene["start"])
        duration = max(float(scene["duration"]), 0.05)
        # Stay away from exact cut boundaries, where either neighbouring shot
        # can leak into the JPEG selected by ffmpeg.
        ratios = (0.20, 0.50, 0.80) if duration >= 0.75 else (0.35, 0.50, 0.65)
        for sample_index, ratio in enumerate(ratios):
            samples.append((scene_index, sample_index, start + duration * ratio))

    def extract_one(item):
        scene_index, sample_index, timestamp = item
        output_path = os.path.join(keyframe_dir, f"kf_{scene_index:04d}_{sample_index}.jpg")
        command = [
            ffmpeg_exe, "-y", "-ss", f"{timestamp:.3f}", "-i", video_path,
            "-vframes", "1", "-vf",
            "scale=224:224:force_original_aspect_ratio=increase,crop=224:224",
            "-q:v", "5", output_path,
        ]
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return scene_index, sample_index, output_path if result.returncode == 0 else None

    paths = [[None, None, None] for _ in scenes]
    workers = max(1, min(4, len(samples)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for scene_index, sample_index, path in pool.map(extract_one, samples):
            paths[scene_index][sample_index] = path
    return paths


def _load_clip():
    """Load multilingual SigLIP2, with the older CLIP model as fallback."""
    global _CLIP_MODEL, _CLIP_PROCESSOR
    if _CLIP_MODEL is not None:
        return _CLIP_MODEL, _CLIP_PROCESSOR
    try:
        import torch
        from transformers import AutoModel, AutoProcessor, CLIPModel, CLIPProcessor

        print("[+] Dang tai mo hinh hinh-van ban...")
        try:
            model_name = "google/siglip2-base-patch16-224"
            _CLIP_MODEL = AutoModel.from_pretrained(model_name, local_files_only=True)
            _CLIP_PROCESSOR = AutoProcessor.from_pretrained(model_name, local_files_only=True)
            print("[+] Dang dung SigLIP2 da ngon ngu.")
        except Exception:
            model_name = "openai/clip-vit-base-patch16"
            try:
                _CLIP_MODEL = CLIPModel.from_pretrained(model_name, local_files_only=True)
                _CLIP_PROCESSOR = CLIPProcessor.from_pretrained(model_name, local_files_only=True)
            except Exception:
                _CLIP_MODEL = CLIPModel.from_pretrained(model_name)
                _CLIP_PROCESSOR = CLIPProcessor.from_pretrained(model_name)
            print("[!] SigLIP2 khong san sang; dung CLIP du phong.")
        if torch.cuda.is_available():
            _CLIP_MODEL = _CLIP_MODEL.to("cuda")
        _CLIP_MODEL.eval()
        print("[+] Mo hinh hinh-van ban da san sang.")
        return _CLIP_MODEL, _CLIP_PROCESSOR
    except ImportError:
        print("[!] transformers/torch chua cai. Khong dung CLIP.")
    except Exception as exc:
        print(f"[!] CLIP load error: {exc}")
    return None, None


def _model_device(model):
    try:
        return next(model.parameters()).device
    except Exception:
        return "cpu"


def _feature_tensor(result):
    """Support both old and new Transformers CLIP return types."""
    if hasattr(result, "pooler_output"):
        return result.pooler_output
    if hasattr(result, "image_embeds"):
        return result.image_embeds
    if hasattr(result, "text_embeds"):
        return result.text_embeds
    return result


def _encode_keyframes_clip(keyframe_paths):
    """Encode keyframes in batches instead of one model call per frame."""
    model, processor = _load_clip()
    if model is None:
        return None
    try:
        import torch
        from PIL import Image

        embeddings = [None] * len(keyframe_paths)
        device = _model_device(model)
        for start in range(0, len(keyframe_paths), 24):
            indexes, images = [], []
            for index in range(start, min(start + 24, len(keyframe_paths))):
                path = keyframe_paths[index]
                if path and os.path.exists(path) and os.path.getsize(path) > 100:
                    with Image.open(path) as image:
                        images.append(image.convert("RGB"))
                    indexes.append(index)
            if not images:
                continue
            inputs = processor(images=images, return_tensors="pt", padding=True)
            inputs = {name: value.to(device) for name, value in inputs.items()}
            with torch.no_grad():
                features = _feature_tensor(model.get_image_features(**inputs))
                batch = features.float().cpu().numpy()
            batch /= np.linalg.norm(batch, axis=1, keepdims=True) + 1e-8
            for index, embedding in zip(indexes, batch):
                embeddings[index] = embedding
        return embeddings
    except Exception as exc:
        print(f"[!] CLIP encode keyframes error: {exc}")
        return None


def _encode_sentences_clip(sentences):
    """Encode narration text in batches."""
    model, processor = _load_clip()
    if model is None:
        return None
    try:
        import torch

        embeddings = []
        device = _model_device(model)
        text_config = getattr(getattr(model, "config", None), "text_config", None)
        max_text_length = int(getattr(text_config, "max_position_embeddings", 77) or 77)
        for start in range(0, len(sentences), 32):
            texts = [sentence[:200] for sentence in sentences[start : start + 32]]
            inputs = processor(
                text=texts,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=max_text_length,
            )
            inputs = {name: value.to(device) for name, value in inputs.items()}
            with torch.no_grad():
                features = _feature_tensor(model.get_text_features(**inputs))
                batch = features.float().cpu().numpy()
            batch /= np.linalg.norm(batch, axis=1, keepdims=True) + 1e-8
            embeddings.extend(batch)
        return embeddings
    except Exception as exc:
        print(f"[!] CLIP encode sentences error: {exc}")
        return None


def _match_scenes_to_sentences(sentence_embs, scene_embs, scenes, timestamps, durations, video_duration):
    """Balance semantic similarity, timeline, duration, and recent reuse."""
    sentence_count = len(sentence_embs)
    scene_count = len(scene_embs)
    if not scene_count:
        return [0] * sentence_count

    first_valid = next((item for item in scene_embs if item is not None), None)
    if first_valid is None:
        return None
    scene_matrix = np.zeros((scene_count, len(first_valid)), dtype=np.float32)
    valid_scene = np.zeros(scene_count, dtype=np.bool_)
    for index, embedding in enumerate(scene_embs):
        if embedding is not None:
            scene_matrix[index] = embedding
            valid_scene[index] = True

    cumulative = np.cumsum([0.0] + list(durations))
    total_audio = max(float(cumulative[-1]), 0.1)
    assigned = []
    recent = []

    for index, sentence_embedding in enumerate(sentence_embs):
        similarities = scene_matrix @ sentence_embedding if sentence_embedding is not None else np.zeros(scene_count)
        similarities[~valid_scene] = -1.0
        low, high = float(similarities.min()), float(similarities.max())
        semantic_range = max(high - low, 1e-6)

        if timestamps and index < len(timestamps) and timestamps[index] is not None:
            expected_time = min(max(float(timestamps[index]), 0.0), video_duration)
        else:
            expected_time = (float(cumulative[index]) / total_audio) * video_duration

        best_scene, best_score = None, -1e9
        for scene_index, scene in enumerate(scenes):
            if not valid_scene[scene_index]:
                continue
            semantic = (float(similarities[scene_index]) - low) / semantic_range
            normalized_distance = abs(float(scene["mid"]) - expected_time) / max(video_duration, 1.0)
            temporal = max(0.0, 1.0 - normalized_distance * 4.0)
            duration_fit = min(float(scene["duration"]) / max(float(durations[index]), 0.3), 1.0)
            if recent and scene_index == recent[-1]:
                reuse_penalty = 0.30
            elif scene_index in recent[-3:]:
                reuse_penalty = 0.14
            else:
                reuse_penalty = 0.0
            jump_penalty = min(normalized_distance, 0.5) * 0.10
            score = 0.55 * semantic + 0.38 * temporal + 0.07 * duration_fit - reuse_penalty - jump_penalty
            if score > best_score:
                best_score = score
                best_scene = scene_index

        if best_scene is None:
            best_scene = min(range(scene_count), key=lambda j: abs(float(scenes[j]["mid"]) - expected_time))
        assigned.append(best_scene)
        recent.append(best_scene)
    return assigned


def _load_cached_embeddings(path, expected_count):
    try:
        packed = np.load(path)
        matrix, valid = packed["embeddings"], packed["valid"]
        if len(matrix) != expected_count:
            return None
        return [matrix[index] if valid[index] else None for index in range(expected_count)]
    except Exception:
        return None


def _save_cached_embeddings(path, embeddings):
    valid = np.array([item is not None for item in embeddings], dtype=np.bool_)
    dimension = next((len(item) for item in embeddings if item is not None), 0)
    matrix = np.zeros((len(embeddings), dimension), dtype=np.float32)
    for index, embedding in enumerate(embeddings):
        if embedding is not None:
            matrix[index] = embedding
    np.savez_compressed(path, embeddings=matrix, valid=valid)


def smart_scene_selection_full(video_path, sentences, timestamps, durations, temp_dir, update_fn=None):
    """Detect, cache, understand and assign a scene to every narration sentence."""
    if update_fn:
        update_fn(52, "Smart Scene: Dang phan tich canh trong video goc...")

    cache_dir, scenes_path, embeddings_path = _cache_paths(video_path)
    os.makedirs(cache_dir, exist_ok=True)

    scenes = None
    if os.path.exists(scenes_path):
        try:
            with open(scenes_path, "r", encoding="utf-8") as handle:
                scenes = json.load(handle)
            print(f"[+] Smart Scene cache: tai {len(scenes)} canh.")
        except Exception:
            scenes = None

    if not scenes:
        scenes = _detect_scenes(video_path)
        if scenes:
            with open(scenes_path, "w", encoding="utf-8") as handle:
                json.dump(scenes, handle, ensure_ascii=False)
    if not scenes or len(scenes) < 3:
        print("[!] Khong du scenes. Fallback linear.")
        return None, None

    video_duration = float(scenes[-1]["end"])
    scene_embeddings = None
    if os.path.exists(embeddings_path):
        scene_embeddings = _load_cached_embeddings(embeddings_path, len(scenes))
        if scene_embeddings is not None:
            print(f"[+] Smart Scene cache: tai {len(scene_embeddings)} CLIP embeddings.")

    if scene_embeddings is None:
        if update_fn:
            update_fn(54, f"Smart Scene: Dang trich xuat {len(scenes)} keyframes...")
        keyframes = _extract_keyframes(video_path, scenes, temp_dir)
        if update_fn:
            update_fn(56, "Smart Scene: Dang ma hoa keyframes voi CLIP...")
        scene_embeddings = _encode_keyframes_clip(keyframes)
        if scene_embeddings is not None:
            _save_cached_embeddings(embeddings_path, scene_embeddings)
    if scene_embeddings is None:
        print("[!] CLIP keyframes that bai. Fallback linear.")
        return scenes, None

    if update_fn:
        update_fn(58, "Smart Scene: Dang ghep cau thoai voi canh...")
    visual_queries = []
    for index, sentence in enumerate(sentences):
        # Short verb clauses often omit the subject ("lao tới", "cuốn lấy...").
        # Add the preceding clause at the same timeline anchor for visual
        # context, while leaving the spoken TTS text untouched.
        same_anchor = (
            index > 0 and timestamps and index < len(timestamps)
            and timestamps[index] is not None
            and timestamps[index] == timestamps[index - 1]
        )
        if same_anchor and len(sentence.split()) < 7:
            visual_queries.append(f"{sentences[index - 1]} {sentence}")
        else:
            visual_queries.append(sentence)
    sentence_embeddings = _encode_sentences_clip(visual_queries)
    if sentence_embeddings is None:
        print("[!] CLIP sentences that bai. Fallback linear.")
        return scenes, None

    assigned = _match_scenes_to_sentences(
        sentence_embeddings,
        scene_embeddings,
        scenes,
        timestamps,
        durations,
        video_duration,
    )
    if not assigned:
        return scenes, None
    print(f"[+] Smart Scene: {len(assigned)} cau dung {len(set(assigned))}/{len(scenes)} canh.")
    return scenes, assigned


def _estimate_sentence_times(timestamps, durations, video_duration):
    """Fill missing Gemini timestamps without scanning the source video."""
    count = len(durations)
    cumulative = np.cumsum([0.0] + list(durations))
    total_audio = max(float(cumulative[-1]), 0.1)
    anchors = {
        index: float(value)
        for index, value in enumerate(timestamps or [])
        if index < count and value is not None and 0 <= float(value) < video_duration
    }
    expected = []
    for index in range(count):
        if index in anchors:
            expected.append(anchors[index])
            continue
        before = [(i, value) for i, value in anchors.items() if i < index]
        after = [(i, value) for i, value in anchors.items() if i > index]
        left = max(before, default=None, key=lambda item: item[0])
        right = min(after, default=None, key=lambda item: item[0])
        if left and right:
            audio_span = float(cumulative[right[0]] - cumulative[left[0]])
            ratio = (
                float(cumulative[index] - cumulative[left[0]]) / audio_span
                if audio_span > 0 else (index - left[0]) / max(1, right[0] - left[0])
            )
            value = left[1] + ratio * (right[1] - left[1])
        elif left:
            value = left[1] + float(cumulative[index] - cumulative[left[0]])
        elif right:
            value = right[1] - float(cumulative[right[0]] - cumulative[index])
        else:
            value = float(cumulative[index]) / total_audio * video_duration
        expected.append(min(max(value, 0.0), max(0.0, video_duration - durations[index])))
    return expected


def _load_local_embedding_cache(path):
    try:
        packed = np.load(path)
        times = packed["times"]
        embeddings = packed["embeddings"]
        return {round(float(t), 3): embeddings[i] for i, t in enumerate(times)}
    except Exception:
        return {}


def _save_local_embedding_cache(path, embedding_map):
    if not embedding_map:
        return
    ordered = sorted(embedding_map.items())
    np.savez_compressed(
        path,
        times=np.array([item[0] for item in ordered], dtype=np.float32),
        embeddings=np.stack([item[1] for item in ordered]).astype(np.float32),
    )


def smart_scene_selection(video_path, sentences, timestamps, durations, temp_dir, update_fn=None, video_duration=None):
    """Adaptive semantic search in +/-15, +/-30, then +/-60 second windows."""
    if not sentences or not durations:
        return None, None
    if not video_duration:
        video_duration = max(
            [float(value) for value in (timestamps or []) if value is not None] + [sum(durations)]
        )

    if update_fn:
        update_fn(52, "Smart Scene: Dang lap ban do canh nhanh...")

    expected_times = _estimate_sentence_times(timestamps, durations, video_duration)
    cache_dir, scenes_path, _old_embeddings_path = _cache_paths(video_path)
    os.makedirs(cache_dir, exist_ok=True)

    scenes = None
    if os.path.exists(scenes_path):
        try:
            with open(scenes_path, "r", encoding="utf-8") as handle:
                scenes = json.load(handle)
            print(f"[+] Adaptive Scene cache: tai {len(scenes)} ranh gioi canh.")
        except Exception:
            scenes = None
    if not scenes:
        scenes = _detect_scenes(video_path)
        if scenes:
            with open(scenes_path, "w", encoding="utf-8") as handle:
                json.dump(scenes, handle, ensure_ascii=False)
    if not scenes:
        print("[!] Khong do duoc ranh gioi canh. Dung timestamp Gemini.")
        fallback = []
        for index, expected in enumerate(expected_times):
            start = min(max(expected, 0.0), max(0.0, video_duration - durations[index]))
            fallback.append({"start": start, "end": start + durations[index], "duration": durations[index], "mid": start})
        return fallback, list(range(len(fallback)))

    embeddings_path = os.path.join(cache_dir, "adaptive_scene_embeddings_3frames.npz")
    scene_embeddings = None
    if os.path.exists(embeddings_path):
        try:
            packed = np.load(embeddings_path)
            cached = packed["embeddings"]
            if cached.ndim == 3 and cached.shape[:2] == (len(scenes), 3):
                scene_embeddings = cached
                print(f"[+] Adaptive Scene cache: tai 3 keyframes cho {len(scenes)} canh.")
        except Exception:
            scene_embeddings = None

    if scene_embeddings is None:
        keyframes_by_scene = _extract_scene_keyframes(video_path, scenes, temp_dir)
        flat_keyframes = [path for group in keyframes_by_scene for path in group]
        if update_fn:
            update_fn(55, f"Smart Scene: Dang ma hoa {len(flat_keyframes)} keyframes dau-giua-cuoi...")
        encoded = _encode_keyframes_clip(flat_keyframes)
        first_valid = next((item for item in (encoded or []) if item is not None), None)
        if first_valid is not None:
            scene_embeddings = np.zeros((len(scenes), 3, len(first_valid)), dtype=np.float32)
            for flat_index, embedding in enumerate(encoded):
                if embedding is not None:
                    scene_embeddings[flat_index // 3, flat_index % 3] = embedding
            np.savez_compressed(embeddings_path, embeddings=scene_embeddings)

    if update_fn:
        update_fn(58, "Smart Scene: Dang tim canh trong cua so 15-30-60 giay...")
    visual_queries = []
    for index, sentence in enumerate(sentences):
        same_anchor = (
            index > 0 and timestamps and index < len(timestamps)
            and timestamps[index] is not None
            and timestamps[index] == timestamps[index - 1]
        )
        if same_anchor and len(sentence.split()) < 7:
            visual_queries.append(f"{sentences[index - 1]} {sentence}")
        else:
            visual_queries.append(sentence)
    sentence_embeddings = _encode_sentences_clip(visual_queries)
    if sentence_embeddings is None or scene_embeddings is None:
        sentence_embeddings = [None] * len(sentences)

    assigned = []
    used_scenes = set()
    last_scene_index = None
    current_run_duration = 0.0
    current_run_source_start = 0.0
    windows = (15.0, 30.0, 60.0)
    min_shot_duration = 2.5

    for index, sentence_embedding in enumerate(sentence_embeddings):
        expected = expected_times[index]
        transition_hint = bool(re.search(
            r'\b(?:xuất hiện|bất ngờ|đột nhiên|lao tới|bắt đầu|bước vào|'
            r'tấn công|nhảy|rơi xuống|ập tới|xông tới|emerge|appear|attack)\b',
            sentences[index],
            flags=re.IGNORECASE,
        ))
        if sentence_embedding is not None and scene_embeddings is not None:
            # A concrete action may only appear at the beginning or end of a
            # shot. Use the strongest of all three visual samples.
            raw_semantic = np.max(scene_embeddings @ sentence_embedding, axis=1)
            low, high = float(raw_semantic.min()), float(raw_semantic.max())
            semantic_normalized = (raw_semantic - low) / max(high - low, 1e-6)
        else:
            raw_semantic = np.zeros(len(scenes), dtype=np.float32)
            semantic_normalized = np.zeros(len(scenes), dtype=np.float32)

        # Do not flash a new shot for tiny narration fragments. Reusing the
        # immediately previous shot here is one continuous source run, not a
        # replay; the renderer resumes at the prior endpoint.
        if last_scene_index is not None and current_run_duration < min_shot_duration:
            chosen = last_scene_index
        else:
            chosen = None
        for window in windows:
            if chosen is not None:
                break
            candidates = []
            for scene_index, scene in enumerate(scenes):
                distance = abs(float(scene["mid"]) - expected)
                if distance > window:
                    continue
                # A source shot can only be entered once and the story never
                # moves backwards. Consecutive continuation was handled above.
                if scene_index in used_scenes:
                    continue
                if last_scene_index is not None and scene_index <= last_scene_index:
                    continue
                temporal = max(0.0, 1.0 - distance / window)
                continuity = 0.0
                if last_scene_index is not None:
                    index_gap = scene_index - last_scene_index
                    continuity = max(0.0, 1.0 - abs(index_gap) / 8.0)
                duration_fit = min(float(scene["duration"]) / max(float(durations[index]), 0.3), 1.0)
                if transition_hint:
                    # Gemini timestamps commonly mark the narration beat a few
                    # seconds before a newly appearing action. Prefer a shot
                    # beginning just after the anchor instead of the old shot
                    # which merely contains the timestamp.
                    start_delta = float(scene["start"]) - expected
                    if -1.0 <= start_delta <= 18.0:
                        action_timing = max(0.0, 1.0 - abs(start_delta - 4.0) / 14.0)
                    else:
                        action_timing = 0.0
                    score = (
                        0.50 * float(semantic_normalized[scene_index]) +
                        0.18 * temporal + 0.07 * continuity +
                        0.03 * duration_fit + 0.22 * action_timing
                    )
                else:
                    score = (
                        0.68 * float(semantic_normalized[scene_index]) +
                        0.22 * temporal + 0.07 * continuity +
                        0.03 * duration_fit
                    )
                candidates.append(
                    (score, float(semantic_normalized[scene_index]), float(raw_semantic[scene_index]), scene_index)
                )

            if not candidates:
                continue
            candidates.sort(reverse=True)
            best = candidates[0]
            margin = best[1] - candidates[1][1] if len(candidates) > 1 else 1.0
            # Model-independent confidence: normalized visual evidence and its
            # lead over the second candidate decide whether to widen the window.
            semantic_is_clear = (
                len(candidates) == 1 or
                (best[1] >= 0.72 and margin >= 0.06) or
                margin >= 0.15
            )
            if sentence_embedding is None or semantic_is_clear:
                chosen = best[3]
                break

        if chosen is None:
            # Prefer an unused future scene still inside the hard 60-second
            # window. If none exists, continue the current source run instead
            # of replaying an old shot or jumping backwards.
            allowed = [
                scene_index for scene_index, scene in enumerate(scenes)
                if scene_index not in used_scenes
                and (last_scene_index is None or scene_index > last_scene_index)
                and abs(float(scene["mid"]) - expected) <= 60.0
            ]
            if allowed:
                chosen = min(allowed, key=lambda j: abs(float(scenes[j]["mid"]) - expected))
            elif last_scene_index is not None:
                chosen = last_scene_index
            else:
                chosen = min(range(len(scenes)), key=lambda j: abs(float(scenes[j]["mid"]) - expected))

        assigned.append(chosen)
        if chosen == last_scene_index:
            current_run_duration += float(durations[index])
        else:
            current_run_duration = float(durations[index])
            current_run_source_start = float(scenes[chosen]["start"])
        # Mark every detected shot crossed by this continuous source range as
        # consumed. It cannot be selected again later under another index.
        consumed_until = current_run_source_start + current_run_duration
        for scene_index in range(chosen, len(scenes)):
            if float(scenes[scene_index]["start"]) < consumed_until:
                used_scenes.add(scene_index)
            else:
                break
        last_scene_index = chosen

    print(
        f"[+] Smart Scene thich ung: {len(assigned)} cau, "
        f"{len(used_scenes)} canh duy nhat, cua so toi da 60 giay."
    )
    return scenes, assigned
