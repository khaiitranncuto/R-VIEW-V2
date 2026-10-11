# Kiến trúc phân tích và dựng video

## Luồng Gemini hiện tại

Tool sử dụng tối đa hai lượt nội dung:

1. Tạo proxy 480p giữ nguyên timeline và âm thanh, rồi upload lên Gemini.
2. Lượt 1 xem toàn bộ video, nhận diện nhân vật, chia câu chuyện thành đúng ba phần,
   chọn cảnh chính/cảnh nối/cảnh dự phòng và viết một câu TTS cho từng `scene_id`.
3. Backend kiểm tra JSON, ranh giới ba phần, timestamp, độ dài, từ trừu tượng,
   câu lặp và các lỗi Gemini tự đánh dấu.
4. Nếu có lỗi, backend tạo một evidence reel chỉ chứa những `scene_id` liên quan.
5. Lượt 2 chỉ được sửa các ID lỗi, kích hoạt cảnh dự phòng hoặc bỏ cảnh nối.
   Timestamp, cảnh chính và các cảnh tốt bị khóa.
6. Mỗi lượt được retry tối đa một lần. Nếu lượt 2 không cải thiện, tool giữ kết quả
   hợp lệ gần nhất từ lượt 1 và tiếp tục.
7. JSON nội bộ được chuyển về các dòng `[TIME=START-END] lời TTS` cho pipeline dựng.

## Chia ba phần và độ dài

- Mốc ban đầu: 0–33%, 33–66% và 66–100% video.
- Gemini được dịch ranh giới tối đa 10% để tránh cắt ngang sự kiện.
- Ba phần phải liên tiếp, không chồng lấn và bao phủ đến cuối video.
- Ngân sách TTS được chia gần đều cho ba phần.
- Mỗi phần được thiếu tối đa 15% hoặc thừa tối đa 20%.
- Khi thiếu, chỉ dùng cảnh `reserve` thuộc cùng phần.
- Khi thừa, rút câu trước; chỉ cảnh `bridge` mới được phép bỏ.
- Mỗi câu TTS tối đa 25 từ.
- Số từ và số `narration_scene` của ba phần vẫn được giữ trong biên -15%/+20% quanh mục tiêu.
- `visual_beat` chỉ là shot hình trong một câu, không được tính thành scene mới khi cân bằng ba phần.
- Với video từ 10 phút, tối thiểu 20% scene của phần 3 phải mang vai trò `resolution` hoặc `ending`,
  và phải có ít nhất một scene `ending`.
- Nếu thiếu chỗ ở phần 3, pipeline yêu cầu gộp/bỏ bridge hành động lặp trước; không hy sinh kết cục.
- Nếu lượt xem toàn video đầu tiên thiếu ending, Gemini được thử lại với toàn video trước khi dùng sửa chọn lọc.

## Dữ liệu nội bộ

Lượt 1 trả về:

- `characters`: ID nhân vật ổn định và mô tả nhận dạng.
- `parts`: đúng ba phần với ranh giới video và ngân sách từ.
- `scenes`: cảnh đang dùng, gồm `scene_id`, timestamp, loại cảnh, sự kiện và TTS.
- `reserve_scenes`: cảnh dự phòng chưa được đưa vào kịch bản.
- `status/errors`: lỗi hoặc độ không chắc chắn của từng cảnh.

Lượt 2 chỉ trả:

- `repairs`: TTS thay thế cho ID được phép.
- `additions`: cảnh dự phòng được kích hoạt.
- `drop_scene_ids`: cảnh `bridge` được bỏ.

Backend bỏ qua mọi ID ngoài lô sửa và không nhận timestamp mới từ lượt 2.

## Render

TTS quyết định thời lượng video đầu ra. `TIME` là vùng hình ảnh nguồn minh họa cho
từng câu. Pipeline render vẫn đảm nhiệm giọng đọc, phụ đề, nhạc nền, chọn cảnh và
xuất MP4/CapCut.

### Chuyển cảnh khóa theo từ

- Một câu TTS có thể chứa nhiều `visual_beats`; beat đầu dùng `TIME`, các lần đổi hình dùng `BEAT`.
- Mỗi beat bổ sung có `trigger_text` 2-6 từ xuất hiện đúng một lần trong câu, một range nguồn,
  `match_type` (`literal`, `reaction`, `context`) và confidence tối thiểu 0.78.
- Sau khi Edge TTS trả word boundaries, renderer đổi hình 0.10 giây trước lúc đọc `trigger_text`.
- Nếu không khớp được từ thật, pipeline lùi về cách chia tỷ lệ từ cũ; nếu beat phân tích không chắc,
  nó bị bỏ và cảnh hiện tại được giữ.
- Khi TTS dài hơn range hình, START vẫn khóa; END được kéo qua footage thật tới trước START khóa kế
  tiếp. Nếu không còn đủ hình, phần thiếu ngắn mới giữ frame cuối, không tự đổi tốc độ giọng.

## Phục hồi lỗi

- Lỗi API tạm thời hoặc quota phút được retry trong giới hạn của lượt hiện tại.
- Quota ngày có thể chuyển sang API key ưu tiên tiếp theo.
- Lỗi lượt 2 không làm mất kết quả lượt 1.
- JSON lượt 1 không thể đọc sau một lần retry thì tác vụ dừng, vì không có dữ liệu
  an toàn để dựng video.
