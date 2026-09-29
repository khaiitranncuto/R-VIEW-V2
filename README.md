# Tool Review Phim

Ứng dụng Flask hỗ trợ phân tích video, tạo lời thuyết minh AI, phụ đề động và dựng video review bằng FFmpeg.

## Cài đặt

Yêu cầu Python 3.10+ và FFmpeg.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Sau đó mở địa chỉ được Flask hiển thị trong trình duyệt và nhập Gemini API key trên giao diện.

Nếu muốn tự động sao chép video sau khi render, sao chép `app_settings.example.json` thành `app_settings.json` rồi sửa `export_directory`.

## Kiểm thử timeline

```powershell
python -m unittest test_timeline.py -v
```

## Lưu ý

- Video nguồn, audio, ảnh, cache và file render không được lưu trong Git.
- API key được nhập từ giao diện; không commit API key hoặc file `.env`.
- Cơ chế dựng hiện dùng audio làm timeline chính và Gemini START–END làm vùng neo hình ảnh.
