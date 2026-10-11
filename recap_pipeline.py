import copy
import json
import math
import re


MAX_SCENE_WORDS = 25
MIN_SCENE_WORDS = 12
TARGET_SCENE_WORDS = 18
MIN_RESERVE_SCENES_PER_PART = 2
MIN_VISUAL_BEAT_CONFIDENCE = 0.78
MIN_ENDING_SCENE_RATIO = 0.20
LONG_STORY_SECONDS = 600.0
ABSTRACT_PHRASES = (
    "mọi chuyện", "điều đó", "tình hình", "một điều gì đó",
    "định mệnh", "số phận", "ý nghĩa cuộc sống", "bài học quý giá",
    "hiểu rằng", "nhận ra rằng", "thầm nghĩ", "tự nhủ", "trong lòng",
    "sâu thẳm", "dường như", "có vẻ như", "quyết tâm",
    "somehow", "things change", "the situation",
)


def _json_object(text):
    source = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", source, re.I)
    candidates = [fenced.group(1)] if fenced else []
    first, last = source.find("{"), source.rfind("}")
    if first >= 0 and last > first:
        candidates.append(source[first:last + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Gemini không trả về JSON object hợp lệ.")


def _time_seconds(value):
    fields = str(value or "").strip().split(":")
    if len(fields) not in (2, 3):
        raise ValueError(f"Timestamp không hợp lệ: {value}")
    try:
        fields = [float(item) for item in fields]
    except ValueError as exc:
        raise ValueError(f"Timestamp không hợp lệ: {value}") from exc
    if len(fields) == 2:
        minutes, seconds = fields
        if seconds >= 60:
            raise ValueError(f"Timestamp không hợp lệ: {value}")
        return minutes * 60 + seconds
    hours, minutes, seconds = fields
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"Timestamp không hợp lệ: {value}")
    return hours * 3600 + minutes * 60 + seconds


def _format_time(seconds):
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:05.2f}"
    return f"{minutes:02d}:{secs:05.2f}"


def three_part_targets(target_words):
    """Split a narration budget evenly without losing remainder words."""
    target_words = max(3, int(target_words))
    base, remainder = divmod(target_words, 3)
    return [base + (1 if index < remainder else 0) for index in range(3)]


def minimum_scene_count(target_words):
    """Minimum clips needed without forcing any TTS over its word ceiling."""
    return max(1, math.ceil(max(1, int(target_words)) / MAX_SCENE_WORDS))


def recommended_scene_range(target_words):
    """Return the -15%/+20% scene range around a natural narration density."""
    target = max(1, math.ceil(max(1, int(target_words)) / TARGET_SCENE_WORDS))
    return target, max(1, math.ceil(target * 0.85)), max(1, math.floor(target * 1.20))


def build_three_part_video_prompt(
    video_duration_sec, target_words, min_words, max_words,
    style="dramatic", custom_prompt="", enable_multi_voice=False,
):
    part_targets = three_part_targets(target_words)
    part_scene_minimums = [minimum_scene_count(words) for words in part_targets]
    part_scene_ranges = [recommended_scene_range(words) for words in part_targets]
    ending_scene_minimum = max(
        2, math.ceil(part_scene_ranges[2][0] * MIN_ENDING_SCENE_RATIO)
    )
    extras = str(custom_prompt or "").strip()
    voice_rule = (
        "Có thể đặt trường voice cho từng scene nếu thật sự cần nhiều giọng."
        if enable_multi_voice else
        "Không cần tạo trường voice."
    )
    return f"""Bạn là biên tập viên recap video. Hãy xem TOÀN BỘ video một lần, từ đầu đến cuối.

Trong cùng lượt này, hãy nhận diện nhân vật, chia câu chuyện thành đúng 3 phần,
chọn cảnh chính và viết một câu TTS bám đúng từng cảnh.

CHIA BA PHẦN:
- Chia gần bằng nhau: 0-33%, 33-66% và 66-100% thời lượng video.
- Chỉ được dịch mỗi ranh giới tối đa 5% thời lượng video để tránh cắt ngang sự kiện.
- Ba phần phải liên tiếp, không chồng lấn, không để khoảng trống.
- Mục tiêu lời kể từng phần lần lượt là {part_targets[0]}, {part_targets[1]}, {part_targets[2]} từ.
- Mỗi phần được thiếu tối đa 15% hoặc thừa tối đa 20% so với mục tiêu riêng.
- Mục tiêu số câu ba phần lần lượt là {part_scene_ranges[0][0]}, {part_scene_ranges[1][0]}, {part_scene_ranges[2][0]}.
- Khoảng chấp nhận (-15%/+20%) lần lượt là {part_scene_ranges[0][1]}-{part_scene_ranges[0][2]}, {part_scene_ranges[1][1]}-{part_scene_ranges[1][2]}, {part_scene_ranges[2][1]}-{part_scene_ranges[2][2]} câu.
- Số cảnh tối thiểu tuyệt đối của ba phần lần lượt là {part_scene_minimums[0]}, {part_scene_minimums[1]}, {part_scene_minimums[2]}.
- Mỗi phần phải có ít nhất {MIN_RESERVE_SCENES_PER_PART} reserve_scenes khác nhau để bù khi thiếu cảnh/lời.
- GIỮ CÂN BẰNG: nếu phần 3 có nhiều biến cố hơn, hãy kể gọn các đòn đánh/cảnh truy đuổi lặp lại;
  không được bỏ kết quả, giải cứu, đoàn tụ, chia tay hoặc trạng thái cuối chỉ để đạt số câu.
- Thứ tự ưu tiên bắt buộc: (1) đúng sự kiện và thời gian, (2) đủ mở đầu-diễn biến-kết cục,
  (3) cân bằng số từ/số câu ba phần, (4) khớp hình với lời, (5) văn phong.
- Trong phần 3, dành tối thiểu {ending_scene_minimum} narration_scene (khoảng 20-30% phần 3)
  cho resolution/ending. Nếu cần chỗ, gộp cảnh hành động lặp trước; không cắt phần kết.

NHÂN VẬT VÀ CẢNH:
- Phân biệt rõ: narration_scene là một câu TTS dùng để tính số câu cân bằng; visual_beat là một shot hình
  ngắn bên trong narration_scene và KHÔNG được tính thành câu/cảnh mới khi cân bằng ba phần.
- Gán nhân vật ổn định P1, P2...; chỉ ghi tên khi video xác nhận, nếu chưa biết thì mô tả ngắn.
- Gán cảnh ổn định S0001, S0002... theo đúng thứ tự thời gian.
- type chỉ được là main, bridge hoặc reserve. main là cảnh bắt buộc; bridge là cảnh nối;
  reserve là cảnh dự phòng và không được đưa vào scenes đang dùng.
- Mỗi cảnh đang dùng có đúng một câu tts, bắt buộc {MIN_SCENE_WORDS}-{MAX_SCENE_WORDS} từ, lý tưởng 16-22 từ.
- Câu phải kể đúng hành động trong START-END, đồng thời nối tự nhiên với diễn biến ngay trước hoặc hệ quả ngay sau đó.
- Nếu một câu kể thêm hành động/phản ứng nhìn thấy ở shot khác, tạo 1-2 visual_beats. Mỗi beat phải có
  trigger_text là cụm 2-6 từ xuất hiện NGUYÊN VẸN đúng một lần trong tts, và start/end là shot nguồn
  cần xuất hiện ngay khi giọng đọc tới trigger_text. Không tạo beat cho từ nối hay ý trừu tượng.
- START-END của scene là hình cho phần đầu câu; visual_beats chỉ chứa các lần đổi hình tiếp theo,
  theo đúng thứ tự trigger_text trong câu và đúng thứ tự thời gian của phim.
- confidence phải phản ánh độ chắc chắn thật; chỉ tạo visual_beat khi confidence >= {MIN_VISUAL_BEAT_CONFIDENCE:.2f}.
  Nếu không chắc đúng nhân vật/hành động hoặc không có shot bổ trợ rõ ràng, để visual_beats=[] và giữ cảnh hiện tại.
- match_type chỉ là literal, reaction hoặc context; ưu tiên literal, sau đó reaction, cuối cùng mới context.
- Không suy diễn nội tâm, động cơ, triết lý; không dùng câu trừu tượng hoặc lặp cách mở câu.
- Nếu chưa chắc câu khớp cảnh hay nhân vật, đặt status="error" và ghi mã lỗi trong errors.
  Nếu chắc chắn, đặt status="ok" và errors=[].
- START-END của narration_scene ưu tiên 6-15 giây; mỗi visual_beat thường chỉ 1-5 giây.
  Các range không chồng nhau và không đi lùi.
- Bỏ quảng cáo, intro, outro, credit và cảnh lặp không làm thay đổi câu chuyện.
- BẮT BUỘC phần 3 phải có ít nhất một scene main kể kết cục thật: xung đột được giải quyết ra sao,
  nhân vật chính sống/chết/trở về/thay đổi thế nào. Không được kết ở cao trào, trận đánh hoặc lúc căn cứ
  sụp đổ nếu video còn phần giải cứu, đoàn tụ, trở về hay kết quả sau đó.
- Scene đang dùng cuối cùng phải nằm gần phần kết nội dung thật. Đồng thời đặt ít nhất một reserve_scene
  ở vùng kết muộn nhất (ưu tiên shot muộn hơn hoặc góc phản ứng khác) làm bằng chứng dự phòng;
  không dùng credit/outro làm cảnh kết.
- Mỗi scene phải có story_role là development, climax, resolution hoặc ending. Phần 3 bắt buộc có cả
  resolution và ending; reserve_scene muộn nhất cũng phải mang story_role=ending.

CHẤT LƯỢNG LỜI KỂ (BẮT BUỘC):
- Viết như một người kể chuyện cuốn hút, mạch lạc; không viết như danh sách nhật ký cảnh quay.
- Mỗi câu cần có chi tiết cụ thể về hành động, trở ngại, kết quả hoặc thay đổi quan trọng đang nhìn thấy.
- Dùng linh hoạt các quan hệ nguyên nhân-kết quả, tương phản và tiếp diễn; không bắt buộc câu nào cũng có từ nối.
- Thay đổi nhịp và cách mở câu; tránh liên tiếp các mẫu "X làm...", "Cả nhóm...", "Họ...".
- Không dùng các câu chung chung như "tình thế nguy kịch", "kẻ thù đáng sợ", "căn cứ hiện đại" nếu hình ảnh không có chi tiết chứng minh.
- Không bịa thêm sự kiện chỉ để làm câu dài. Nếu một cảnh không đủ nội dung, hãy chọn cảnh giàu thông tin hơn.

ĐỘ DÀI TOÀN BÀI:
- Video dài khoảng {float(video_duration_sec) / 60.0:.1f} phút.
- Tổng TTS mục tiêu là {int(target_words)} từ; nên ít nhất {int(min_words)} từ. Nếu nội dung cần dài hơn thì được phép vượt mức mục tiêu.
- {voice_rule}
- Phong cách: {str(style or 'dramatic').strip()}.
{('- Yêu cầu thêm: ' + extras) if extras else ''}

Chỉ trả một JSON object hợp lệ, không Markdown và không giải thích:
{{
  "characters":[{{"id":"P1","name":"","description":"..."}}],
  "parts":[
    {{
      "part":1,
      "video_start":"00:00",
      "video_end":"00:40:00",
      "target_words":{part_targets[0]},
      "scenes":[{{
        "scene_id":"S0001","start":"00:10","end":"00:20",
        "type":"main","characters":["P1"],"event":"...","tts":"...",
        "story_role":"development",
        "visual_beats":[{{"trigger_text":"cụm từ trong tts","start":"00:16","end":"00:19",
          "match_type":"literal","confidence":0.92}}],
        "status":"ok","errors":[]
      }}],
      "reserve_scenes":[{{
        "scene_id":"S0901","start":"01:20","end":"01:30",
        "type":"reserve","characters":["P1"],"event":"...","story_role":"ending"
      }}]
    }}
  ]
}}
"""


def build_single_part_video_prompt(
    part_number, clip_duration_sec, global_start_sec, global_end_sec,
    target_words, style="dramatic", custom_prompt="", enable_multi_voice=False,
):
    """Prompt Gemini for one physical third of the source video."""
    part_number = int(part_number)
    target_words = max(1, int(target_words))
    target_scenes, minimum_scenes, maximum_scenes = recommended_scene_range(target_words)
    absolute_minimum = minimum_scene_count(target_words)
    minimum_words = math.ceil(target_words * 0.85)
    minimum_scenes_per_band = max(1, math.floor(target_scenes * 0.20))
    extras = str(custom_prompt or "").strip()
    ending_rules = ""
    if part_number == 3:
        ending_rules = f"""
- Đây là PHẦN CUỐI: bắt buộc xem đến hết clip và kể resolution/ending thật.
- Dành ít nhất {max(2, math.ceil(target_scenes * MIN_ENDING_SCENE_RATIO))} scenes cho resolution/ending.
- Không được dừng ở cao trào nếu sau đó còn giải cứu, đoàn tụ, trở về hoặc kết quả cuối.
"""
    voice_rule = (
        "Có thể đặt voice riêng cho scene khi thật sự cần nhiều giọng."
        if enable_multi_voice else "Không cần tạo trường voice."
    )
    return f"""Bạn là biên tập viên recap video. File đính kèm CHỈ là phần {part_number}/3 của video gốc.
Hãy xem TOÀN BỘ clip này từ đầu đến cuối và chỉ viết nội dung cho phần {part_number}.

MỤC TIÊU BẮT BUỘC:
- Tổng TTS mục tiêu: {target_words} từ; nên có ít nhất {minimum_words} từ. Nếu nội dung cần dài hơn thì được phép vượt mức mục tiêu.
- Tạo khoảng {target_scenes} narration scenes; biên chấp nhận {minimum_scenes}-{maximum_scenes} scenes.
- Tuyệt đối không được ít hơn {absolute_minimum} scenes + reserve_scenes, vì mỗi TTS tối đa {MAX_SCENE_WORDS} từ.
- Mỗi scene có đúng một câu TTS {MIN_SCENE_WORDS}-{MAX_SCENE_WORDS} từ, lý tưởng 16-22 từ.
- Tạo ít nhất {MIN_RESERVE_SCENES_PER_PART} reserve_scenes thật, khác scenes đang dùng.
- Trước khi trả JSON, hãy TỰ ĐẾM lại số scenes và tổng từ TTS. Nếu thiếu, bổ sung cảnh giàu thông tin; không kết thúc sớm.

- Chia clip thành 3 vùng thời gian bằng nhau; MỖI vùng phải có ít nhất {minimum_scenes_per_band} scenes đang dùng.
- Phải có ít nhất một scene đang dùng nằm trong 10% CUỐI clip. Không được viết đủ câu ở đầu rồi bỏ phần cuối.

TIMESTAMP:
- Mọi start/end trong JSON phải là thời gian CỤC BỘ của clip đính kèm, bắt đầu từ 00:00.
- Clip dài khoảng {float(clip_duration_sec) / 60.0:.1f} phút và ứng với vùng
  {_format_time(global_start_sec)}-{_format_time(global_end_sec)} của video gốc.
- Scene phải tăng theo thời gian, không đi lùi và không chồng lấn.

NỘI DUNG VÀ CHẤT LƯỢNG:
- Chọn main cho biến cố bắt buộc, bridge cho cảnh nối; bỏ quảng cáo, intro/outro/credit và cảnh lặp.
- scene_id dùng S0001, S0002... theo thời gian trong clip. Backend sẽ đổi ID khi ghép ba phần.
- Nhân vật dùng P1, P2...; mô tả ngoại hình/vai trò rõ để backend có thể đối chiếu giữa các phần.
- event mô tả sự kiện nhìn thấy; TTS phải đúng hành động trong timestamp, cụ thể và có mạch kể.
- Không bịa nội tâm hoặc sự kiện để đủ từ. Nếu thiếu, chọn thêm cảnh có diễn biến thật.
- story_role chỉ là development, climax, resolution hoặc ending.
- visual_beats chỉ tạo khi cần đổi hình trong cùng câu; trigger_text phải xuất hiện nguyên văn đúng một lần trong TTS.
- confidence của visual beat phải >= {MIN_VISUAL_BEAT_CONFIDENCE:.2f}; không chắc thì để visual_beats=[].
- {voice_rule}
- Phong cách: {str(style or 'dramatic').strip()}.
{ending_rules}
{('- Yêu cầu thêm: ' + extras) if extras else ''}

CHỈ trả một JSON object hợp lệ, không Markdown, không giải thích:
{{
  "characters":[{{"id":"P1","name":"","description":"..."}}],
  "parts":[{{
    "part":{part_number},
    "video_start":"00:00",
    "video_end":"{_format_time(clip_duration_sec)}",
    "target_words":{target_words},
    "scenes":[{{
      "scene_id":"S0001","start":"00:10","end":"00:20",
      "type":"main","characters":["P1"],"event":"...","tts":"...",
      "story_role":"development","visual_beats":[],"status":"ok","errors":[]
    }}],
    "reserve_scenes":[{{
      "scene_id":"S0901","start":"01:20","end":"01:30",
      "type":"reserve","characters":["P1"],"event":"...","story_role":"development"
    }}]
  }}]
}}
"""


def single_part_missing_requirements(text, clip_duration_sec, target_words):
    """Return the exact clip-local regions that still need narration scenes."""
    payload = _json_object(text)
    parts = payload.get("parts")
    if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
        raise ValueError("Kết quả một phần phải chứa đúng một phần.")
    part = parts[0]
    scenes = [row for row in (part.get("scenes") or []) if isinstance(row, dict)]
    reserves = [
        row for row in (part.get("reserve_scenes") or []) if isinstance(row, dict)
    ]
    duration = max(1.0, float(clip_duration_sec))
    boundaries = [0.0, duration / 3.0, duration * 2.0 / 3.0, duration]
    target_scenes, _, _ = recommended_scene_range(target_words)
    minimum_per_band = max(1, math.floor(target_scenes * 0.20))
    counts = [0, 0, 0]
    valid_ends = []
    for scene in scenes:
        start = _time_seconds(scene.get("start"))
        end = _time_seconds(scene.get("end"))
        if end <= start:
            continue
        midpoint = (start + end) / 2.0
        band = min(2, max(0, int(midpoint / duration * 3.0)))
        counts[band] += 1
        valid_ends.append(end)

    actual_words = sum(len(str(scene.get("tts", "")).split()) for scene in scenes)
    minimum_words = math.ceil(max(1, int(target_words)) * 0.85)
    required_capacity = minimum_scene_count(target_words)
    available_capacity = len(scenes) + len(reserves)
    missing_words = max(0, minimum_words - actual_words)
    needed_total = max(
        max(0, required_capacity - available_capacity),
        math.ceil(missing_words / MAX_SCENE_WORDS),
    )

    missing_by_band = [max(0, minimum_per_band - count) for count in counts]
    needs_endpoint = not valid_ends or max(valid_ends) < duration * 0.90
    if needs_endpoint:
        missing_by_band[2] = max(1, missing_by_band[2])
    while sum(missing_by_band) < needed_total:
        band = min(range(3), key=lambda idx: counts[idx] + missing_by_band[idx])
        missing_by_band[band] += 1

    regions = []
    for index, missing in enumerate(missing_by_band):
        if missing:
            regions.append({
                "band": index + 1,
                "start": (
                    duration * 0.90
                    if index == 2 and needs_endpoint else boundaries[index]
                ),
                "end": boundaries[index + 1],
                "actual_scenes": counts[index],
                "required_scenes": minimum_per_band,
                "new_scenes": missing,
            })
    return {
        "regions": regions,
        "actual_words": actual_words,
        "minimum_words": minimum_words,
        "missing_words": missing_words,
        "available_capacity": available_capacity,
        "required_capacity": required_capacity,
    }


def build_single_part_continuation_prompt(
    part_number, clip_duration_sec, target_words, current_text, requirements,
):
    """Ask only for new scenes in uncovered clip-local time regions."""
    payload = _json_object(current_text)
    current_part = payload["parts"][0]
    existing = [
        {
            "start": row.get("start"),
            "end": row.get("end"),
            "event": row.get("event", ""),
        }
        for row in current_part.get("scenes") or []
        if isinstance(row, dict)
    ]
    region_lines = []
    for region in requirements["regions"]:
        region_lines.append(
            f"- Vùng {region['band']} ({_format_time(region['start'])}-"
            f"{_format_time(region['end'])}): thêm đúng {region['new_scenes']} scenes mới."
        )
    return f"""Đây là lượt VIẾT TIẾP cho phần {int(part_number)}/3, không viết lại kết quả cũ.
Bạn vẫn đang xem đúng clip dài khoảng {float(clip_duration_sec) / 60.0:.1f} phút; timestamp phải là thời gian cục bộ từ 00:00.

KẾT QUẢ HIỆN TẠI:
- Có {requirements['actual_words']} từ TTS; mức tối thiểu là {requirements['minimum_words']} từ.
- Còn thiếu khoảng {requirements['missing_words']} từ.

CHỈ BỔ SUNG CÁC VÙNG SAU:
{chr(10).join(region_lines)}

QUY TẮC BẮT BUỘC:
- Chỉ trả scene MỚI nằm trong đúng các vùng trên; không lặp timestamp hoặc sự kiện đã có.
- Mỗi scene có một câu TTS {MIN_SCENE_WORDS}-{MAX_SCENE_WORDS} từ và mô tả đúng hình nhìn thấy.
- Xem kỹ vùng cuối; không tự đoán kết thúc. Phần 3 phải kể kết quả thật nếu clip cho thấy kết cục.
- Không trả lại các scene cũ, không Markdown, không giải thích.

SCENE ĐÃ CÓ (để tránh trùng):
{json.dumps(existing, ensure_ascii=False)}

Chỉ trả JSON:
{{"characters":[],"parts":[{{"part":{int(part_number)},"scenes":[{{
"scene_id":"NEW001","start":"00:00","end":"00:08","type":"main",
"characters":[],"event":"...","tts":"...","story_role":"development",
"visual_beats":[],"status":"ok","errors":[]
}}],"reserve_scenes":[]}}]}}
"""


def merge_single_part_additions(current_text, additions_text, allowed_regions):
    """Append only non-overlapping new scenes from requested local regions."""
    current = _json_object(current_text)
    additions = _json_object(additions_text)
    current_parts = current.get("parts")
    addition_parts = additions.get("parts")
    if not isinstance(current_parts, list) or len(current_parts) != 1:
        raise ValueError("Kết quả hiện tại không có đúng một phần.")
    if not isinstance(addition_parts, list) or len(addition_parts) != 1:
        raise ValueError("Kết quả bổ sung không có đúng một phần.")
    part = current_parts[0]
    existing = [row for row in (part.get("scenes") or []) if isinstance(row, dict)]
    occupied = [
        (_time_seconds(row.get("start")), _time_seconds(row.get("end")))
        for row in existing
    ]
    limits = {
        int(region["band"]): int(region["new_scenes"])
        for region in allowed_regions
    }
    accepted_by_band = {band: 0 for band in limits}
    accepted = []
    candidates = [
        row for row in (addition_parts[0].get("scenes") or [])
        if isinstance(row, dict)
    ]
    candidates.sort(key=lambda row: _time_seconds(row.get("start")))
    for row in candidates:
        start = _time_seconds(row.get("start"))
        end = _time_seconds(row.get("end"))
        if end <= start:
            continue
        midpoint = (start + end) / 2.0
        region = next(
            (
                item for item in allowed_regions
                if float(item["start"]) <= midpoint < float(item["end"])
            ),
            None,
        )
        if region is None:
            continue
        band = int(region["band"])
        if accepted_by_band[band] >= limits[band]:
            continue
        if any(start < old_end and end > old_start for old_start, old_end in occupied):
            continue
        row = copy.deepcopy(row)
        row["scene_id"] = f"NEW{len(existing) + len(accepted) + 1:04d}"
        accepted.append(row)
        occupied.append((start, end))
        accepted_by_band[band] += 1

    if accepted:
        part.setdefault("scenes", []).extend(accepted)
        part["scenes"].sort(key=lambda row: _time_seconds(row.get("start")))
    existing_character_ids = {
        str(row.get("id", "")).strip().upper()
        for row in current.get("characters") or [] if isinstance(row, dict)
    }
    for character in additions.get("characters") or []:
        if not isinstance(character, dict):
            continue
        character_id = str(character.get("id", "")).strip().upper()
        if character_id and character_id not in existing_character_ids:
            current.setdefault("characters", []).append(copy.deepcopy(character))
            existing_character_ids.add(character_id)
    return json.dumps(current, ensure_ascii=False), len(accepted)


def merge_single_part_responses(responses, boundaries, target_words):
    """Merge three clip-local Gemini responses into one global payload."""
    if len(responses) != 3 or len(boundaries) != 4:
        raise ValueError("Cần đúng ba kết quả và bốn mốc ranh giới để ghép.")

    merged_characters = []
    seen_character_ids = set()
    merged_parts = []
    active_id = 1
    reserve_id = 9001

    for index, response_text in enumerate(responses):
        part_number = index + 1
        payload = _json_object(response_text)
        raw_parts = payload.get("parts")
        if not isinstance(raw_parts, list) or len(raw_parts) != 1:
            raise ValueError(f"Kết quả phần {part_number} phải chứa đúng một phần.")
        raw_part = copy.deepcopy(raw_parts[0])
        if not isinstance(raw_part, dict):
            raise ValueError(f"Kết quả phần {part_number} không phải object.")

        for character in payload.get("characters") or []:
            if not isinstance(character, dict):
                continue
            character_id = str(character.get("id", "")).strip().upper()
            if character_id and character_id not in seen_character_ids:
                seen_character_ids.add(character_id)
                merged_characters.append(copy.deepcopy(character))

        offset = float(boundaries[index])
        raw_part["part"] = part_number
        raw_part["video_start"] = _format_time(boundaries[index])
        raw_part["video_end"] = _format_time(boundaries[index + 1])

        for key in ("scenes", "reserve_scenes"):
            rows = raw_part.get(key)
            if not isinstance(rows, list):
                rows = []
            rows.sort(key=lambda row: _time_seconds(row.get("start")) if isinstance(row, dict) else 0.0)
            normalized_rows = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row = copy.deepcopy(row)
                row["start"] = _format_time(offset + _time_seconds(row.get("start")))
                row["end"] = _format_time(offset + _time_seconds(row.get("end")))
                for beat in row.get("visual_beats") or []:
                    if isinstance(beat, dict):
                        beat["start"] = _format_time(offset + _time_seconds(beat.get("start")))
                        beat["end"] = _format_time(offset + _time_seconds(beat.get("end")))
                if key == "scenes":
                    row["scene_id"] = f"S{active_id:04d}"
                    active_id += 1
                else:
                    row["scene_id"] = f"S{reserve_id:04d}"
                    reserve_id += 1
                normalized_rows.append(row)
            raw_part[key] = normalized_rows
        merged_parts.append(raw_part)

    merged = {"characters": merged_characters, "parts": merged_parts}
    return normalize_three_part_payload(
        json.dumps(merged, ensure_ascii=False), boundaries[-1], target_words
    )


def validate_single_part_response(
    text, part_number, target_words, require_coverage=True,
):
    """Reject an undersized clip response before moving to the next third."""
    payload = _json_object(text)
    parts = payload.get("parts")
    if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
        raise ValueError(f"Kết quả phần {part_number} phải chứa đúng một phần.")
    part = parts[0]
    scenes = part.get("scenes") if isinstance(part.get("scenes"), list) else []
    reserves = (
        part.get("reserve_scenes")
        if isinstance(part.get("reserve_scenes"), list) else []
    )
    required = minimum_scene_count(target_words)
    available = len(scenes) + len(reserves)
    if available < required:
        raise ValueError(
            f"Phần {part_number} chỉ có {available} cảnh/cảnh dự phòng; "
            f"cần ít nhất {required} để đủ ngân sách lời."
        )
    actual_words = sum(
        len(str(scene.get("tts", "")).split())
        for scene in scenes if isinstance(scene, dict)
    )
    minimum_words = math.ceil(max(1, int(target_words)) * 0.85)
    if actual_words < minimum_words:
        raise ValueError(
            f"Phần {part_number} chỉ có {actual_words} từ TTS; "
            f"cần ít nhất {minimum_words} từ."
        )
    clip_duration = max(
        [_time_seconds(scene.get("end")) for scene in scenes if isinstance(scene, dict)]
        + [1.0]
    )
    declared_end = _time_seconds(part.get("video_end"))
    if declared_end > clip_duration:
        clip_duration = declared_end
    requirements = single_part_missing_requirements(text, clip_duration, target_words)
    if require_coverage and requirements["regions"]:
        details = ", ".join(
            f"vùng {item['band']} thiếu {item['new_scenes']} cảnh"
            for item in requirements["regions"]
        )
        raise ValueError(f"Phần {part_number} chưa phủ đủ thời gian: {details}.")
    return payload


def normalize_three_part_payload(text, video_duration_sec, target_words):
    """Parse and normalize the pass-1 JSON while preserving model evidence."""
    payload = _json_object(text)
    characters = payload.get("characters")
    parts = payload.get("parts")
    if not isinstance(characters, list):
        characters = []
    if not isinstance(parts, list) or len(parts) != 3:
        raise ValueError("Lượt 1 phải trả đúng ba phần.")

    expected_parts = {1, 2, 3}
    seen_parts = set()
    seen_scene_ids = set()
    normalized_parts = []
    previous_boundary = 0.0
    previous_active_start = -1.0
    previous_active_scene = None
    part_targets = three_part_targets(target_words)
    for raw_part in parts:
        if not isinstance(raw_part, dict):
            raise ValueError("Mỗi phần phải là một object.")
        number = int(raw_part.get("part", 0))
        if number not in expected_parts or number in seen_parts:
            raise ValueError("Số phần bị thiếu hoặc trùng.")
        if number != len(normalized_parts) + 1:
            raise ValueError("Ba phần phải được trả theo thứ tự 1, 2, 3.")
        seen_parts.add(number)
        start = _time_seconds(raw_part.get("video_start"))
        end = _time_seconds(raw_part.get("video_end"))
        if end <= start:
            raise ValueError(f"Phần {number} có ranh giới không hợp lệ.")
        if number == 1 and abs(start) > 2.0:
            raise ValueError("Phần 1 phải bắt đầu từ đầu video.")
        if number > 1 and abs(start - previous_boundary) > 2.0:
            raise ValueError("Ba phần phải nối tiếp nhau, không có khoảng trống.")
        previous_boundary = end

        normalized = {
            "part": number,
            "video_start": _format_time(start),
            "video_end": _format_time(end),
            "target_words": part_targets[number - 1],
            "scenes": [],
            "reserve_scenes": [],
        }
        for key in ("scenes", "reserve_scenes"):
            rows = raw_part.get(key)
            if not isinstance(rows, list):
                rows = []
            for raw_scene in rows:
                if not isinstance(raw_scene, dict):
                    continue
                scene_id = str(raw_scene.get("scene_id", "")).strip().upper()
                if not re.fullmatch(r"S\d{4,}", scene_id):
                    raise ValueError(f"scene_id không hợp lệ: {scene_id or 'rỗng'}")
                if scene_id in seen_scene_ids:
                    raise ValueError(f"scene_id bị trùng: {scene_id}")
                seen_scene_ids.add(scene_id)
                scene_start = _time_seconds(raw_scene.get("start"))
                scene_end = _time_seconds(raw_scene.get("end"))
                if scene_end <= scene_start:
                    raise ValueError(f"{scene_id} có START-END không hợp lệ.")
                if key == "scenes" and scene_start <= previous_active_start:
                    raise ValueError("Các scene_id đang dùng phải tăng theo thời gian.")
                if key == "scenes":
                    if (
                        previous_active_scene is not None
                        and _time_seconds(previous_active_scene["end"]) > scene_start
                    ):
                        previous_active_scene["end"] = _format_time(scene_start)
                    previous_active_start = scene_start
                scene_type = str(raw_scene.get("type", "")).strip().lower()
                if key == "reserve_scenes":
                    scene_type = "reserve"
                elif scene_type not in {"main", "bridge"}:
                    scene_type = "main"
                scene = {
                    "scene_id": scene_id,
                    "start": _format_time(scene_start),
                    "end": _format_time(scene_end),
                    "type": scene_type,
                    "characters": list(raw_scene.get("characters") or []),
                    "event": re.sub(r"\s+", " ", str(raw_scene.get("event", ""))).strip(),
                    "tts": re.sub(r"\s+", " ", str(raw_scene.get("tts", ""))).strip(),
                    "story_role": str(raw_scene.get("story_role", "")).strip().lower(),
                    "status": str(raw_scene.get("status", "ok")).strip().lower(),
                    "errors": list(raw_scene.get("errors") or []),
                }
                if scene["story_role"] not in {
                    "development", "climax", "resolution", "ending"
                }:
                    scene["story_role"] = ""
                # Optional word-locked shot changes inside the same natural
                # narration sentence. Invalid beats are ignored instead of
                # making an otherwise usable analysis fail completely.
                visual_beats = []
                narration = scene["tts"]
                previous_trigger_end = 0
                previous_beat_start = scene_start
                for raw_beat in list(raw_scene.get("visual_beats") or []):
                    if not isinstance(raw_beat, dict):
                        continue
                    trigger = re.sub(
                        r"\s+", " ", str(raw_beat.get("trigger_text", ""))
                    ).strip()
                    trigger_word_count = len(re.findall(r"\w+", trigger, flags=re.UNICODE))
                    if not trigger or not 2 <= trigger_word_count <= 6:
                        continue
                    matches = list(re.finditer(re.escape(trigger), narration, flags=re.IGNORECASE))
                    if len(matches) != 1 or matches[0].start() < previous_trigger_end:
                        continue
                    try:
                        beat_start = _time_seconds(raw_beat.get("start"))
                        beat_end = _time_seconds(raw_beat.get("end"))
                    except ValueError:
                        continue
                    if beat_end <= beat_start or beat_start <= previous_beat_start:
                        continue
                    confidence = raw_beat.get("confidence", 0.0)
                    try:
                        confidence = min(1.0, max(0.0, float(confidence)))
                    except (TypeError, ValueError):
                        confidence = 0.0
                    if confidence < MIN_VISUAL_BEAT_CONFIDENCE:
                        continue
                    match_type = str(raw_beat.get("match_type", "literal")).strip().lower()
                    if match_type not in {"literal", "reaction", "context"}:
                        match_type = "literal"
                    visual_beats.append({
                        "trigger_text": trigger,
                        "start": _format_time(beat_start),
                        "end": _format_time(beat_end),
                        "match_type": match_type,
                        "confidence": confidence,
                    })
                    previous_trigger_end = matches[0].end()
                    previous_beat_start = beat_start
                scene["visual_beats"] = visual_beats
                normalized[key].append(scene)
                if key == "scenes":
                    previous_active_scene = scene
        normalized_parts.append(normalized)

    normalized_parts.sort(key=lambda item: item["part"])
    # Gemini occasionally puts a valid reserve clip under the neighboring
    # part. Reserve clips are not active yet, so safely re-home them by their
    # midpoint instead of rejecting the complete analysis.
    all_reserves = [
        scene
        for part in normalized_parts
        for scene in part["reserve_scenes"]
    ]
    for part in normalized_parts:
        part["reserve_scenes"] = []
    for scene in all_reserves:
        midpoint = (_time_seconds(scene["start"]) + _time_seconds(scene["end"])) / 2.0
        owner = next(
            (
                part for part in normalized_parts
                if _time_seconds(part["video_start"]) <= midpoint
                <= _time_seconds(part["video_end"])
            ),
            None,
        )
        if owner is not None:
            owner["reserve_scenes"].append(scene)

    for part in normalized_parts:
        part["reserve_scenes"].sort(key=lambda item: _time_seconds(item["start"]))
        required_scenes = minimum_scene_count(part["target_words"])
        available_scenes = len(part["scenes"]) + len(part["reserve_scenes"])
        if available_scenes < required_scenes:
            raise ValueError(
                f"Phần {part['part']} chỉ có {available_scenes} cảnh/cảnh dự phòng; "
                f"cần ít nhất {required_scenes} để đủ ngân sách lời."
            )
    if seen_parts != expected_parts:
        raise ValueError("Lượt 1 thiếu một trong ba phần.")
    if abs(previous_boundary - float(video_duration_sec)) > max(5.0, float(video_duration_sec) * 0.03):
        raise ValueError("Phần 3 chưa bao phủ đến cuối video.")
    source_duration = max(1.0, float(video_duration_sec))
    first_boundary = _time_seconds(normalized_parts[0]["video_end"]) / source_duration
    second_boundary = _time_seconds(normalized_parts[1]["video_end"]) / source_duration
    if not 0.283 <= first_boundary <= 0.383:
        raise ValueError("Ranh giới phần 1 nằm ngoài vùng 33% ±5%.")
    if not 0.617 <= second_boundary <= 0.717:
        raise ValueError("Ranh giới phần 2 nằm ngoài vùng 66% ±5%.")
    if not any(part["scenes"] for part in normalized_parts):
        raise ValueError("Lượt 1 không tạo cảnh recap.")
    return {"characters": characters, "parts": normalized_parts}


def inspect_three_part_payload(payload):
    """Return deterministic scene/part defects for the selective second pass."""
    issues = []
    previous_scene_start = -1.0
    opening_phrases = {}
    phrase_owners = {}
    for part in payload["parts"]:
        part_words = 0
        for scene in part["scenes"]:
            scene_id = scene["scene_id"]
            start = _time_seconds(scene["start"])
            end = _time_seconds(scene["end"])
            narration = scene["tts"]
            words = narration.split()
            codes = []
            if not narration:
                codes.append("missing_tts")
            elif len(words) < MIN_SCENE_WORDS:
                codes.append("tts_too_short")
            if len(words) > MAX_SCENE_WORDS:
                codes.append("tts_too_long")
            if start < previous_scene_start:
                codes.append("timestamp_out_of_order")
            if end <= start:
                codes.append("invalid_timestamp")
            previous_scene_start = max(previous_scene_start, start)
            lowered = narration.lower()
            if any(phrase in lowered for phrase in ABSTRACT_PHRASES):
                codes.append("abstract_wording")
            opening = " ".join(re.findall(r"\w+", lowered, re.UNICODE)[:4])
            if opening:
                opening_phrases.setdefault(opening, []).append(scene_id)
            tokens = re.findall(r"\w+", lowered, re.UNICODE)
            for index in range(max(0, len(tokens) - 3)):
                phrase = " ".join(tokens[index:index + 4])
                phrase_owners.setdefault(phrase, []).append(scene_id)
            if scene.get("status") == "error":
                codes.extend(str(item).strip() for item in scene.get("errors", []) if str(item).strip())
                if not scene.get("errors"):
                    codes.append("model_uncertain")
            for code in dict.fromkeys(codes):
                issues.append({"scope": "scene", "target_id": scene_id, "code": code})
            part_words += len(words)

        target = max(1, int(part["target_words"]))
        target_scenes, minimum_scenes, maximum_scenes = recommended_scene_range(target)
        minimum = math.ceil(target * 0.85)
        maximum = max(minimum, int(target * 1.20))
        if len(part["scenes"]) < minimum_scenes:
            issues.append({
                "scope": "part", "target_id": f"PART_{part['part']}",
                "code": "part_too_few_scenes",
                "actual_scenes": len(part["scenes"]),
                "target_scenes": target_scenes,
                "minimum_scenes": minimum_scenes,
                "missing_scenes": minimum_scenes - len(part["scenes"]),
            })
        elif len(part["scenes"]) > maximum_scenes:
            issues.append({
                "scope": "part", "target_id": f"PART_{part['part']}",
                "code": "part_too_many_scenes",
                "actual_scenes": len(part["scenes"]),
                "target_scenes": target_scenes,
                "maximum_scenes": maximum_scenes,
                "excess_scenes": len(part["scenes"]) - maximum_scenes,
            })
        if part_words < minimum:
            issues.append({
                "scope": "part", "target_id": f"PART_{part['part']}",
                "code": "part_too_short", "actual_words": part_words,
                "minimum_words": minimum, "maximum_words": maximum,
            })

    for scene_ids in opening_phrases.values():
        if len(scene_ids) > 2:
            for scene_id in scene_ids[2:]:
                issues.append({"scope": "scene", "target_id": scene_id, "code": "repeated_opening"})
    repeated_ids = set()
    for owners in phrase_owners.values():
        unique_owners = list(dict.fromkeys(owners))
        if len(unique_owners) > 2:
            repeated_ids.update(unique_owners[2:])
    for scene_id in sorted(repeated_ids):
        issues.append({"scope": "scene", "target_id": scene_id, "code": "repeated_phrase"})

    # The part metadata may claim coverage to EOF while its selected scenes
    # silently stop at the climax. Catch a material uncovered tail, but allow
    # normal credits/outro (up to ten percent or five minutes).
    final_part = payload["parts"][-1]
    final_scenes = final_part.get("scenes") or []
    if final_scenes:
        story_end = max(_time_seconds(scene["end"]) for scene in final_scenes)
        video_end = _time_seconds(final_part["video_end"])
        allowed_tail = max(300.0, video_end * 0.10)
        uncovered_tail = max(0.0, video_end - story_end)
        if uncovered_tail > allowed_tail:
            issues.append({
                "scope": "part",
                "target_id": f"PART_{final_part['part']}",
                "code": "story_ending_missing",
                "last_scene_end": _format_time(story_end),
                "video_end": _format_time(video_end),
                "uncovered_seconds": round(uncovered_tail, 2),
            })
        if video_end >= LONG_STORY_SECONDS:
            closing_roles = {"resolution", "ending"}
            closing_count = sum(
                1 for scene in final_scenes
                if scene.get("story_role") in closing_roles
            )
            required_closing = min(
                len(final_scenes),
                max(2, math.ceil(len(final_scenes) * MIN_ENDING_SCENE_RATIO)),
            )
            has_ending = any(
                scene.get("story_role") == "ending" for scene in final_scenes
            )
            if closing_count < required_closing or not has_ending:
                issues.append({
                    "scope": "part",
                    "target_id": f"PART_{final_part['part']}",
                    "code": "part3_ending_budget_low",
                    "closing_scenes": closing_count,
                    "minimum_closing_scenes": required_closing,
                    "has_ending": has_ending,
                })
    return issues


def issue_scene_ids(payload, issues):
    """Return only clips needed to repair reported scene or part defects."""
    ids = []
    for issue in issues:
        target = issue["target_id"]
        if issue["scope"] == "scene":
            ids.append(target)
            continue
        part_number = int(target.split("_", 1)[1])
        part = next(item for item in payload["parts"] if item["part"] == part_number)
        ids.extend(scene["scene_id"] for scene in part["scenes"])
        if issue["code"] in {
            "part_too_short", "part_too_few_scenes", "story_ending_missing",
            "part3_ending_budget_low",
        }:
            ids.extend(scene["scene_id"] for scene in part["reserve_scenes"])
    return list(dict.fromkeys(ids))


def scenes_by_ids(payload, scene_ids):
    requested = set(scene_ids)
    rows = []
    for part in payload["parts"]:
        for key in ("scenes", "reserve_scenes"):
            for scene in part[key]:
                if scene["scene_id"] in requested:
                    row = copy.deepcopy(scene)
                    row["part"] = part["part"]
                    row["is_reserve"] = key == "reserve_scenes"
                    rows.append(row)
    return rows


def build_selective_repair_prompt(payload, issues):
    scene_ids = issue_scene_ids(payload, issues)
    evidence = scenes_by_ids(payload, scene_ids)
    return f"""Đây là lượt sửa cuối. Video đính kèm CHỈ gồm các scene_id bị lỗi hoặc cảnh dự phòng liên quan.

Chỉ sửa đúng các lỗi được liệt kê. Không viết lại cảnh tốt, không đổi timestamp,
không đổi thứ tự câu chuyện và không thêm sự kiện ngoài dữ liệu lượt 1.

Quy tắc:
- Với lỗi scene: trả tts mới cho đúng scene_id.
- tts_too_short: viết lại thành {MIN_SCENE_WORDS}-{MAX_SCENE_WORDS} từ bằng chi tiết thật trong clip; thêm nguyên nhân, trở ngại hoặc kết quả nhìn thấy, tuyệt đối không chèn từ rỗng hay bịa sự kiện.
- part_too_short: được kích hoạt scene reserve của chính phần đó bằng additions.
- part_too_few_scenes: phải kích hoạt đủ số reserve trong missing_scenes bằng additions; không kéo dài câu cũ để thay thế cảnh thiếu.
- part_too_many_scenes: phải bỏ bớt đúng số excess_scenes; rút gọn TTS và chỉ ghi scene type=bridge vào drop_scene_ids, không bỏ main.
- story_ending_missing: kịch bản đang dừng trước kết cục. Phải chọn addition muộn nhất có nội dung thật
  trong reserve_scenes của phần 3, viết TTS kể kết quả sau cao trào và số phận/trạng thái cuối của nhân vật.
  Không được coi trận đánh, chạy thoát tạm thời hay căn cứ sụp đổ là kết thúc nếu clip còn diễn biến sau đó.
- part3_ending_budget_low: giữ tổng số câu phần 3 trong biên cân bằng nhưng dành đủ minimum_closing_scenes
  cho resolution/ending. Kích hoạt reserve kết thúc bằng additions và bỏ đúng số bridge hành động lặp bằng
  drop_scene_ids khi cần để tổng số câu không tăng quá biên; không bỏ main, resolution hoặc ending.
- Luôn ưu tiên đưa mỗi phần về target_scenes; bắt buộc nằm trong biên -15%/+20%.
- Mỗi TTS là một câu {MIN_SCENE_WORDS}-{MAX_SCENE_WORDS} từ, cụ thể, có mạch kể và đọc tự nhiên.
- Không trả chuỗi câu ngắn theo mẫu "tên nhân vật + hành động"; phải thay đổi cấu trúc và nối logic với câu chuyện.
- Nếu không sửa chắc chắn được, không đưa scene đó vào repairs; backend sẽ giữ câu cũ.
- Chỉ trả JSON hợp lệ, không Markdown.

LỖI:
{json.dumps(issues, ensure_ascii=False)}

DỮ LIỆU CÁC CẢNH LIÊN QUAN:
{json.dumps(evidence, ensure_ascii=False)}

Định dạng trả về:
{{"repairs":[{{"scene_id":"S0001","tts":"Câu đã sửa."}}],
 "additions":[{{"scene_id":"S0901","tts":"Câu cho cảnh dự phòng."}}],
 "drop_scene_ids":["S0002"]}}
"""


def apply_selective_repairs(payload, repair_text, allowed_scene_ids=None):
    """Merge only whitelisted repairs; timestamps and good scenes stay locked."""
    repair = _json_object(repair_text)
    candidate = copy.deepcopy(payload)
    allowed = (
        {str(item).strip().upper() for item in allowed_scene_ids}
        if allowed_scene_ids is not None else None
    )
    active = {}
    reserve = {}
    scene_part = {}
    for part in candidate["parts"]:
        for scene in part["scenes"]:
            active[scene["scene_id"]] = scene
            scene_part[scene["scene_id"]] = part
        for scene in part["reserve_scenes"]:
            reserve[scene["scene_id"]] = scene
            scene_part[scene["scene_id"]] = part

    for row in repair.get("repairs", []):
        if not isinstance(row, dict):
            continue
        scene_id = str(row.get("scene_id", "")).strip().upper()
        narration = re.sub(r"\s+", " ", str(row.get("tts", ""))).strip()
        if scene_id in active and narration and (allowed is None or scene_id in allowed):
            active[scene_id]["tts"] = narration
            active[scene_id]["status"] = "ok"
            active[scene_id]["errors"] = []

    for row in repair.get("additions", []):
        if not isinstance(row, dict):
            continue
        scene_id = str(row.get("scene_id", "")).strip().upper()
        narration = re.sub(r"\s+", " ", str(row.get("tts", ""))).strip()
        if (
            scene_id not in reserve or not narration
            or (allowed is not None and scene_id not in allowed)
        ):
            continue
        scene = reserve.pop(scene_id)
        scene["type"] = "bridge"
        scene["tts"] = narration
        scene["status"] = "ok"
        scene["errors"] = []
        part = scene_part[scene_id]
        part["reserve_scenes"] = [item for item in part["reserve_scenes"] if item["scene_id"] != scene_id]
        part["scenes"].append(scene)

    drop_ids = {
        str(item).strip().upper() for item in repair.get("drop_scene_ids", [])
    }
    for scene_id in drop_ids:
        if allowed is not None and scene_id not in allowed:
            continue
        scene = active.get(scene_id)
        if scene and scene.get("type") == "bridge":
            part = scene_part[scene_id]
            part["scenes"] = [item for item in part["scenes"] if item["scene_id"] != scene_id]

    for part in candidate["parts"]:
        part["scenes"].sort(key=lambda item: _time_seconds(item["start"]))
    return candidate


def payload_to_timeline(payload):
    rows = []
    for part in payload["parts"]:
        for scene in part["scenes"]:
            narration = str(scene.get("tts", "")).strip()
            if narration:
                # Encode word-locked changes as inline BEAT markers. The TTS
                # parser removes them from speech and later resolves the exact
                # cut from Edge word-boundary timing.
                insertions = []
                for beat in scene.get("visual_beats", []):
                    trigger = str(beat.get("trigger_text", "")).strip()
                    matches = list(re.finditer(re.escape(trigger), narration, flags=re.IGNORECASE)) if trigger else []
                    if len(matches) == 1:
                        marker = f"[BEAT={beat['start']}-{beat['end']}] "
                        insertions.append((matches[0].start(), marker))
                for position, marker in sorted(insertions, reverse=True):
                    narration = narration[:position] + marker + narration[position:]
                rows.append(f"[TIME={scene['start']}-{scene['end']}] {narration}")
    if not rows:
        raise ValueError("Không có câu TTS hợp lệ để dựng video.")
    return "\n".join(rows)


def clamp_payload_narrations(payload, max_words=MAX_SCENE_WORDS):
    """Apply the final word ceiling directly to stored scene metadata."""
    result = copy.deepcopy(payload)
    for part in result["parts"]:
        for scene in part["scenes"]:
            words = str(scene.get("tts", "")).split()
            if len(words) > int(max_words):
                scene["tts"] = (
                    " ".join(words[:int(max_words)]).rstrip(" ,;:-.!?") + "."
                )
    return result
