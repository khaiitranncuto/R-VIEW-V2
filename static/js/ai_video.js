document.addEventListener('DOMContentLoaded', () => {
    // DOM Elements
    const apiKeyInput = document.getElementById('api-key-input');
    const btnToggleApiKey = document.getElementById('btn-toggle-api-key');
    const btnVerifyApi = document.getElementById('btn-verify-api');
    const apiStatusBadge = document.getElementById('api-status-badge');
    
    const scriptText = document.getElementById('script-text');
    const stylePrompt = document.getElementById('style-prompt');
    const styleChips = document.querySelectorAll('.style-chip');
    
    const aspectRatio = document.getElementById('aspect-ratio');
    const voiceSelect = document.getElementById('voice-select');
    
    const btnStartRender = document.getElementById('btn-start-render');
    const renderProgressBox = document.getElementById('render-progress-box');
    const renderStatusLbl = document.getElementById('render-status-lbl');
    const renderStatusPercent = document.getElementById('render-status-percent');
    const renderProgressFill = document.getElementById('render-progress-fill');
    
    const videoPreviewPlaceholder = document.getElementById('video-preview-placeholder');
    const videoPreviewWrapper = document.getElementById('video-preview-wrapper');
    const finalVideoPlayer = document.getElementById('final-video-player');
    const btnDownloadVideo = document.getElementById('btn-download-video');
    const exportDirectoryDisplay = document.getElementById('export-directory-display');
    const btnChooseExportDirectory = document.getElementById('btn-choose-export-directory');
    const toastContainer = document.getElementById('toast-container');

    // Toast helper
    function showToast(message, type = 'success') {
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.innerHTML = `
            <i class="fa-solid ${type === 'success' ? 'fa-circle-check text-green' : 'fa-circle-exclamation text-red'}"></i>
            <span>${message}</span>
        `;
        toastContainer.appendChild(toast);
        setTimeout(() => {
            toast.style.animation = 'slideIn 0.3s reverse forwards';
            setTimeout(() => toast.remove(), 300);
        }, 4000);
    }

    async function loadExportDirectory() {
        try {
            const res = await fetch('/api/export-directory');
            const data = await res.json();
            exportDirectoryDisplay.value = data.directory || '';
        } catch (_) {}
    }

    btnChooseExportDirectory.addEventListener('click', async () => {
        btnChooseExportDirectory.disabled = true;
        try {
            const res = await fetch('/api/export-directory', { method: 'POST' });
            const data = await res.json();
            if (data.success) {
                exportDirectoryDisplay.value = data.directory;
                showToast('Đã lưu thư mục xuất cho các lần sau.', 'success');
            } else if (!data.cancelled) {
                showToast(data.message || 'Không thể chọn thư mục.', 'error');
            }
        } catch (_) {
            showToast('Không thể mở hộp chọn thư mục.', 'error');
        } finally {
            btnChooseExportDirectory.disabled = false;
        }
    });
    loadExportDirectory();

    // API Key Logic
    const savedKey = localStorage.getItem('gemini_api_key');
    if (savedKey) {
        apiKeyInput.value = savedKey;
        // Optionally auto-verify if needed
        apiStatusBadge.querySelector('.indicator').className = 'indicator green';
        apiStatusBadge.querySelector('.status-text').textContent = 'API Sẵn Sàng (Từ bộ nhớ)';
    }

    btnToggleApiKey.addEventListener('click', () => {
        const type = apiKeyInput.type === 'password' ? 'text' : 'password';
        apiKeyInput.type = type;
        btnToggleApiKey.querySelector('i').className = type === 'password' ? 'fa-solid fa-eye' : 'fa-solid fa-eye-slash';
    });

    btnVerifyApi.addEventListener('click', async () => {
        const key = apiKeyInput.value.trim();
        if (!key) {
            showToast('Vui lòng nhập API Key!', 'error');
            return;
        }
        btnVerifyApi.disabled = true;
        btnVerifyApi.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Đang xác thực...';
        
        try {
            const res = await fetch('/api/check-key', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ api_key: key })
            });
            const data = await res.json();
            
            if (data.valid) {
                localStorage.setItem('gemini_api_key', key);
                apiStatusBadge.querySelector('.indicator').className = 'indicator green';
                apiStatusBadge.querySelector('.status-text').textContent = 'API Đã Kết Nối';
                showToast('Kết nối API Gemini thành công!', 'success');
            } else {
                apiStatusBadge.querySelector('.indicator').className = 'indicator red';
                apiStatusBadge.querySelector('.status-text').textContent = 'Chưa xác thực API';
                showToast(data.message, 'error');
            }
        } catch (err) {
            showToast('Lỗi kết nối máy chủ', 'error');
        } finally {
            btnVerifyApi.disabled = false;
            btnVerifyApi.innerHTML = '<i class="fa-solid fa-circle-check"></i> Xác Thực Kết Nối API';
        }
    });

    // Style chips logic
    styleChips.forEach(chip => {
        chip.addEventListener('click', () => {
            stylePrompt.value = chip.getAttribute('data-style');
        });
    });

    // Render logic
    function pollTaskStatus(taskId) {
        let errorCount = 0;
        const timer = setInterval(async () => {
            try {
                const res = await fetch(`/api/task-status/${taskId}`);
                const data = await res.json();
                
                if (data.status === 'success') {
                    clearInterval(timer);
                    renderStatusPercent.textContent = `100%`;
                    renderProgressFill.style.width = `100%`;
                    renderStatusLbl.textContent = data.message;
                    
                    // Show video
                    videoPreviewPlaceholder.classList.add('hidden');
                    videoPreviewWrapper.classList.remove('hidden');
                    finalVideoPlayer.src = data.result;
                    btnDownloadVideo.href = data.result;
                    if (data.export_path) {
                        showToast('Đã lưu thêm vào: ' + data.export_path, 'success');
                    } else if (data.export_error) {
                        showToast('Video đã tạo nhưng chưa chép được: ' + data.export_error, 'error');
                    }
                    
                    btnStartRender.disabled = false;
                    btnStartRender.innerHTML = '<i class="fa-solid fa-circle-play"></i> BẮT ĐẦU TẠO VIDEO AI';
                    showToast('Đã tạo video thành công!', 'success');
                } else if (data.status === 'failed' || data.status === 'not_found') {
                    clearInterval(timer);
                    showToast(data.message || 'Lỗi hệ thống', 'error');
                    btnStartRender.disabled = false;
                    btnStartRender.innerHTML = '<i class="fa-solid fa-circle-play"></i> BẮT ĐẦU TẠO VIDEO AI';
                } else {
                    renderStatusPercent.textContent = `${data.progress}%`;
                    renderProgressFill.style.width = `${data.progress}%`;
                    renderStatusLbl.textContent = data.message;
                }
            } catch (err) {
                errorCount++;
                if (errorCount > 3) {
                    clearInterval(timer);
                    showToast('Lỗi mất kết nối mạng', 'error');
                    btnStartRender.disabled = false;
                }
            }
        }, 2000);
    }

    btnStartRender.addEventListener('click', async () => {
        const key = apiKeyInput.value.trim();
        const script = scriptText.value.trim();
        if (!key) return showToast('Bạn phải nhập API Key trước.', 'error');
        if (!script) return showToast('Vui lòng nhập kịch bản.', 'error');

        btnStartRender.disabled = true;
        btnStartRender.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> ĐANG TẠO VIDEO...';
        
        renderProgressBox.classList.remove('hidden');
        renderStatusLbl.textContent = 'Đang xếp hàng...';
        renderStatusPercent.textContent = '0%';
        renderProgressFill.style.width = '0%';

        try {
            const res = await fetch('/api/generate-ai-video', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    api_key: key,
                    script_text: script,
                    style_prompt: stylePrompt.value,
                    voice_name: voiceSelect.value,
                    aspect_ratio: aspectRatio.value
                })
            });
            const data = await res.json();
            
            if (data.success) {
                pollTaskStatus(data.task_id);
            } else {
                showToast(data.message, 'error');
                btnStartRender.disabled = false;
                btnStartRender.innerHTML = '<i class="fa-solid fa-circle-play"></i> BẮT ĐẦU TẠO VIDEO AI';
            }
        } catch (err) {
            showToast('Lỗi gửi yêu cầu', 'error');
            btnStartRender.disabled = false;
            btnStartRender.innerHTML = '<i class="fa-solid fa-circle-play"></i> BẮT ĐẦU TẠO VIDEO AI';
        }
    });
});
