import os
import re
import unicodedata
import asyncio
import subprocess
import edge_tts
import traceback
import hashlib
import json
import shutil
from datetime import timedelta

# Accept both mm:ss (including videos longer than 99 minutes) and hh:mm:ss.
# Ordering the 3-part alternative first prevents a partial match on 01:39:47.
_TIME_VALUE_PATTERN = r'(?:\d+:\d{1,2}:\d{2}(?:\.\d{1,3})?|\d+:\d{2}(?:\.\d{1,3})?)'
TIMESTAMP_TOKEN_RE = re.compile(
    rf'\[(?:TIME=)?(?P<start>{_TIME_VALUE_PATTERN})'
    rf'(?:\s*-\s*(?P<end>{_TIME_VALUE_PATTERN}))?\]',
    flags=re.IGNORECASE,
)
VISUAL_BEAT_TOKEN_RE = re.compile(
    rf'\[BEAT=(?P<start>{_TIME_VALUE_PATTERN})\s*-\s*(?P<end>{_TIME_VALUE_PATTERN})\]',
    flags=re.IGNORECASE,
)


def format_timeline_rows(text):
    """Put every timestamp block on exactly one visible editor row."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    matches = list(TIMESTAMP_TOKEN_RE.finditer(text))
    if not matches:
        return re.sub(r'[ \t]+', ' ', text)

    rows = []
    preamble = text[:matches[0].start()].strip()
    if preamble:
        rows.append(re.sub(r'\s+', ' ', preamble))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = re.sub(r'\s+', ' ', text[match.end():end]).strip()
        row = match.group(0).upper().replace('[TIME=TIME=', '[TIME=')
        rows.append(f"{row} {body}".rstrip())
    return "\n".join(rows)


def remove_non_story_timeline_rows(script_text):
    """Remove obvious narration about logos, title cards and end credits."""
    non_story_terms = (
        "logo", "tên bộ phim", "tên phim", "đạo diễn", "diễn viên",
        "đoàn làm phim", "danh sách đoàn", "credit", "hãng sản xuất",
        "studio", "website", "www.", "màn hình đen", "màn hình trắng",
    )
    formatted = format_timeline_rows(script_text)
    rows = formatted.splitlines()
    kept = [row for row in rows if not any(term in row.casefold() for term in non_story_terms)]
    return "\n".join(kept).strip() or formatted


def _split_action_clauses(sentence):
    """Split a long narration sentence at explicit Vietnamese action changes."""
    sentence = sentence.strip()
    if len(sentence.split()) < 8:
        return [sentence]
    action_words = (
        r'rồi|sau đó|nhưng|đồng thời|bất ngờ|lập tức|tiếp tục|'
        r'lao|cuốn|chạy|nhảy|bắt|tấn công|đánh|kéo|ném|rơi|'
        r'xuất hiện|biến mất|quay|đuổi|trốn|thoát'
    )
    parts = re.split(rf',\s+(?=(?:{action_words})\b)', sentence, flags=re.IGNORECASE)
    expanded = []
    for part in parts:
        if len(part.split()) >= 7:
            subparts = re.split(rf'\s+(?=và\s+(?:{action_words})\b)', part, flags=re.IGNORECASE)
            expanded.extend(subparts)
        else:
            expanded.append(part)
    return [part.strip() for part in expanded if part.strip()]


def get_ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"
def get_audio_duration(filepath):
    ffmpeg_exe = get_ffmpeg_exe()
    dur_result = subprocess.run(
        [ffmpeg_exe, "-i", filepath, "-hide_banner"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    import re
    dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", dur_result.stderr)
    if dur_match:
        h, m, s_dur, cs = dur_match.groups()
        return int(h)*3600 + int(m)*60 + int(s_dur) + int(cs)/100.0
    return 0.0

def split_text_into_sentences(text):
    """
    Tach doan van thanh danh sach cac cau dua tren cac dau cau pho bien.
    Sau do can bang do dai cac cau de phu de dong deu hon.
    """
    import re
    # Keep timestamp rows intact. Every action derived from a row inherits the
    # same source-video anchor, instead of losing it after the first sentence.
    text = format_timeline_rows(text)
    
    # === LOC SACH BREAK TAGS — Xoa hoan toan moi the BREAK/break truoc khi xu ly ===
    text = re.sub(r'<\s*BREAK[^>]*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[\s*BREAK[^\]]*\]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<\s*break\s+time[^>]*/?>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[ \t]{2,}', ' ', text).strip()  # Keep timeline newlines
    
    # Quét thẻ [SFX=tên_file.mp3] và đổi thành định dạng nội bộ <SFX=tên_file.mp3>
    text = re.sub(r'\[SFX=([^\]]+)\]', r'<SFX=\1>', text, flags=re.IGNORECASE)
    
    # Chuan hoa the [SFX=...] sang <SFX=...>
    text = re.sub(r'\[SFX=([^\]]+)\]', r'<SFX=\1>', text, flags=re.IGNORECASE)
    
    # Quét thẻ [VOICE=...] và chuyển thành định dạng nội bộ <VOICE=...>
    text = re.sub(r'\[VOICE=([^\]]+)\]', r'<VOICE=\1>', text, flags=re.IGNORECASE)
    
    sentences = []
    sentence_end = re.compile(r'(?<=[.!?;])\s+')

    timeline_rows = text.splitlines() or [text]
    for row in timeline_rows:
        row = row.strip()
        if not row:
            continue
        timestamp_match = TIMESTAMP_TOKEN_RE.match(row)
        timestamp = timestamp_match.group(0) if timestamp_match else ""
        has_explicit_end = bool(timestamp_match and timestamp_match.group('end'))
        body = row[timestamp_match.end():].strip() if timestamp_match else row
        parts = re.split(r'(<SFX=[^>]+>|<VOICE=[^>]+>)', body)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if part.startswith('<SFX=') or part.startswith('<VOICE='):
                sentences.append(f"{timestamp} {part}".strip())
                continue
            if has_explicit_end:
                # Explicit ranges are locked shot decisions. Never guess an
                # internal visual boundary from verbs or commas; only an
                # explicit BEAT/SHOT decision may change the source image.
                sentences.append(f"{timestamp} {part}".strip())
                continue
            for raw_sentence in sentence_end.split(part):
                for action in _split_action_clauses(raw_sentence):
                    if len(action) > 3:
                        sentences.append(f"{timestamp} {action}".strip())
    
    # Action splitting above already bounds most chunks. Keeping the complete
    # timestamp-prefixed unit here avoids dropping the anchor from a second half.
    balanced = sentences
    
    # Only merge genuinely unusable fragments. Short actions such as "lao tới"
    # must stay separate so the scene can change with the narration action.
    final = []
    i = 0
    while i < len(balanced):
        current = balanced[i]
        if current.startswith('<SFX='):
            final.append(current)
            i += 1
            continue
            
        # Neu cau hien tai qua ngan va con cau tiep theo, gop lai
        current_without_time = TIMESTAMP_TOKEN_RE.sub('', current).strip()
        if len(current_without_time) < 4 and i + 1 < len(balanced) and not balanced[i + 1].startswith('<SFX='):
            merged = current + " " + balanced[i + 1]
            final.append(merged)
            i += 2
        # Neu cau hien tai qua ngan va co cau truoc, gop vao cau truoc
        elif len(current_without_time) < 4 and len(final) > 0 and not final[-1].startswith('<SFX='):
            final[-1] = final[-1] + " " + current
            i += 1
        else:
            final.append(current)
            i += 1
    
    return final

def _time_value_to_seconds(value):
    if not value:
        return None
    parts = value.split(':')
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def extract_time_range(text):
    """Return (start_seconds, end_seconds, clean_text) for a timeline row."""
    match = TIMESTAMP_TOKEN_RE.match(text.strip())
    if not match:
        return None, None, text
    start = _time_value_to_seconds(match.group('start'))
    end = _time_value_to_seconds(match.group('end'))
    clean = text.strip()[match.end():].strip()
    return start, end, clean

def sanitize_script_text(text):
    """Xóa toàn bộ thẻ timestamp dạng [TIME=mm:ss] hoặc [TIME=hh:mm:ss] và ký tự rác khỏi kịch bản."""
    # Xóa toàn bộ timestamp còn sót lại trong câu
    cleaned = TIMESTAMP_TOKEN_RE.sub('', text)
    cleaned = VISUAL_BEAT_TOKEN_RE.sub('', cleaned)
    # Xóa khoảng trắng thừa
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def parse_sentences_and_visual_beats(sentences):
    """Keep narration intact while extracting independently timed visual beats.

    The first visual beat uses the row's [TIME=start-end]. Additional actions
    may be introduced inline with [BEAT=start-end]. These markers are removed
    before TTS so prosody remains that of one complete sentence/paragraph.
    """
    clean_sentences = []
    starts = []
    ends = []
    visual_groups = []

    for sentence in sentences:
        start, end, body = extract_time_range(sentence)
        markers = list(VISUAL_BEAT_TOKEN_RE.finditer(body))
        parts = []
        cursor = 0
        beat_start, beat_end = start, end
        for marker in markers:
            text = sanitize_script_text(body[cursor:marker.start()])
            if text:
                marker_start = _time_value_to_seconds(marker.group("start"))
                # Gemini sometimes makes TIME cover the complete narration row
                # and then places a BEAT inside that range. Treat BEAT as the
                # internal scene boundary instead of rejecting the whole job.
                effective_end = beat_end
                if (
                    beat_start is not None and beat_end is not None
                    and marker_start is not None
                    and beat_start < marker_start < beat_end
                ):
                    effective_end = marker_start
                parts.append({"text": text, "start": beat_start, "end": effective_end})
            beat_start = _time_value_to_seconds(marker.group("start"))
            beat_end = _time_value_to_seconds(marker.group("end"))
            cursor = marker.end()
        tail = sanitize_script_text(body[cursor:])
        if tail:
            parts.append({"text": tail, "start": beat_start, "end": beat_end})

        clean = sanitize_script_text(body)
        if not parts and clean:
            parts = [{"text": clean, "start": start, "end": end}]
        clean_sentences.append(clean)
        starts.append(start)
        ends.append(end)
        visual_groups.append(parts)

    # Normalize every visual range into a strictly increasing timeline. Gemini
    # can emit partial overlaps, fully nested ranges, backwards ranges, or an
    # end equal to the start. Repair all of them here so the render endpoint and
    # worker use the exact same safe timeline.
    minimum_duration = 1.0
    previous_beat = None
    for row_index, group in enumerate(visual_groups):
        for beat_index, beat in enumerate(group):
            current_start = beat.get("start")
            current_end = beat.get("end")

            if current_start is None and previous_beat is not None:
                current_start = previous_beat.get("end")
                beat["start"] = current_start
            if current_start is not None and (current_end is None or current_end <= current_start):
                current_end = current_start + minimum_duration
                beat["end"] = current_end

            if previous_beat is not None and current_start is not None:
                previous_start = previous_beat.get("start")
                previous_end = previous_beat.get("end")
                if previous_end is not None and current_start < previous_end:
                    # Prefer preserving Gemini's current anchor by trimming the
                    # prior range when that still leaves a positive duration.
                    if previous_start is not None and current_start >= previous_start + minimum_duration:
                        previous_beat["end"] = current_start
                    else:
                        # A fully backwards/nested anchor cannot be fixed by
                        # trimming. Move it directly after the previous range.
                        current_start = previous_end
                        beat["start"] = current_start
                        if current_end is None or current_end <= current_start:
                            current_end = current_start + minimum_duration
                            beat["end"] = current_end
            previous_beat = beat

    # A later row may trim the final beat of an earlier row, so synchronize
    # row-level ranges only after the complete flattened timeline is repaired.
    for row_index, group in enumerate(visual_groups):
        if group:
            starts[row_index] = group[0].get("start")
            ends[row_index] = group[-1].get("end")

    return clean_sentences, starts, ends, visual_groups


def expand_visual_beats(sentences, durations, visual_groups, word_cues=None):
    """Create an audio-led visual EDL using real TTS word boundaries."""
    beat_texts, beat_durations, beat_starts, beat_ends = [], [], [], []
    word_cues = word_cues or [[] for _ in sentences]

    for index, sentence in enumerate(sentences):
        duration = max(0.05, float(durations[index]))
        beats = visual_groups[index] if index < len(visual_groups) else []
        if not beats:
            beats = [{"text": sentence, "start": None, "end": None}]
        cues = word_cues[index] if index < len(word_cues) else []

        if len(beats) == 1:
            boundaries = [0.0, duration]
        elif cues and len(cues) >= len(beats):
            # Prefer an exact lexical anchor: every inline BEAT is inserted
            # immediately before its trigger phrase.  Edge exposes the real
            # start time of that phrase, so the picture can lead speech by a
            # barely perceptible amount instead of being divided by word ratio.
            boundaries = _word_locked_beat_boundaries(beats, cues, duration)
            if boundaries is None:
                partitions = _cue_partitions(cues, [beat["text"] for beat in beats])
                cue_end = max(float(cues[-1]["end"]), 0.001)
                scale = duration / cue_end
                boundaries = [0.0]
                for _, cue_index in partitions[:-1]:
                    left = float(cues[max(0, cue_index - 1)]["end"])
                    right = float(cues[min(len(cues) - 1, cue_index)]["start"])
                    boundaries.append(min(duration, max(boundaries[-1], (left + right) * 0.5 * scale)))
                boundaries.append(duration)
        else:
            weights = [max(1, len(re.findall(r"\w+", beat["text"], flags=re.UNICODE))) for beat in beats]
            total = max(1, sum(weights))
            boundaries = [0.0]
            running = 0
            for weight in weights[:-1]:
                running += weight
                boundaries.append(duration * running / total)
            boundaries.append(duration)

        for beat_index, beat in enumerate(beats):
            beat_texts.append(beat["text"])
            beat_durations.append(max(0.05, boundaries[beat_index + 1] - boundaries[beat_index]))
            beat_starts.append(beat.get("start"))
            beat_ends.append(beat.get("end"))

    return beat_texts, beat_durations, beat_starts, beat_ends


def _alignment_tokens(value):
    """Normalize narration/cue text without losing Vietnamese word identity."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)


def _word_locked_beat_boundaries(beats, cues, duration, visual_lead=0.10):
    """Locate every beat's first spoken words in Edge word-boundary cues.

    Returns ``None`` when lexical alignment is incomplete so callers can use
    the legacy proportional fallback. Cue content may contain either one word
    or a short phrase, therefore tokens retain their owning cue index.
    """
    if len(beats) <= 1 or not cues:
        return [0.0, max(0.05, float(duration))]

    cue_tokens = []
    token_cue_indexes = []
    try:
        for cue_index, cue in enumerate(cues):
            for token in _alignment_tokens(cue.get("content", "")):
                cue_tokens.append(token)
                token_cue_indexes.append(cue_index)
    except (AttributeError, TypeError):
        return None
    if not cue_tokens:
        return None

    cue_end = max(float(cues[-1]["end"]), 0.001)
    scale = float(duration) / cue_end
    boundaries = [0.0]
    search_from = 0

    for beat in beats[1:]:
        beat_tokens = _alignment_tokens(beat.get("text", ""))
        if not beat_tokens:
            return None
        # Three leading words are normally unique enough while remaining
        # robust when Edge omits or combines a boundary.
        needle = beat_tokens[:min(3, len(beat_tokens))]
        found = None
        for token_index in range(search_from, len(cue_tokens) - len(needle) + 1):
            if cue_tokens[token_index:token_index + len(needle)] == needle:
                found = token_index
                break
        if found is None:
            return None

        cue_index = token_cue_indexes[found]
        trigger_start = float(cues[cue_index]["start"]) * scale
        boundary = trigger_start - max(0.0, float(visual_lead))
        # Keep every beat positive even when two triggers are very close.
        remaining = len(beats) - len(boundaries)
        maximum = float(duration) - 0.05 * remaining
        boundary = min(maximum, max(boundaries[-1] + 0.05, boundary))
        boundaries.append(boundary)
        search_from = found + len(needle)

    boundaries.append(float(duration))
    return boundaries

# 2. Tao giong doc TTS - SUBMAKER cho thoi gian chinh xac tung tu
# =====================================================================
MAX_TTS_RETRIES = 3
TTS_MAX_CONCURRENCY = 3
TTS_RETRY_DELAYS = (5.0, 15.0)
TTS_RECOVERY_DELAYS = (10.0, 30.0, 60.0, 120.0)
TTS_REQUEST_TIMEOUT = 60.0
TTS_REQUEST_START_INTERVAL = 2.0
TTS_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tts_cache")
TTS_SAFE_TEXT_CHARS = 500


def _split_long_tts_text(text, max_chars=TTS_SAFE_TEXT_CHARS):
    """Split oversized service requests at natural punctuation/word boundaries."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= max_chars:
        return [text] if text else []
    units = re.split(r"(?<=[.!?;,:])\s+", text)
    chunks = []
    current = ""
    for unit in units:
        words = unit.split()
        while words:
            room = max_chars - len(current) - (1 if current else 0)
            candidate = " ".join(words)
            if len(candidate) <= room:
                current = f"{current} {candidate}".strip()
                words = []
                continue
            if current:
                chunks.append(current)
                current = ""
                continue
            piece = []
            piece_len = 0
            while words and piece_len + len(words[0]) + (1 if piece else 0) <= max_chars:
                word = words.pop(0)
                piece.append(word)
                piece_len += len(word) + (1 if len(piece) > 1 else 0)
            if not piece:
                piece = [words.pop(0)[:max_chars]]
            chunks.append(" ".join(piece))
    if current:
        chunks.append(current)
    return chunks


async def _wait_before_tts_retry(attempt):
    """Short, bounded cooldown between attempts."""
    base_delay = TTS_RETRY_DELAYS[min(attempt, len(TTS_RETRY_DELAYS) - 1)]
    await asyncio.sleep(base_delay)


class _TtsRequestPacer:
    """Space starts globally while still allowing requests to overlap."""

    def __init__(self, interval=TTS_REQUEST_START_INTERVAL):
        self.interval = max(0.0, float(interval))
        self._lock = asyncio.Lock()
        self._next_start = 0.0

    async def wait_turn(self):
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            delay = max(0.0, self._next_start - now)
            if delay:
                await asyncio.sleep(delay)
            self._next_start = loop.time() + self.interval


async def generate_tts_chunk_with_timing(
    text, voice, rate, output_path, log_fn=None, chunk_index=None, max_retries=None,
    request_pacer=None,
):
    """Generate TTS audio va lay thoi gian chinh xac tung cau bang SubMaker."""
    from edge_tts import SubMaker
    import re
    
    # Khong dung the BREAK nua — chi truyen van ban thuan tuy
    audio_text = text
    
    retry_count = max(1, int(max_retries or MAX_TTS_RETRIES))
    for attempt in range(retry_count):
        try:
            if request_pacer is not None:
                await request_pacer.wait_turn()
            if log_fn:
                log_fn("INFO", f"TTS cau {chunk_index + 1}: bat dau lan thu {attempt + 1}/{retry_count} (voice={voice}, {len(text)} ky tu)")
            # boundary='word' de nhan SentenceBoundary events (Viet chi co SentenceBoundary)
            communicate = edge_tts.Communicate(
                audio_text,
                voice,
                rate=rate,
                boundary='word',
                connect_timeout=10,
                receive_timeout=60,
            )
            submaker = SubMaker()

            async def receive_audio():
                with open(output_path, "wb") as f:
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            f.write(chunk["data"])
                        elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                            submaker.feed(chunk)

            await asyncio.wait_for(receive_audio(), timeout=TTS_REQUEST_TIMEOUT)

            audio_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            minimum_size = max(4000, len(audio_text) * 40)
            has_valid_cues = bool(submaker.cues)
            if audio_size >= minimum_size and has_valid_cues:
                if log_fn:
                    log_fn("SUCCESS", f"TTS cau {chunk_index + 1}: thanh cong, {audio_size} bytes")
                return submaker

            if log_fn:
                log_fn(
                    "WARNING",
                    f"TTS cau {chunk_index + 1}: audio thieu ({audio_size} bytes); thu lai",
                )
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except OSError:
                pass
            
            if attempt < retry_count - 1:
                await _wait_before_tts_retry(attempt)
        except Exception as e:
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except OSError:
                pass
            if log_fn:
                log_fn(
                    "ERROR",
                    f"TTS cau {chunk_index + 1}, lan {attempt + 1}: {type(e).__name__}: {e}",
                    traceback.format_exc(),
                )
            if attempt < retry_count - 1:
                await _wait_before_tts_retry(attempt)
    return None

async def process_one_tts(
    i, s, voice, rate, chunk_path, sem, ffmpeg_exe, log_fn=None,
    request_pacer=None,
):
    # sfx processing
    if voice == "SFX":
        sfx_filename = s[5:-1]
        sfx_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "sfx", sfx_filename)
        if os.path.exists(sfx_path):
            subprocess.run([
                ffmpeg_exe, "-y", "-i", sfx_path,
                "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame",
                chunk_path
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return (i, "SFX", chunk_path, None)
        raise FileNotFoundError(f"Khong tim thay SFX: {sfx_filename}")
    
    # normal TTS
    async with sem:
        submaker = await generate_tts_chunk_with_timing(
            s, voice, rate, chunk_path, log_fn, i,
            request_pacer=request_pacer,
        )
        
        if submaker is None or not os.path.exists(chunk_path):
            if log_fn:
                log_fn("ERROR", f"TTS cau {i + 1}: that bai sau {MAX_TTS_RETRIES} lan; dung truoc khi render de tranh mat giong")
            raise RuntimeError(f"TTS cau {i + 1} that bai sau {MAX_TTS_RETRIES} lan thu.")
            
        return (i, "TTS", chunk_path, submaker)


def _build_tts_groups(sentences, sentence_voices, max_items=4, max_chars=500):
    """Batch consecutive lines of one voice to reduce Edge WebSocket churn."""
    groups = []
    current = []
    current_voice = None
    for index, text in enumerate(sentences):
        voice = sentence_voices[index]
        if voice == "VOICE_TAG":
            continue
        if voice == "SFX":
            if current:
                groups.append((current_voice, current))
                current = []
            groups.append(("SFX", [(index, text)]))
            current_voice = None
            continue
        would_be = sum(len(item[1]) for item in current) + len(text) + len(current)
        if current and (
            voice != current_voice or len(current) >= max_items
            or would_be > max_chars
        ):
            groups.append((current_voice, current))
            current = []
        current_voice = voice
        current.append((index, text))
    if current:
        groups.append((current_voice, current))
    return groups


def _cue_partitions(cues, texts):
    """Map word-boundary cues back to narration beats by cumulative word count."""
    if not cues or len(texts) <= 1:
        return [(0, len(cues))]
    weights = [max(1, len(re.findall(r"\w+", text, flags=re.UNICODE))) for text in texts]
    total_weight = sum(weights)
    partitions = []
    cue_start = 0
    cumulative_weight = 0
    for index, weight in enumerate(weights):
        cumulative_weight += weight
        if index == len(weights) - 1:
            cue_end = len(cues)
        else:
            cue_end = round(len(cues) * cumulative_weight / total_weight)
            # WordBoundary can occasionally omit cues. Empty partitions are
            # valid metadata; they must not manufacture an out-of-range index.
            cue_end = min(len(cues), max(cue_start, cue_end))
        partitions.append((cue_start, cue_end))
        cue_start = cue_end
    return partitions


def _tts_cache_paths(text, voice, rate):
    identity = f"v2\0{voice}\0{rate}\0{text}".encode("utf-8", errors="replace")
    cache_key = hashlib.sha256(identity).hexdigest()
    return (
        os.path.join(TTS_CACHE_DIR, f"{cache_key}.mp3"),
        os.path.join(TTS_CACHE_DIR, f"{cache_key}.json"),
    )


def _load_cached_tts(text, voice, rate, raw_path):
    """Restore audio and word cues from the persistent cache."""
    from edge_tts import SubMaker
    from edge_tts.submaker import Subtitle

    audio_cache, cue_cache = _tts_cache_paths(text, voice, rate)
    minimum_size = max(4000, len(text) * 40)
    if (
        not os.path.isfile(audio_cache)
        or os.path.getsize(audio_cache) < minimum_size
        or not os.path.isfile(cue_cache)
    ):
        return None
    try:
        with open(cue_cache, "r", encoding="utf-8") as cue_file:
            cue_items = json.load(cue_file)
        submaker = SubMaker()
        for cue in cue_items:
            submaker.cues.append(Subtitle(
                index=None,
                start=timedelta(seconds=float(cue["start"])),
                end=timedelta(seconds=float(cue["end"])),
                content=str(cue["content"]),
            ))
        if not submaker.cues:
            return None
        cached_duration = get_audio_duration(audio_cache)
        if cached_duration <= 0.05 or submaker.cues[-1].end.total_seconds() > cached_duration + 0.75:
            return None
        shutil.copy2(audio_cache, raw_path)
        return submaker
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _save_cached_tts(text, voice, rate, raw_path, submaker):
    """Atomically cache a successful Edge TTS response for later renders."""
    if not os.path.isfile(raw_path) or os.path.getsize(raw_path) <= 1000:
        return
    os.makedirs(TTS_CACHE_DIR, exist_ok=True)
    audio_cache, cue_cache = _tts_cache_paths(text, voice, rate)
    audio_temp = audio_cache + ".tmp"
    cue_temp = cue_cache + ".tmp"
    cue_items = [
        {
            "start": cue.start.total_seconds(),
            "end": cue.end.total_seconds(),
            "content": cue.content,
        }
        for cue in (submaker.cues or [])
    ]
    try:
        shutil.copy2(raw_path, audio_temp)
        with open(cue_temp, "w", encoding="utf-8", newline="\n") as cue_file:
            json.dump(cue_items, cue_file, ensure_ascii=False)
        os.replace(audio_temp, audio_cache)
        os.replace(cue_temp, cue_cache)
    except OSError:
        for temp_path in (audio_temp, cue_temp):
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass


def _invalidate_tts_group_cache(items, voice, rate):
    """Remove only the failed group's cache; successful groups stay immutable."""
    if voice == "SFX":
        return False
    group_text = " ".join(text for _, text in items)
    removed = False
    for cache_path in _tts_cache_paths(group_text, voice, rate):
        try:
            if os.path.exists(cache_path):
                os.remove(cache_path)
                removed = True
        except OSError:
            pass
    return removed


def _exception_details(exc):
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()


async def _recover_failed_tts_groups(
    failed_groups, rate, temp_dir, ffmpeg_exe, log_fn=None,
    request_pacer=None,
):
    """Retry only failed groups with cooldown, cache repair and circuit breaker."""
    pending = list(failed_groups)
    recovered = []
    last_errors = {group_index: exc for group_index, _, exc in pending}

    for recovery_round, cooldown in enumerate(TTS_RECOVERY_DELAYS, start=1):
        if not pending:
            break
        if log_fn:
            log_fn(
                "WARNING",
                f"TTS recovery vong {recovery_round}/{len(TTS_RECOVERY_DELAYS)}: "
                f"con {len(pending)} cum loi; giam xuong 1 ket noi va cho {int(cooldown)} giay.",
            )
        await asyncio.sleep(cooldown)
        next_pending = []

        for group_index, (voice, items), _ in pending:
            repaired_cache = _invalidate_tts_group_cache(items, voice, rate)
            if repaired_cache and log_fn:
                log_fn("WARNING", f"TTS cum {group_index + 1}: cache loi da duoc loai bo rieng")
            try:
                # Never fall back to one API request per event: it produces a
                # service-padding gap at every cut and can exhaust the quota.
                result = await process_tts_group(
                    group_index,
                    items,
                    voice,
                    rate,
                    temp_dir,
                    asyncio.Semaphore(1),
                    ffmpeg_exe,
                    log_fn,
                    max_retries=MAX_TTS_RETRIES,
                    request_pacer=request_pacer,
                )
                recovered.append(result)
            except Exception as exc:
                last_errors[group_index] = exc
                next_pending.append((group_index, (voice, items), exc))
                if log_fn:
                    sentence_numbers = ", ".join(str(index + 1) for index, _ in items)
                    log_fn(
                        "ERROR",
                        f"TTS recovery cum {group_index + 1} (cau {sentence_numbers}) "
                        f"vong {recovery_round}: {type(exc).__name__}: {exc}",
                        _exception_details(exc),
                    )
        pending = next_pending

    # Last resort: isolate every failed sentence, split it into short requests,
    # then join those pieces back into one audio item. Successful groups above
    # remain untouched and continue to come from memory/cache.
    if pending:
        split_pending = []
        for group_index, group, previous_error in pending:
            try:
                result = await _recover_tts_group_as_short_pieces(
                    group_index, group, rate, temp_dir, ffmpeg_exe, log_fn,
                    request_pacer=request_pacer,
                )
                recovered.append(result)
                if log_fn:
                    log_fn(
                        "SUCCESS",
                        f"TTS cum {group_index + 1}: phuc hoi bang cach chia cau thanh doan ngan.",
                    )
            except Exception as exc:
                last_errors[group_index] = exc
                split_pending.append((group_index, group, exc))
                if log_fn:
                    log_fn(
                        "ERROR",
                        f"TTS cum {group_index + 1}: chia cau van that bai: "
                        f"{type(exc).__name__}: {exc}",
                        _exception_details(exc),
                    )
        pending = split_pending

    return recovered, pending, last_errors


async def _generate_oversized_tts(
    text, voice, rate, raw_path, temp_dir, group_index, log_fn=None,
    max_retries=None, max_chars=TTS_SAFE_TEXT_CHARS, request_pacer=None,
):
    """Synthesize a long narration as safe requests and rebuild one cue timeline."""
    from edge_tts import SubMaker
    from edge_tts.submaker import Subtitle

    pieces = _split_long_tts_text(text, max_chars=max_chars)
    if len(pieces) <= 1:
        return None
    if log_fn:
        log_fn(
            "WARNING",
            f"TTS cau {group_index + 1} dai {len(text)} ky tu; tu chia thanh {len(pieces)} doan an toan.",
        )

    ffmpeg_exe = get_ffmpeg_exe()
    piece_paths = []
    combined = SubMaker()
    offset = 0.0
    for piece_index, piece in enumerate(pieces):
        piece_path = os.path.join(temp_dir, f"tts_long_{group_index:04d}_{piece_index:02d}.mp3")
        piece_submaker = await generate_tts_chunk_with_timing(
            piece, voice, rate, piece_path, log_fn, group_index,
            max_retries=max_retries,
            request_pacer=request_pacer,
        )
        if piece_submaker is None or not os.path.isfile(piece_path):
            raise RuntimeError(
                f"TTS cau {group_index + 1}: doan nho {piece_index + 1}/{len(pieces)} that bai."
            )
        piece_duration = get_audio_duration(piece_path)
        if piece_duration <= 0.05:
            raise RuntimeError(f"TTS cau {group_index + 1}: doan nho co audio khong hop le.")
        for cue in piece_submaker.cues or []:
            combined.cues.append(Subtitle(
                index=None,
                start=timedelta(seconds=offset + cue.start.total_seconds()),
                end=timedelta(seconds=offset + cue.end.total_seconds()),
                content=cue.content,
            ))
        piece_paths.append(piece_path)
        offset += piece_duration

    concat_path = os.path.join(temp_dir, f"tts_long_{group_index:04d}.txt")
    with open(concat_path, "w", encoding="utf-8", newline="\n") as concat_file:
        for piece_path in piece_paths:
            safe_path = os.path.abspath(piece_path).replace(chr(92), "/")
            concat_file.write(f"file '{safe_path}'\n")
    result = subprocess.run([
        ffmpeg_exe, "-y", "-f", "concat", "-safe", "0", "-i", concat_path,
        "-c:a", "libmp3lame", "-q:a", "3", raw_path,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0 or get_audio_duration(raw_path) <= 0.05:
        raise RuntimeError(f"TTS cau {group_index + 1}: khong ghep duoc cac doan nho.")
    return combined


async def _recover_tts_group_as_short_pieces(
    group_index, group, rate, temp_dir, ffmpeg_exe, log_fn=None,
    request_pacer=None,
):
    """Recover a failed group by synthesizing short pieces per original sentence."""
    voice, items = group
    if voice == "SFX":
        index, text = items[0]
        chunk_path = os.path.join(temp_dir, f"chunk_{index:03d}.mp3")
        result = await process_one_tts(
            index, text, voice, rate, chunk_path, asyncio.Semaphore(1),
            ffmpeg_exe, log_fn, request_pacer=request_pacer,
        )
        return group_index, [result], None

    item_results = []
    for index, text in items:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        if not normalized:
            raise RuntimeError(f"TTS cau {index + 1}: noi dung rong.")

        # Force at least two requests for an ordinary failed sentence whenever
        # word boundaries permit it; long sentences are capped near 100 chars.
        words = normalized.split()
        if len(words) >= 4:
            half_length = max(24, len(normalized) // 2)
            fallback_chars = min(100, half_length)
        else:
            fallback_chars = 100

        output_path = os.path.join(temp_dir, f"chunk_{index:03d}_split.mp3")
        pieces = _split_long_tts_text(normalized, max_chars=fallback_chars)
        if len(pieces) > 1:
            submaker = await _generate_oversized_tts(
                normalized,
                voice,
                rate,
                output_path,
                temp_dir,
                index,
                log_fn,
                max_retries=MAX_TTS_RETRIES,
                max_chars=fallback_chars,
                request_pacer=request_pacer,
            )
        else:
            submaker = await generate_tts_chunk_with_timing(
                normalized,
                voice,
                rate,
                output_path,
                log_fn,
                index,
                max_retries=MAX_TTS_RETRIES,
                request_pacer=request_pacer,
            )
        if submaker is None or not os.path.isfile(output_path):
            raise RuntimeError(f"TTS cau {index + 1}: cac doan ngan van that bai.")
        duration = get_audio_duration(output_path)
        if duration <= 0.05:
            raise RuntimeError(f"TTS cau {index + 1}: audio ghep khong hop le.")
        item_results.append((index, "TTS", output_path, submaker, duration))

    return group_index, item_results, None


async def process_tts_group(
    group_index, items, voice, rate, temp_dir, sem, ffmpeg_exe, log_fn=None,
    max_retries=None, request_pacer=None,
):
    """Generate one continuous TTS take and expose sentence-sized timing chunks."""
    if voice == "SFX":
        index, text = items[0]
        chunk_path = os.path.join(temp_dir, f"chunk_{index:03d}.mp3")
        result = await process_one_tts(
            index, text, voice, rate, chunk_path, sem, ffmpeg_exe, log_fn,
            request_pacer=request_pacer,
        )
        return group_index, [result], None

    from edge_tts import SubMaker
    from edge_tts.submaker import Subtitle

    group_text = " ".join(text for _, text in items)
    raw_path = os.path.join(temp_dir, f"tts_group_{group_index:03d}_raw.mp3")
    group_path = os.path.join(temp_dir, f"tts_group_{group_index:03d}.mp3")
    submaker = _load_cached_tts(group_text, voice, rate, raw_path)
    if submaker is not None:
        if log_fn:
            log_fn("SUCCESS", f"TTS cum {group_index + 1}: dung cache, khong goi lai Microsoft")
    elif len(group_text) > TTS_SAFE_TEXT_CHARS:
        async with sem:
            submaker = await _generate_oversized_tts(
                group_text, voice, rate, raw_path, temp_dir, group_index, log_fn,
                max_retries=max_retries,
                request_pacer=request_pacer,
            )
        if submaker is not None and os.path.exists(raw_path):
            _save_cached_tts(group_text, voice, rate, raw_path, submaker)
    else:
        async with sem:
            submaker = await generate_tts_chunk_with_timing(
                group_text, voice, rate, raw_path, log_fn, items[0][0],
                max_retries=max_retries, request_pacer=request_pacer,
            )
        if submaker is not None and os.path.exists(raw_path):
            _save_cached_tts(group_text, voice, rate, raw_path, submaker)

    if submaker is None or not os.path.exists(raw_path):
        failed_sentences = ", ".join(str(index + 1) for index, _ in items)
        if log_fn:
            log_fn(
                "ERROR",
                f"TTS cum {group_index + 1} (cau {failed_sentences}) chua tao duoc audio",
            )
        raise RuntimeError(f"Khong tao duoc giong doc cho cau {failed_sentences}.")

    raw_duration = get_audio_duration(raw_path)
    cue_end = submaker.cues[-1].end.total_seconds() if submaker.cues else raw_duration
    # Protect quiet Vietnamese final consonants, while removing Edge's variable
    # service padding. No amplitude-based silence trimming is used.
    content_end = min(raw_duration, cue_end + 0.18)
    trim_result = subprocess.run([
        ffmpeg_exe, "-y", "-i", raw_path, "-af", f"atrim=0:{content_end:.6f},asetpts=PTS-STARTPTS",
        "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame", "-q:a", "3", group_path,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if trim_result.returncode != 0:
        group_path = raw_path
        content_end = raw_duration
    encoded_group_duration = get_audio_duration(group_path)
    if encoded_group_duration <= 0.05:
        raise RuntimeError(f"TTS group {group_index} co master audio khong hop le")
    duration_scale = encoded_group_duration / max(content_end, 0.001)

    cues = list(submaker.cues or [])
    item_texts = [text for _, text in items]
    partitions = _cue_partitions(cues, item_texts)
    item_weights = [max(1, len(re.findall(r"\w+", text, flags=re.UNICODE))) for text in item_texts]
    total_weight = max(1, sum(item_weights))
    boundaries = [0.0]
    cumulative_weight = 0
    for item_position, (_, cue_index) in enumerate(partitions[:-1]):
        cumulative_weight += item_weights[item_position]
        proportional = content_end * cumulative_weight / total_weight
        if cues and 0 < cue_index < len(cues):
            left = cues[cue_index - 1].end.total_seconds()
            right = cues[cue_index].start.total_seconds()
            candidate = (left + right) / 2.0
        else:
            candidate = proportional
        minimum = boundaries[-1] + 0.06
        remaining = len(items) - item_position - 1
        maximum = content_end - 0.06 * remaining
        boundaries.append(min(maximum, max(minimum, candidate)))
    boundaries.append(content_end)

    results = []
    for item_pos, (index, _) in enumerate(items):
        start_time, end_time = boundaries[item_pos], boundaries[item_pos + 1]
        chunk_path = os.path.join(temp_dir, f"chunk_{index:03d}.mp3")
        split_result = subprocess.run([
            ffmpeg_exe, "-y", "-i", group_path,
            "-af", f"atrim={start_time:.6f}:{end_time:.6f},asetpts=PTS-STARTPTS",
            "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame", "-q:a", "3", chunk_path,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if split_result.returncode != 0:
            raise RuntimeError(f"Khong tach duoc TTS group {group_index}, cau {index + 1}")
        actual_chunk_duration = get_audio_duration(chunk_path)
        if actual_chunk_duration <= 0.05:
            raise RuntimeError(f"TTS group {group_index}, cau {index + 1} co audio khong hop le")

        local_submaker = SubMaker()
        cue_from, cue_to = partitions[item_pos]
        for cue in cues[cue_from:cue_to]:
            local_start = max(0.0, cue.start.total_seconds() - start_time)
            local_end = max(local_start, cue.end.total_seconds() - start_time)
            local_submaker.cues.append(Subtitle(
                index=None,
                start=timedelta(seconds=local_start),
                end=timedelta(seconds=local_end),
                content=cue.content,
            ))
        # Visual durations must add up to the grouped master track that will be
        # muxed. Using each re-encoded chunk's MP3 padding would accumulate drift.
        timeline_duration = (end_time - start_time) * duration_scale
        results.append((index, "TTS", chunk_path, local_submaker, timeline_duration))
    if log_fn:
        log_fn("SUCCESS", f"TTS cum {group_index + 1}: {len(items)} y/cau trong mot luot doc lien mach")
    return group_index, results, group_path

def generate_all_tts(
    sentences, default_voice, rate, temp_dir, options=None, log_fn=None,
    return_word_cues=False,
):
    """
    Tao TTS tung cau va lay thong tin Subtitles (SubMaker).
    Phien ban DA LUONG (Concurrent Async) de tang toc do x10 cho video dai.
    """
    os.makedirs(temp_dir, exist_ok=True)
    ffmpeg_exe = get_ffmpeg_exe()
    options = options or {}
    enable_karaoke_sub = options.get("enable_karaoke_sub", True)
    
    # 1. Tinh toan giong noi cho tung cau
    sentence_voices = []
    curr_v = default_voice
    for s in sentences:
        if s.startswith('<VOICE=') and s.endswith('>'):
            curr_v = s[7:-1].strip()
            sentence_voices.append("VOICE_TAG")
        elif s.startswith('<SFX=') and s.endswith('>'):
            sentence_voices.append("SFX")
        else:
            sentence_voices.append(curr_v)

    # 2. Khoi tao loop va Semaphore
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    sem = asyncio.Semaphore(TTS_MAX_CONCURRENCY)
    request_pacer = _TtsRequestPacer(TTS_REQUEST_START_INTERVAL)
    
    tts_groups = _build_tts_groups(sentences, sentence_voices)
    tasks = [
        process_tts_group(
            group_index, items, voice, rate, temp_dir, sem, ffmpeg_exe, log_fn,
            request_pacer=request_pacer,
        )
        for group_index, (voice, items) in enumerate(tts_groups)
    ]
        
    print(f"[INFO] Bat dau tai TTS {len(tasks)} cum (toi da {TTS_MAX_CONCURRENCY} ket noi song song)...")
    if log_fn:
        log_fn(
            "INFO",
            f"TTS: {len(sentences)} y/cau, {len(tasks)} cum (toi da 4 cau/cum), "
            f"toi da {TTS_MAX_CONCURRENCY} ket noi, bat dau cach nhau "
            f"{TTS_REQUEST_START_INTERVAL:g} giay, retry {MAX_TTS_RETRIES} lan",
        )
    try:
        first_outcomes = loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        group_results = []
        failed_groups = []
        for group_index, outcome in enumerate(first_outcomes):
            if isinstance(outcome, Exception):
                failed_groups.append((group_index, tts_groups[group_index], outcome))
                if log_fn:
                    voice, items = tts_groups[group_index]
                    sentence_numbers = ", ".join(str(i + 1) for i, _ in items)
                    log_fn(
                        "ERROR",
                        f"TTS cum {group_index + 1} (cau {sentence_numbers}) loi ban dau: "
                        f"{type(outcome).__name__}: {outcome}",
                        _exception_details(outcome),
                    )
            else:
                group_results.append(outcome)

        if failed_groups:
            if log_fn:
                log_fn(
                    "WARNING",
                    f"TTS batch co {len(failed_groups)} cum loi. Bat dau Recovery Queue; "
                    "cac cum da thanh cong duoc giu nguyen.",
                )
            recovered_groups, remaining_groups, last_errors = loop.run_until_complete(
                _recover_failed_tts_groups(
                    failed_groups, rate, temp_dir, ffmpeg_exe, log_fn,
                    request_pacer=request_pacer,
                )
            )
            group_results.extend(recovered_groups)

            details = "; ".join(
                f"cum {group_index + 1} (cau {', '.join(str(i + 1) for i, _ in items)})"
                for group_index, (_, items), _ in remaining_groups
            )
            if not remaining_groups:
                if log_fn:
                    log_fn(
                        "SUCCESS",
                        f"TTS Recovery Queue da phuc hoi du {len(recovered_groups)} cum; tiep tuc render.",
                    )
            else:
                final_details = "\n\n".join(
                    f"Cum {group_index + 1}:\n{_exception_details(last_errors[group_index])}"
                    for group_index, _, _ in remaining_groups
                )
                if log_fn:
                    log_fn(
                        "ERROR",
                        f"TTS Recovery Queue van con loi sau {len(TTS_RECOVERY_DELAYS)} vong: {details}.",
                        final_details,
                    )
                raise RuntimeError(
                    f"Khong phuc hoi duoc TTS: {details}. "
                    "Cac cum khac da duoc giu trong cache; co the tiep tuc lai sau."
                )
    except Exception as exc:
        if log_fn:
            log_fn("ERROR", f"TTS batch dung do {type(exc).__name__}: {exc}", traceback.format_exc())
        raise
    finally:
        loop.close()
    print(f"[INFO] Hoan thanh tai TTS da luong.")
    
    # 3. Sap xep ket qua va tinh toan time tich luy (Cumulative time)
    group_results.sort(key=lambda x: x[0])
    results = []
    continuous_group_paths = []
    can_use_continuous_master = True
    for _, item_results, group_path in group_results:
        results.extend(item_results)
        if group_path:
            continuous_group_paths.append(group_path)
        else:
            can_use_continuous_master = False
    results.sort(key=lambda x: x[0])

    # Keep the exact grouped takes for the master track. Sentence chunks remain
    # available for visual timing and subtitle generation.
    if can_use_continuous_master and continuous_group_paths:
        concat_path = os.path.join(temp_dir, "continuous_tts_concat.txt")
        with open(concat_path, "w", encoding="utf-8") as concat_file:
            for path in continuous_group_paths:
                absolute_path = os.path.abspath(path).replace(chr(92), "/")
                concat_file.write(f"file '{absolute_path}'\n")
        continuous_master = os.path.join(temp_dir, "continuous_master_tts.mp3")
        concat_result = subprocess.run([
            ffmpeg_exe, "-y", "-f", "concat", "-safe", "0", "-i", concat_path,
            "-c:a", "libmp3lame", "-q:a", "3", continuous_master,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if concat_result.returncode == 0 and os.path.getsize(continuous_master) > 1000:
            master_duration = get_audio_duration(continuous_master)
            planned_duration = sum(float(item[4]) for item in results if len(item) >= 5)
            if master_duration > 0.05 and planned_duration > 0.05:
                master_scale = master_duration / planned_duration
                results = [
                    (*item[:4], float(item[4]) * master_scale) if len(item) >= 5 else item
                    for item in results
                ]
            with open(os.path.join(temp_dir, "_USE_MASTER_TTS_"), "w", encoding="utf-8") as marker:
                marker.write(os.path.abspath(continuous_master))
    
    audio_paths = []
    durations = []
    visual_word_cues = []
    srt_lines = []
    cumulative_time = 0.0
    
    res_dict = {r[0]: r for r in results}
    
    for i, s in enumerate(sentences):
        if sentence_voices[i] == "VOICE_TAG":
            continue
            
        if i not in res_dict:
            continue
            
        result_item = res_dict[i]
        _, r_type, chunk_path, submaker = result_item[:4]
        
        if r_type == "MISSING_SFX":
            continue
            
        if r_type == "SFX":
            sfx_dur = get_audio_duration(chunk_path)
            audio_paths.append(chunk_path)
            durations.append(sfx_dur)
            visual_word_cues.append([])
            srt_lines.append((cumulative_time, cumulative_time + sfx_dur, ""))
            cumulative_time += sfx_dur
            continue
            
        if r_type == "EMPTY_TTS":
            audio_paths.append(chunk_path)
            durations.append(3.0)
            visual_word_cues.append([])
            cumulative_time += 3.0
            continue
            
        # Truong hop TTS binh thuong
        if len(result_item) >= 5:
            chunk_dur = float(result_item[4])
        elif submaker and submaker.cues:
            chunk_dur = submaker.cues[-1].end.total_seconds()
        else:
            chunk_dur = max(2.0, len(s) * 0.07)
            
        audio_paths.append(chunk_path)
        durations.append(chunk_dur)
        visual_word_cues.append([
            {
                "start": cue.start.total_seconds(),
                "end": cue.end.total_seconds(),
                "content": cue.content,
            }
            for cue in (submaker.cues if submaker and submaker.cues else [])
        ])
        
        # Tao phu de tung tu (Karaoke / Hormozi)
        if submaker and submaker.cues:
            if enable_karaoke_sub:
                for idx, cue in enumerate(submaker.cues):
                    word_start = cumulative_time + cue.start.total_seconds()
                    if idx < len(submaker.cues) - 1:
                        word_end = cumulative_time + submaker.cues[idx+1].start.total_seconds()
                    else:
                        word_end = cumulative_time + cue.end.total_seconds()
                        
                    words_ass = []
                    for j, c in enumerate(submaker.cues):
                        if idx == j:
                            words_ass.append(f"{{\\c&H0000FFFF&\\fscx118\\fscy118}}{c.content}{{\\r}}")
                        else:
                            words_ass.append(c.content)
                    full_text = " ".join(words_ass)
                    srt_lines.append((word_start, word_end, full_text))
            else:
                word_start = cumulative_time + submaker.cues[0].start.total_seconds()
                word_end = cumulative_time + submaker.cues[-1].end.total_seconds()
                full_text = " ".join([c.content for c in submaker.cues])
                srt_lines.append((word_start, word_end, full_text))
                
        cumulative_time += chunk_dur
        
    # Ghi file .ass chuan
    ass_path = os.path.join(temp_dir, "submaker_subtitles.ass")
    with open(ass_path, 'w', encoding='utf-8') as f:
        f.write("[Script Info]\n")
        f.write("ScriptType: v4.00+\n")
        f.write("PlayResX: 1920\n")
        f.write("PlayResY: 1080\n")
        f.write("WrapStyle: 1\n\n")
        
        f.write("[V4+ Styles]\n")
        f.write("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
        f.write("Style: TikTokStyle,Arial,40,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,50,50,90,1\n\n")
        
        f.write("[Events]\n")
        f.write("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        
        for (start, end, text) in srt_lines:
            if text == "": continue
            f.write(f"Dialogue: 0,{format_timestamp_ass(start)},{format_timestamp_ass(end)},TikTokStyle,,0,0,0,,{text}\n")
            
    if return_word_cues:
        return audio_paths, durations, visual_word_cues
    return audio_paths, durations

def format_timestamp_ass(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

def create_srt(sentences, durations, filepath):
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write("[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 1\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: TikTokStyle,Arial,40,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,50,50,90,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        current_time = 0.0
        for i, text in enumerate(sentences):
            start_str = format_timestamp_ass(current_time)
            end_str = format_timestamp_ass(current_time + durations[i])
            f.write(f"Dialogue: 0,{start_str},{end_str},TikTokStyle,,0,0,0,,{text}\n")
            current_time += durations[i]
