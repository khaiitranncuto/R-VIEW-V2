# Tool Review Phim

Ứng dụng Flask hỗ trợ phân tích video bằng Gemini, tạo lời thuyết minh AI,
phụ đề động và dựng video review bằng FFmpeg.

## Cài đặt

Yêu cầu Python 3.10+ và FFmpeg.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Sau đó mở `http://127.0.0.1:5000` và nhập Gemini API key trên giao diện.

Muốn tự động sao chép video sau khi render, sao chép
`app_settings.example.json` thành `app_settings.json`, rồi đặt
`export_directory` tới thư mục đích.

## Cấu trúc

```text
app.py                 Flask API và luồng phân tích Gemini
recap_pipeline.py      Chia 3 phần, kiểm tra và sửa chọn lọc theo scene ID
render_worker.py       Worker render chạy tách khỏi web server
video_renderer.py      Pipeline FFmpeg và đóng gói CapCut
tts_engine.py          Tạo giọng đọc, timing và phụ đề
event_storyboard.py    Kiểm tra TIME range và evidence reel
scene_matcher.py       Tìm cảnh phục hồi khi thiếu timestamp
task_store.py          Lưu trạng thái/payload của render task
process_utils.py       Chính sách subprocess trên Windows
templates/             Giao diện HTML
static/                CSS, JavaScript và kết quả render
tests/                 Bộ kiểm thử tự động
docs/                  Tài liệu kiến trúc
```

Video nguồn được đặt ở thư mục gốc để giữ tương thích với luồng hiện tại.
Cache, log, task và thư mục tạm được tạo tự động khi cần và không được lưu
trong Git.

## Kiểm thử

```powershell
python -m unittest discover -s tests -v
```

## Lưu ý

- Không commit API key, `app_settings.json`, video nguồn hoặc kết quả render.
- Tool tự chuyển tên video tải lên sang ASCII để tránh lỗi codec trên Windows.
- Timeline dựng dùng audio làm trục chính và TIME START–END làm vùng neo hình ảnh.
- Tài liệu pipeline chi tiết nằm tại `docs/ARCHITECTURE.md`.
