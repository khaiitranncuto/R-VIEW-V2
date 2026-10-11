import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed


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


def _format_time(seconds):
    value = max(0.0, float(seconds))
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    secs = value % 60
    return f"{hours:02d}:{minutes:02d}:{secs:05.2f}"


def clamp_final_timeline_end(text, video_duration):
    """Clamp only the final TIME range when Gemini runs past the source end."""
    source = str(text or "")
    marker_re = re.compile(
        r"\[(TIME)\s*=\s*(\d{1,3}:\d{2}(?::\d{2})?(?:\.\d{1,3})?)\s*-\s*"
        r"(\d{1,3}:\d{2}(?::\d{2})?(?:\.\d{1,3})?)\s*\]",
        re.IGNORECASE,
    )
    matches = list(marker_re.finditer(source))
    if not matches:
        return source
    match = matches[-1]
    duration = max(0.0, float(video_duration))
    start = float(_to_seconds(match.group(2)))
    end = float(_to_seconds(match.group(3)))
    if end <= duration or start >= duration:
        return source
    replacement = f"[TIME={match.group(2)}-{_format_time(duration)}]"
    return source[:match.start()] + replacement + source[match.end():]


def repair_nonpositive_timeline_ranges(text, video_duration):
    """Give zero/backwards TIME ranges a short positive end inside the source."""
    source = str(text or "")
    marker_re = re.compile(
        r"\[(TIME)\s*=\s*(\d{1,3}:\d{2}(?::\d{2})?(?:\.\d{1,3})?)\s*-\s*"
        r"(\d{1,3}:\d{2}(?::\d{2})?(?:\.\d{1,3})?)\s*\]",
        re.IGNORECASE,
    )
    matches = list(marker_re.finditer(source))
    if not matches:
        return source
    duration = max(0.0, float(video_duration))
    chunks = []
    cursor = 0
    for index, match in enumerate(matches):
        chunks.append(source[cursor:match.start()])
        start = float(_to_seconds(match.group(2)))
        end = float(_to_seconds(match.group(3)))
        replacement = match.group(0)
        if end <= start and start < duration:
            repaired_end = min(duration, start + 1.0)
            if index + 1 < len(matches):
                next_start = float(_to_seconds(matches[index + 1].group(2)))
                if next_start > start:
                    repaired_end = min(repaired_end, next_start)
            if repaired_end > start:
                replacement = f"[TIME={match.group(2)}-{_format_time(repaired_end)}]"
        chunks.append(replacement)
        cursor = match.end()
    chunks.append(source[cursor:])
    return "".join(chunks)


def validate_timeline_ranges(text, video_duration):
    """Reject invalid or overlapping TIME ranges.

    ``video_duration`` is retained for API compatibility, but timestamps past
    the reported container duration are allowed. Some sources report a shorter
    duration than their final usable timestamp.
    """
    marker_re = re.compile(
        r"\[(TIME|BEAT)\s*=\s*(\d{1,3}:\d{2}(?::\d{2})?(?:\.\d{1,3})?)\s*-\s*"
        r"(\d{1,3}:\d{2}(?::\d{2})?(?:\.\d{1,3})?)\s*\]",
        re.IGNORECASE,
    )
    matches = list(marker_re.finditer(text or ""))
    if not matches:
        raise ValueError("Kich ban khong co TIME range hop le.")
    previous_end = -1.0
    previous_time_index = 0
    for index, match in enumerate(matches, start=1):
        kind = match.group(1).upper()
        start = float(_to_seconds(match.group(2)))
        end = float(_to_seconds(match.group(3)))
        if end <= start:
            raise ValueError(f"Dong {index} co TIME_END khong lon hon TIME_START.")
        if kind == "TIME" and start < previous_end - 0.05:
            raise ValueError(
                f"Dong {index} bat dau {_format_time(start)} nhung dong "
                f"{previous_time_index} ket thuc {_format_time(previous_end)}; "
                "hai khoang dang di lui hoac chong len nhau."
            )
        if kind == "TIME":
            previous_end = end
            previous_time_index = index
    return True


def build_timeline_verification_reel(
    source_path, timeline_events, output_dir, ffmpeg_exe="ffmpeg", log_fn=None,
    progress_fn=None, max_workers=2, output_fps=4.0, seek_preroll=3.0,
):
    """Build exact TIME clips without decoding from source start for every scene."""
    if not timeline_events:
        raise ValueError("Khong co canh TIME de tao video kiem tra.")
    os.makedirs(output_dir, exist_ok=True)
    concat_path = os.path.join(output_dir, "timeline_verify_concat.txt")
    segment_paths = [None] * len(timeline_events)
    font_path = "C\\:/Windows/Fonts/arialbd.ttf"

    def render_segment(index, event):
        row_id = str(event.get("id") or f"R{index + 1:04d}").upper()
        start = max(0.0, float(event["start"]))
        end = max(start + 0.08, float(event["end"]))
        segment_path = os.path.join(output_dir, f"timeline_verify_{index:04d}.mp4")
        label = re.sub(r"[^A-Z0-9_-]", "", row_id) or f"R{index + 1:04d}"
        preroll = min(max(0.0, float(seek_preroll)), start)
        input_seek = max(0.0, start - preroll)
        video_filter = (
            f"setpts=PTS-STARTPTS,scale=-2:480,fps={float(output_fps):g},"
            f"drawtext=fontfile='{font_path}':text='{label}':"
            "x=18:y=18:fontsize=38:fontcolor=white:borderw=3:bordercolor=black,"
            f"drawtext=fontfile='{font_path}':text='OFFSET %{{pts\\:hms}}':"
            "x=18:y=64:fontsize=24:fontcolor=white:borderw=2:bordercolor=black"
        )
        command = [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            # Fast input seek jumps close to the scene. The small output seek
            # then decodes only the preroll and preserves an accurate START.
            "-ss",
            f"{input_seek:.3f}",
            "-i",
            source_path,
            "-ss",
            f"{preroll:.3f}",
            "-t",
            f"{end - start:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            video_filter,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "48k",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-movflags",
            "+faststart",
            segment_path,
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if result.returncode != 0 or not os.path.isfile(segment_path):
            raise RuntimeError(
                f"Khong tao duoc clip kiem tra {row_id}: {result.stderr[-800:]}"
            )
        return index, segment_path

    worker_count = max(1, min(int(max_workers), len(timeline_events)))
    completed = 0
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(render_segment, index, event)
            for index, event in enumerate(timeline_events)
        ]
        for future in as_completed(futures):
            index, segment_path = future.result()
            segment_paths[index] = segment_path
            completed += 1
            if progress_fn:
                progress_fn(completed, len(timeline_events))

    with open(concat_path, "w", encoding="utf-8", newline="\n") as handle:
        for segment_path in segment_paths:
            escaped = os.path.abspath(segment_path).replace("'", "'\\''").replace("\\", "/")
            handle.write(f"file '{escaped}'\n")

    output_path = os.path.join(output_dir, "timeline_verification_reel.mp4")
    result = subprocess.run(
        [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_path,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            output_path,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0 or not os.path.isfile(output_path):
        raise RuntimeError(f"Khong ghep duoc video kiem tra TIME: {result.stderr[-1200:]}")
    if log_fn:
        log_fn(f"Da tao video kiem tra rieng cho {len(timeline_events)} TIME range.")
    return output_path
