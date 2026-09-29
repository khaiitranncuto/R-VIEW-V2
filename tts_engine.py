import os
import re
import asyncio
import subprocess
import edge_tts
import traceback

_TIME_VALUE_PATTERN = r'(?:(?:\d+):)?\d{1,2}:\d{2}'
TIMESTAMP_TOKEN_RE = re.compile(
    rf'\[(?:TIME=)?(?P<start>{_TIME_VALUE_PATTERN})'
    rf'(?:\s*-\s*(?P<end>{_TIME_VALUE_PATTERN}))?\]',
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
    return 1.0

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
                # A START-END row is an atomic narration/clip contract. Do not
                # duplicate its range by splitting it into several TTS chunks.
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

def extract_timestamp(text):
    """
    Tim va trich xuat timestamp (vi du: [12:30] hoac [TIME=01:12:30]) tu dau cau.
    Tra ve (thoi_gian_giay, cau_da_xoa_timestamp).
    """
    start, _, clean = extract_time_range(text)
    return start, clean


def _time_value_to_seconds(value):
    if not value:
        return None
    parts = [int(part) for part in value.split(':')]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


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
    # Xóa khoảng trắng thừa
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def parse_sentences_and_timestamps(sentences):
    """Xoa timestamp khoi cau de TTS/Sub khong bi dinh, nhung giu lai de sync video."""
    clean_sentences = []
    timestamps = []
    for s in sentences:
        t, clean = extract_timestamp(s)
        clean = sanitize_script_text(clean)
        clean_sentences.append(clean)
        timestamps.append(t)
    return clean_sentences, timestamps


def parse_sentences_and_time_ranges(sentences):
    """Parse start/end anchors while keeping backward compatibility."""
    clean_sentences = []
    starts = []
    ends = []
    for sentence in sentences:
        start, end, clean = extract_time_range(sentence)
        clean_sentences.append(sanitize_script_text(clean))
        starts.append(start)
        ends.append(end)
    return clean_sentences, starts, ends

# 2. Tao giong doc TTS - SUBMAKER cho thoi gian chinh xac tung tu
# =====================================================================
MAX_TTS_RETRIES = 3

async def generate_tts_chunk_with_timing(text, voice, rate, output_path, log_fn=None, chunk_index=None):
    """Generate TTS audio va lay thoi gian chinh xac tung cau bang SubMaker."""
    from edge_tts import SubMaker
    import re
    
    # Khong dung the BREAK nua — chi truyen van ban thuan tuy
    audio_text = text
    
    for attempt in range(MAX_TTS_RETRIES):
        try:
            if log_fn:
                log_fn("INFO", f"TTS cau {chunk_index + 1}: bat dau lan thu {attempt + 1}/{MAX_TTS_RETRIES} (voice={voice}, {len(text)} ky tu)")
            # boundary='word' de nhan SentenceBoundary events (Viet chi co SentenceBoundary)
            communicate = edge_tts.Communicate(audio_text, voice, rate=rate, boundary='word')
            submaker = SubMaker()
            
            with open(output_path, "wb") as f:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                        submaker.feed(chunk)
            
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                if log_fn:
                    log_fn("SUCCESS", f"TTS cau {chunk_index + 1}: thanh cong, {os.path.getsize(output_path)} bytes")
                return submaker
            
            if attempt < MAX_TTS_RETRIES - 1:
                await asyncio.sleep(2.0)
        except Exception as e:
            if log_fn:
                log_fn(
                    "ERROR",
                    f"TTS cau {chunk_index + 1}, lan {attempt + 1}: {type(e).__name__}: {e}",
                    traceback.format_exc(),
                )
            if attempt < MAX_TTS_RETRIES - 1:
                await asyncio.sleep(2.0)
    return None

def split_text_for_ass(text, max_len=40):
    if len(text) <= max_len:
        return text
    words = text.split(" ")
    lines = []
    current_line = []
    current_len = 0
    for w in words:
        if current_len + len(w) + (1 if current_len > 0 else 0) <= max_len:
            current_line.append(w)
            current_len += len(w) + (1 if current_len > 0 else 0)
        else:
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [w]
            current_len = len(w)
    if current_line:
        lines.append(" ".join(current_line))
    return "\\N".join(lines)

async def process_one_tts(i, s, voice, rate, chunk_path, sem, ffmpeg_exe, log_fn=None):
    import random
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
        return (i, "MISSING_SFX", None, None)
    
    # normal TTS
    async with sem:
        await asyncio.sleep(random.uniform(0.1, 1.0)) # Stagger requests
        submaker = await generate_tts_chunk_with_timing(s, voice, rate, chunk_path, log_fn, i)
        
        if submaker is None or not os.path.exists(chunk_path):
            if log_fn:
                log_fn("WARNING", f"TTS cau {i + 1}: het retry, tao 3 giay im lang de tiep tuc chan doan")
            subprocess.run([
                ffmpeg_exe, "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                "-t", "3.0", "-c:a", "libmp3lame", "-q:a", "9", chunk_path
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return (i, "EMPTY_TTS", chunk_path, None)
            
        return (i, "TTS", chunk_path, submaker)

def generate_all_tts(sentences, default_voice, rate, temp_dir, options=None, log_fn=None):
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
    sem = asyncio.Semaphore(3) # 3 luong cung luc de tranh bi Microsoft block IP
    
    tasks = []
    for i, s in enumerate(sentences):
        v = sentence_voices[i]
        if v == "VOICE_TAG":
            continue
        chunk_path = os.path.join(temp_dir, f"chunk_{i:03d}.mp3")
        tasks.append(process_one_tts(i, s, v, rate, chunk_path, sem, ffmpeg_exe, log_fn))
        
    print(f"[INFO] Bat dau tai TTS {len(tasks)} file song song (Semaphore 3)...")
    if log_fn:
        log_fn("INFO", f"TTS: chuan bi {len(tasks)} cau, toi da 3 ket noi song song")
    try:
        results = loop.run_until_complete(asyncio.gather(*tasks))
    except Exception as exc:
        if log_fn:
            log_fn("ERROR", f"TTS batch dung do {type(exc).__name__}: {exc}", traceback.format_exc())
        raise
    finally:
        loop.close()
    print(f"[INFO] Hoan thanh tai TTS da luong.")
    
    # 3. Sap xep ket qua va tinh toan time tich luy (Cumulative time)
    results.sort(key=lambda x: x[0])
    
    audio_paths = []
    durations = []
    srt_lines = []
    cumulative_time = 0.0
    
    res_dict = {r[0]: r for r in results}
    
    for i, s in enumerate(sentences):
        if sentence_voices[i] == "VOICE_TAG":
            continue
            
        if i not in res_dict:
            continue
            
        _, r_type, chunk_path, submaker = res_dict[i]
        
        if r_type == "MISSING_SFX":
            continue
            
        if r_type == "SFX":
            sfx_dur = get_audio_duration(chunk_path)
            audio_paths.append(chunk_path)
            durations.append(sfx_dur)
            srt_lines.append((cumulative_time, cumulative_time + sfx_dur, ""))
            cumulative_time += sfx_dur
            continue
            
        if r_type == "EMPTY_TTS":
            audio_paths.append(chunk_path)
            durations.append(3.0)
            cumulative_time += 3.0
            continue
            
        # Truong hop TTS binh thuong
        if submaker and submaker.cues:
            chunk_dur = submaker.cues[-1].end.total_seconds()
        else:
            chunk_dur = max(2.0, len(s) * 0.07)
            
        audio_paths.append(chunk_path)
        durations.append(chunk_dur)
        
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
            
    return audio_paths, durations

def format_timestamp(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

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
