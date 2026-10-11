// =====================================================================
// GLOBAL APP STATE
// =====================================================================
const state = {
    apiKey: '',
    apiKeys: [],
    apiVerified: false,
    selectedVideo: '',
    selectedBgm: '',
    bgmVolume: 12,
    modelName: 'gemini-3.1-flash-lite',
    script: '',
    sentences: [],
    voiceName: 'vi-VN-HoaiMyNeural',
    voiceRate: '+10%',
    projectId: '',
    scriptProjectId: '',
    scriptVideo: '',
    splitScriptSnapshot: '',
    highestStepReached: 1
};

// =====================================================================
// DOM ELEMENTS REFERENCE
// =====================================================================
const dom = {
    // Stepper
    steps: document.querySelectorAll('.step'),
    stepLines: document.querySelectorAll('.step-line'),
    panels: document.querySelectorAll('.wizard-panel'),
    
    // Step 1
    apiKeyInput: document.getElementById('api-key-input'),
    apiKeyInput2: document.getElementById('api-key-input-2'),
    apiKeyInput3: document.getElementById('api-key-input-3'),
    btnToggleApiKey: document.getElementById('btn-toggle-api-key'),
    btnExpandApiKeys: document.getElementById('btn-expand-api-keys'),
    additionalApiKeys: document.getElementById('additional-api-keys'),
    apiKeysInlineError: document.getElementById('api-keys-inline-error'),
    apiKeysGroup: document.getElementById('api-keys-group'),
    apiPriorityButtons: document.querySelectorAll('.btn-api-priority'),
    modelSelect: document.getElementById('model-select'),
    btnVerifyApi: document.getElementById('btn-verify-api'),
    apiStatusBadge: document.getElementById('api-status-badge'),
    videoSelect: document.getElementById('video-select'),
    videoDropZone: document.getElementById('video-drop-zone'),
    videoFileInput: document.getElementById('video-file-input'),
    videoUploadProgress: document.getElementById('video-upload-progress'),
    btnToStep2: document.getElementById('btn-to-step2'),
    
    // Step 2
    styleSelect: document.getElementById('style-select'),
    customPrompt: document.getElementById('custom-prompt'),
    btnGenerateScript: document.getElementById('btn-generate-script'),
    scriptEditor: document.getElementById('script-editor'),
    scriptCharCount: document.getElementById('script-char-count'),
    scriptGenLoading: document.getElementById('script-gen-loading'),
    scriptLoadingTitle: document.getElementById('script-loading-title'),
    scriptProgressFill: document.getElementById('script-progress-fill'),
    scriptLoadingMsg: document.getElementById('script-loading-msg'),
    btnToStep3: document.getElementById('btn-to-step3'),
    
    // Step 3
    voiceSelect: document.getElementById('voice-select'),
    voiceRateSlider: document.getElementById('voice-rate-slider'),
    voiceRateVal: document.getElementById('voice-rate-val'),
    btnReSplit: document.getElementById('btn-re-split'),
    btnAddSentence: document.getElementById('btn-add-sentence'),
    sentencesList: document.getElementById('sentences-list'),
    btnToStep4: document.getElementById('btn-to-step4'),
    
    // Step 4
    bgmSelect: document.getElementById('bgm-select'),
    bgmVolumeSlider: document.getElementById('bgm-volume-slider'),
    bgmVolumeVal: document.getElementById('bgm-volume-val'),
    bgmDropZone: document.getElementById('bgm-drop-zone'),
    bgmFileInput: document.getElementById('bgm-file-input'),
    bgmUploadProgress: document.getElementById('bgm-upload-progress'),
    filterMirror: document.getElementById('filter-mirror'),
    filterZoom: document.getElementById('filter-zoom'),
    filterKenBurns: document.getElementById('filter-ken-burns'),
    filterContrast: document.getElementById('filter-contrast'),
    filterSpeed: document.getElementById('filter-speed'),
    subSize: document.getElementById('sub-size'),
    subColor: document.getElementById('sub-color'),
    subOpacity: document.getElementById('sub-opacity'),
    btnToStep5: document.getElementById('btn-to-step5'),
    
    // Step 5
    summaryVideo: document.getElementById('summary-video'),
    summarySentences: document.getElementById('summary-sentences'),
    summaryVoice: document.getElementById('summary-voice'),
    summaryBgm: document.getElementById('summary-bgm'),
    summaryFilters: document.getElementById('summary-filters'),
    btnStartRender: document.getElementById('btn-start-render'),
    renderProgressBox: document.getElementById('render-progress-box'),
    renderStatusLbl: document.getElementById('render-status-lbl'),
    renderStatusPercent: document.getElementById('render-status-percent'),
    renderProgressFill: document.getElementById('render-progress-fill'),
    renderLogTerminal: document.getElementById('render-log-terminal'),
    videoPreviewPlaceholder: document.getElementById('video-preview-placeholder'),
    videoPreviewWrapper: document.getElementById('video-preview-wrapper'),
    finalVideoPlayer: document.getElementById('final-video-player'),
    btnDownloadVideo: document.getElementById('btn-download-video'),
    btnRenderBack: document.getElementById('btn-render-back'),
    exportDirectoryDisplay: document.getElementById('export-directory-display'),
    btnChooseExportDirectory: document.getElementById('btn-choose-export-directory'),
    
    // Commons
    backButtons: document.querySelectorAll('.btn-back'),
    toastContainer: document.getElementById('toast-container')
};

// =====================================================================
// HELPER FUNCTIONS
// =====================================================================
function showToast(message, type = 'success') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `
        <i class="fa-solid ${type === 'success' ? 'fa-circle-check text-green' : 'fa-circle-exclamation text-red'}"></i>
        <span>${message}</span>
    `;
    dom.toastContainer.appendChild(toast);
    
    // Auto remove after 4 seconds
    setTimeout(() => {
        toast.style.animation = 'slideIn 0.3s reverse forwards';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

function createProjectId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return window.crypto.randomUUID();
    }
    return `project-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function resetProjectData(nextVideo = '') {
    state.projectId = createProjectId();
    state.selectedVideo = nextVideo;
    state.script = '';
    state.sentences = [];
    state.scriptProjectId = '';
    state.scriptVideo = '';
    state.splitScriptSnapshot = '';
    state.highestStepReached = Math.min(state.highestStepReached, 2);

    if (dom.scriptEditor) dom.scriptEditor.value = '';
    if (dom.scriptCharCount) dom.scriptCharCount.textContent = '0 từ';
    if (dom.sentencesList) dom.sentencesList.innerHTML = '';
    if (dom.renderProgressBox) dom.renderProgressBox.classList.add('hidden');
    if (dom.renderLogTerminal) dom.renderLogTerminal.innerHTML = '';
    if (dom.videoPreviewWrapper) dom.videoPreviewWrapper.classList.add('hidden');
    if (dom.videoPreviewPlaceholder) dom.videoPreviewPlaceholder.classList.remove('hidden');
    if (dom.finalVideoPlayer) {
        dom.finalVideoPlayer.pause();
        dom.finalVideoPlayer.removeAttribute('src');
        dom.finalVideoPlayer.load();
    }
    if (dom.btnDownloadVideo) dom.btnDownloadVideo.removeAttribute('href');
    if (dom.btnStartRender) {
        dom.btnStartRender.disabled = false;
        dom.btnStartRender.innerHTML = '<i class="fa-solid fa-circle-play"></i> BẮT ĐẦU XUẤT VIDEO REVIEW';
    }
    if (dom.btnRenderBack) dom.btnRenderBack.disabled = false;
    stopRenderTimer();
}

async function loadExportDirectory() {
    try {
        const res = await fetch('/api/export-directory');
        const data = await res.json();
        if (dom.exportDirectoryDisplay) dom.exportDirectoryDisplay.value = data.directory || '';
        if (data.directory && data.valid === false && data.message) {
            showToast(data.message, 'error');
        }
    } catch (_) {
        // Export directory is optional; normal rendering can continue.
    }
}

async function chooseExportDirectory() {
    if (!dom.btnChooseExportDirectory) return;
    dom.btnChooseExportDirectory.disabled = true;
    try {
        const res = await fetch('/api/export-directory', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            dom.exportDirectoryDisplay.value = data.directory;
            showToast('Đã lưu thư mục xuất. Video sau khi render sẽ tự chép vào đây.', 'success');
        } else if (!data.cancelled) {
            showToast(data.message || 'Không thể chọn thư mục xuất.', 'error');
        }
    } catch (_) {
        showToast('Không thể mở hộp chọn thư mục.', 'error');
    } finally {
        dom.btnChooseExportDirectory.disabled = false;
    }
}

function updateAPIIndicator(isValid, message) {
    state.apiVerified = Boolean(isValid);
    const indicator = dom.apiStatusBadge.querySelector('.indicator');
    const statusText = dom.apiStatusBadge.querySelector('.status-text');
    
    if (isValid) {
        indicator.className = 'indicator green';
        statusText.textContent = 'API Đã Kết Nối';
        dom.apiStatusBadge.style.background = 'rgba(16, 185, 129, 0.08)';
        dom.apiStatusBadge.style.borderColor = 'rgba(16, 185, 129, 0.3)';
    } else {
        indicator.className = 'indicator red';
        statusText.textContent = 'Chưa xác thực API';
        dom.apiStatusBadge.style.background = 'rgba(239, 68, 68, 0.08)';
        dom.apiStatusBadge.style.borderColor = 'rgba(239, 68, 68, 0.3)';
    }
}

function updateCharCount() {
    const text = dom.scriptEditor.value.trim();
    const wordCount = text ? text.split(/\s+/).filter(w => w.length > 0).length : 0;
    // TTS doc khoang 150 tu/phut
    const estMinutes = (wordCount / 150).toFixed(1);
    dom.scriptCharCount.textContent = `${wordCount} từ (~${estMinutes} phút)`;
}

// =====================================================================
// STEP NAVIGATION LOGIC
// =====================================================================
function switchPanel(stepNum) {
    if (stepNum > state.highestStepReached) return;
    
    // Mở Panel tương ứng
    dom.panels.forEach(p => p.classList.remove('active'));
    document.getElementById(`panel-${stepNum}`).classList.remove('hidden');
    document.getElementById(`panel-${stepNum}`).classList.add('active');
    
    // Cập nhật Stepper CSS
    dom.steps.forEach(step => {
        const sNum = parseInt(step.getAttribute('data-step'));
        step.classList.remove('active', 'completed');
        if (sNum === stepNum) {
            step.classList.add('active');
        } else if (sNum < stepNum) {
            step.classList.add('completed');
        }
    });
    
    dom.stepLines.forEach((line, idx) => {
        line.classList.remove('completed');
        if (idx + 1 < stepNum) {
            line.classList.add('completed');
        }
    });
    
    // Một số logic chuẩn bị khi vào bước cụ thể
    if (stepNum === 3) {
        // Tự động phân tách kịch bản thành câu nếu câu đang trống
        if (state.sentences.length === 0) {
            autoSplitScript();
        }
    } else if (stepNum === 5) {
        updateRenderSummary();
    }
}

function enableStep(stepNum) {
    if (stepNum > state.highestStepReached) {
        state.highestStepReached = stepNum;
    }
    // Kích hoạt click sự kiện
    const stepEl = document.querySelector(`.step[data-step="${stepNum}"]`);
    if (stepEl) {
        stepEl.style.cursor = 'pointer';
    }
}

// =====================================================================
// INITIAL LOADING & API TASKS
// =====================================================================
function apiKeySlotInputs() {
    return [dom.apiKeyInput, dom.apiKeyInput2, dom.apiKeyInput3];
}

function setApiKeySlotValues(keys) {
    const padded = [...keys.slice(0, 3), '', '', ''].slice(0, 3);
    apiKeySlotInputs().forEach((input, index) => {
        if (input) input.value = padded[index];
    });
}

function validateApiKeySlots() {
    const inputs = apiKeySlotInputs();
    const values = inputs.map(input => input ? input.value.trim() : '');
    inputs.forEach(input => input && input.classList.remove('api-key-duplicate'));

    let error = '';
    if (!values[0] && (values[1] || values[2])) {
        error = 'API 1 không được để trống khi có API dự phòng.';
    } else if (!values[1] && values[2]) {
        error = 'Hãy nhập API 2 trước khi dùng API 3.';
    }

    const firstIndexByKey = new Map();
    values.forEach((key, index) => {
        if (!key) return;
        if (firstIndexByKey.has(key)) {
            const firstIndex = firstIndexByKey.get(key);
            inputs[firstIndex].classList.add('api-key-duplicate');
            inputs[index].classList.add('api-key-duplicate');
            if (!error) error = `API ${index + 1} bị trùng với API ${firstIndex + 1}.`;
        } else {
            firstIndexByKey.set(key, index);
        }
    });

    if (dom.apiKeysInlineError) {
        dom.apiKeysInlineError.textContent = error;
        dom.apiKeysInlineError.classList.toggle('hidden', !error);
    }
    return { valid: !error, keys: values.filter(Boolean), error };
}

function collectApiKeys() {
    const result = validateApiKeySlots();
    if (!result.valid) return null;
    return result.keys;
}

function persistOrderedApiKeys(keys) {
    localStorage.setItem('gemini_api_keys', JSON.stringify(keys));
    if (keys[0]) localStorage.setItem('gemini_api_key', keys[0]);
    else localStorage.removeItem('gemini_api_key');
}

function prioritizeApiSlot(slotIndex) {
    const validation = validateApiKeySlots();
    if (!validation.valid) {
        showToast(validation.error, 'error');
        return;
    }
    const values = apiKeySlotInputs().map(input => input ? input.value.trim() : '');
    const selected = values[slotIndex];
    if (!selected) {
        showToast(`API ${slotIndex + 1} đang trống.`, 'error');
        return;
    }
    if (slotIndex === 0) {
        showToast('API 1 đã là ưu tiên cao nhất.', 'success');
        return;
    }

    const oldPrimary = values[0];
    const middle = values
        .slice(1)
        .filter((key, offset) => offset + 1 !== slotIndex && key);
    const ordered = [selected, ...middle];
    if (oldPrimary && oldPrimary !== selected) ordered.push(oldPrimary);
    setApiKeySlotValues(ordered);
    state.apiKeys = ordered;
    state.apiKey = ordered[0];
    if (state.apiVerified) persistOrderedApiKeys(ordered);
    validateApiKeySlots();
    setAdditionalApiKeysExpanded(ordered.length > 1);
    showToast(`Đã đặt API ${slotIndex + 1} lên ưu tiên số 1.`, 'success');
}

function setAdditionalApiKeysExpanded(expanded) {
    if (!dom.additionalApiKeys || !dom.btnExpandApiKeys) return;
    dom.additionalApiKeys.classList.toggle('hidden', !expanded);
    if (dom.apiKeysGroup) dom.apiKeysGroup.classList.toggle('api-keys-expanded', expanded);
    dom.btnExpandApiKeys.classList.toggle('expanded', expanded);
    dom.btnExpandApiKeys.setAttribute('aria-expanded', expanded ? 'true' : 'false');
}

async function loadInitialData() {
    // Load API key tu localStorage neu co
    let savedKeys = [];
    try {
        const parsed = JSON.parse(localStorage.getItem('gemini_api_keys') || '[]');
        if (Array.isArray(parsed)) savedKeys = parsed.slice(0, 3);
    } catch (_) {}
    if (!savedKeys.length) {
        const savedKey = localStorage.getItem('gemini_api_key');
        if (savedKey) savedKeys = [savedKey];
    }
    [dom.apiKeyInput, dom.apiKeyInput2, dom.apiKeyInput3].forEach((input, index) => {
        if (input && savedKeys[index]) input.value = savedKeys[index];
    });
    state.apiKeys = collectApiKeys() || [];
    state.apiKey = state.apiKeys[0] || '';
    
    // Tai danh sach video va nhac nen co san
    await loadVideosList();
    await loadBgmsList();
    
    // Tu dong kiem tra API Key neu co san
    if (state.apiKey) {
        verifyApiKey(false);
    }
}

async function verifyApiKey(showSuccessToast = true) {
    const apiKeys = collectApiKeys();
    if (apiKeys === null) {
        showToast(dom.apiKeysInlineError.textContent || 'Danh sách API không hợp lệ.', 'error');
        return false;
    }
    if (!apiKeys.length) {
        showToast('Vui lòng nhập API Key!', 'error');
        return false;
    }
    state.apiKeys = apiKeys;
    state.apiKey = apiKeys[0];
    
    dom.btnVerifyApi.disabled = true;
    dom.btnVerifyApi.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Đang xác thực...';
    
    try {
        let primaryData = null;
        for (let index = 0; index < apiKeys.length; index++) {
            dom.btnVerifyApi.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Xác thực API ${index + 1}/${apiKeys.length}...`;
            const res = await fetch('/api/check-key', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ api_key: apiKeys[index] })
            });
            const data = await res.json();
            if (!data.valid) {
                updateAPIIndicator(false);
                showToast(`API ${index + 1}: ${data.message}`, 'error');
                return false;
            }
            if (index === 0) primaryData = data;
        }

        if (primaryData) {
            updateAPIIndicator(true);
            persistOrderedApiKeys(apiKeys);
            if (showSuccessToast) {
                showToast(`Đã xác thực ${apiKeys.length} Gemini API theo thứ tự ưu tiên.`, 'success');
            }
            
            // Auto-populate model dropdown tu API
            if (primaryData.models && primaryData.models.length > 0) {
                const modelSelect = dom.modelSelect;
                const currentVal = modelSelect.value;
                modelSelect.innerHTML = '';
                
                // Prefer the full Flash model for long-form story coherence.
                const preferred = ['gemini-3.5-flash', 'gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.6-flash', 'gemini-3-flash-preview', 'gemini-3.5-flash-lite', 'gemini-3.1-flash-lite'];
                const sorted = primaryData.models.sort((a, b) => {
                    const aIdx = preferred.indexOf(a);
                    const bIdx = preferred.indexOf(b);
                    if (aIdx !== -1 && bIdx !== -1) return aIdx - bIdx;
                    if (aIdx !== -1) return -1;
                    if (bIdx !== -1) return 1;
                    return a.localeCompare(b);
                });
                
                sorted.forEach((m, i) => {
                    const opt = document.createElement('option');
                    opt.value = m;
                    let label = m;
                    if (m.includes('flash-lite')) label += ' (Nhẹ, nhanh)';
                    else if (m.includes('flash')) label += ' (Nhanh)';
                    else if (m.includes('pro')) label += ' (Thông minh)';
                    if (i === 0) label += ' ⭐ Khuyên dùng';
                    opt.textContent = label;
                    if (m === currentVal) opt.selected = true;
                    modelSelect.appendChild(opt);
                });
                
                // Neu ko co current value, chon cai dau tien
                if (!currentVal || !sorted.includes(currentVal)) {
                    modelSelect.selectedIndex = 0;
                }
                console.log(`[+] Loaded ${sorted.length} models from API`);
            }
            
            checkNextStep1Availability();
            return true;
        }
    } catch (err) {
        updateAPIIndicator(false);
        showToast('Không thể kết nối đến máy chủ backend.', 'error');
        return false;
    } finally {
        dom.btnVerifyApi.disabled = false;
        dom.btnVerifyApi.innerHTML = '<i class="fa-solid fa-circle-check"></i> Xác Thực Kết Nối API';
    }
}

async function loadVideosList() {
    try {
        const previousVideo = state.selectedVideo || dom.videoSelect.value;
        const res = await fetch('/api/list-videos');
        const list = await res.json();
        
        dom.videoSelect.innerHTML = '<option value="" disabled selected>-- Chọn video của bạn --</option>';
        list.forEach(vid => {
            const opt = document.createElement('option');
            opt.value = vid;
            opt.textContent = vid;
            dom.videoSelect.appendChild(opt);
        });
        
        const automaticVideo = list.includes(previousVideo)
            ? previousVideo
            : (list.length === 1 ? list[0] : (list.includes('video_goc.mp4') ? 'video_goc.mp4' : ''));
        if (automaticVideo) {
            dom.videoSelect.value = automaticVideo;
            if (automaticVideo !== state.selectedVideo) resetProjectData(automaticVideo);
            checkNextStep1Availability();
        }
    } catch (err) {
        showToast('Không thể tải danh sách video.', 'error');
    }
}

async function loadBgmsList() {
    try {
        const res = await fetch('/api/list-bgm');
        const list = await res.json();
        
        dom.bgmSelect.innerHTML = '<option value="">Không dùng nhạc nền (Chỉ giữ giọng thuyết minh)</option>';
        list.forEach(bgm => {
            const opt = document.createElement('option');
            opt.value = bgm;
            opt.textContent = bgm;
            dom.bgmSelect.appendChild(opt);
        });
    } catch (err) {
        showToast('Không thể tải danh sách nhạc nền BGM.', 'error');
    }
}

async function loadSfxList() {
    try {
        const res = await fetch('/api/list-sfx');
        const list = await res.json();
        const container = document.querySelector('.prompt-templates:has(.sfx-chip)');
        if (container) {
            container.innerHTML = '';
            const maxVisible = 6;
            list.forEach((sfx, index) => {
                const btn = document.createElement('button');
                btn.className = 'prompt-chip sfx-chip';
                btn.setAttribute('data-prompt', `[SFX=${sfx}]`);
                // Format ten dep hon
                let displayName = sfx.replace(/\.(mp3|wav|m4a|aac|ogg)$/i, '');
                // Gioi han 15 ky tu cho dep
                if (displayName.length > 20) displayName = displayName.substring(0, 18) + '...';
                
                btn.innerHTML = `<i class="fa-solid fa-music"></i> ${displayName}`;
                
                if (index >= maxVisible) {
                    btn.style.display = 'none';
                    btn.classList.add('sfx-hidden');
                }
                
                btn.addEventListener('click', () => {
                    if (dom.scriptEditor) {
                        const promptText = `[SFX=${sfx}]`;
                        const startPos = dom.scriptEditor.selectionStart;
                        const endPos = dom.scriptEditor.selectionEnd;
                        const val = dom.scriptEditor.value;
                        
                        if (startPos !== undefined && startPos !== null) {
                            dom.scriptEditor.value = val.substring(0, startPos) + " " + promptText + " " + val.substring(endPos);
                            dom.scriptEditor.focus();
                            const newPos = startPos + promptText.length + 2;
                            dom.scriptEditor.selectionStart = newPos;
                            dom.scriptEditor.selectionEnd = newPos;
                        } else {
                            dom.scriptEditor.value += " " + promptText;
                        }
                        updateCharCount();
                        
                        btn.style.borderColor = 'var(--accent-purple)';
                        btn.style.color = 'var(--accent-purple)';
                        setTimeout(() => {
                            btn.style.borderColor = '';
                            btn.style.color = '';
                        }, 800);
                        showToast('Đã chèn SFX vào kịch bản!', 'success');
                    }
                });
                container.appendChild(btn);
            });
            
            if (list.length > maxVisible) {
                const toggleBtn = document.createElement('button');
                toggleBtn.className = 'prompt-chip sfx-toggle-btn';
                toggleBtn.style.background = 'rgba(255,255,255,0.05)';
                toggleBtn.style.borderStyle = 'dashed';
                toggleBtn.innerHTML = `<i class="fa-solid fa-chevron-down"></i> Xem thêm (${list.length - maxVisible})`;
                
                toggleBtn.addEventListener('click', () => {
                    const hiddenBtns = container.querySelectorAll('.sfx-hidden');
                    const isExpanded = toggleBtn.classList.contains('expanded');
                    
                    if (isExpanded) {
                        hiddenBtns.forEach(b => b.style.display = 'none');
                        toggleBtn.innerHTML = `<i class="fa-solid fa-chevron-down"></i> Xem thêm (${list.length - maxVisible})`;
                        toggleBtn.classList.remove('expanded');
                    } else {
                        hiddenBtns.forEach(b => b.style.display = 'inline-flex');
                        toggleBtn.innerHTML = `<i class="fa-solid fa-chevron-up"></i> Thu gọn`;
                        toggleBtn.classList.add('expanded');
                    }
                });
                container.appendChild(toggleBtn);
            }
        }
    } catch (err) {
        console.error("Loi load SFX: ", err);
    }
}

function checkNextStep1Availability() {
    const isReady = state.apiVerified && Boolean(dom.videoSelect.value);
    dom.btnToStep2.disabled = false;
    dom.btnToStep2.setAttribute('aria-disabled', isReady ? 'false' : 'true');
    dom.btnToStep2.classList.toggle('is-ready', isReady);
    if (isReady) {
        enableStep(2);
    }
}

async function continueToScriptStep() {
    const selectedVideo = dom.videoSelect.value;
    if (!selectedVideo) {
        showToast('Hãy chọn video trong danh sách hoặc tải video mới lên trước.', 'error');
        dom.videoSelect.focus();
        return;
    }
    if (!dom.apiKeyInput.value.trim()) {
        showToast('Hãy nhập API Key Gemini trước.', 'error');
        dom.apiKeyInput.focus();
        return;
    }
    if (!state.apiVerified) {
        const verified = await verifyApiKey(false);
        if (!verified) return;
    }
    if (selectedVideo !== state.selectedVideo) resetProjectData(selectedVideo);
    state.selectedVideo = selectedVideo;
    enableStep(2);
    switchPanel(2);
}

// =====================================================================
// FILE UPLOAD HANDLING (DRAG & DROP)
// =====================================================================
function setupDragAndDrop(dropZone, fileInput, progressBox, type, callback) {
    dropZone.addEventListener('click', () => fileInput.click());
    
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('dragover');
    });
    
    dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('dragover');
    });
    
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('dragover');
        if (e.dataTransfer.files.length > 0) {
            handleFileUpload(e.dataTransfer.files[0], progressBox, type, callback);
        }
    });
    
    fileInput.addEventListener('change', () => {
        if (fileInput.files.length > 0) {
            handleFileUpload(fileInput.files[0], progressBox, type, callback);
        }
    });
}

function handleFileUpload(file, progressBox, type, callback) {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('type', type);
    
    progressBox.classList.remove('hidden');
    const fill = progressBox.querySelector('.progress-fill');
    const percent = progressBox.querySelector('.progress-percent');
    
    fill.style.width = '0%';
    percent.textContent = '0%';
    
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload-file', true);
    
    xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
            const pct = Math.round((e.loaded / e.total) * 100);
            fill.style.width = `${pct}%`;
            percent.textContent = `${pct}%`;
        }
    };
    
    xhr.onload = () => {
        progressBox.classList.add('hidden');
        if (xhr.status === 200) {
            const res = JSON.parse(xhr.responseText);
            if (res.success) {
                const renameNote = res.renamed ? ` (dã dổi tên: ${res.filename})` : '';
                showToast(`Tải lên tệp '${file.name}' thành công!${renameNote}`, 'success');
                callback(res.filename);
            } else {
                showToast(res.message, 'error');
            }
        } else {
            showToast('Lỗi khi tải file lên máy chủ.', 'error');
        }
    };
    
    xhr.onerror = () => {
        progressBox.classList.add('hidden');
        showToast('Kết nối tải tệp bị gián đoạn.', 'error');
    };
    
    xhr.send(formData);
}

// =====================================================================
// STEP 2: AI SCRIPT GENERATOR
// =====================================================================
async function generateScript() {
    const apiKeys = collectApiKeys();
    if (apiKeys === null) {
        showToast(dom.apiKeysInlineError.textContent || 'Danh sách API không hợp lệ.', 'error');
        return;
    }
    state.apiKeys = apiKeys || [];
    state.apiKey = state.apiKeys[0] || '';
    const requestedVideo = dom.videoSelect.value;
    // Every Gemini generation is a new isolated project, even for the same file.
    resetProjectData(requestedVideo);
    state.selectedVideo = requestedVideo;
    state.modelName = dom.modelSelect.value;
    const requestedProjectId = state.projectId;
    
    const style = dom.styleSelect.value;
    const customPromptText = dom.customPrompt.value;
    
    if (!state.selectedVideo) {
        showToast('Vui lòng chọn video trước!', 'error');
        return;
    }
    
    dom.scriptGenLoading.classList.remove('hidden');
    // Invalidate every derived value from the previous Gemini result now.
    state.script = '';
    state.sentences = [];
    state.scriptProjectId = '';
    state.scriptVideo = '';
    state.splitScriptSnapshot = '';
    dom.scriptEditor.value = '';
    dom.sentencesList.innerHTML = '';
    updateCharCount();
    updateScriptLoading(0, 'Đang gửi yêu cầu khởi tạo đến máy chủ...', 'Kết nối Gemini...');
    
    try {
        const res = await fetch('/api/analyze-video', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                api_key: state.apiKey,
                api_keys: state.apiKeys,
                model_name: state.modelName,
                video_name: state.selectedVideo,
                project_id: requestedProjectId,
                style: style,
                custom_prompt: customPromptText,
                enable_multi_voice: document.getElementById('enable-multi-voice') ? document.getElementById('enable-multi-voice').checked : false
            })
        });
        const data = await res.json();
        
        if (data.success) {
            // Bắt đầu Polling tiến trình AI
            pollTaskStatus(data.task_id, (statusData) => {
                if (state.projectId !== requestedProjectId || state.selectedVideo !== requestedVideo) return;
                updateScriptLoading(statusData.progress, statusData.message, 'AI Phân Tích Khung Hình');
            }, (scriptResult, taskData) => {
                if (state.projectId !== requestedProjectId || state.selectedVideo !== requestedVideo) {
                    return;
                }
                dom.scriptGenLoading.classList.add('hidden');
                dom.scriptEditor.value = scriptResult;
                state.script = scriptResult;
                state.sentences = [];
                state.splitScriptSnapshot = '';
                state.scriptProjectId = taskData.project_id || requestedProjectId;
                state.scriptVideo = taskData.video_name || requestedVideo;
                updateCharCount();
                showToast('AI viết kịch bản hoàn tất!', 'success');
                enableStep(3);
            }, (errMessage) => {
                if (state.projectId !== requestedProjectId || state.selectedVideo !== requestedVideo) return;
                dom.scriptGenLoading.classList.add('hidden');
                showToast(errMessage, 'error');
            });
        } else {
            dom.scriptGenLoading.classList.add('hidden');
            showToast(data.message, 'error');
        }
    } catch (err) {
        dom.scriptGenLoading.classList.add('hidden');
        showToast('Lỗi gửi yêu cầu viết kịch bản.', 'error');
    }
}

function updateScriptLoading(progress, message, title) {
    dom.scriptLoadingTitle.textContent = title;
    dom.scriptProgressFill.style.width = `${progress}%`;
    dom.scriptLoadingMsg.textContent = message;
}

function updateCharCount() {
    const text = dom.scriptEditor.value.trim();
    const count = text ? text.split(/\s+/).length : 0;
    dom.scriptCharCount.textContent = `${count} từ`;
    state.script = text;
}

// =====================================================================
// STEP 3: SENTENCES SPLITTING & TTS PREVIEW
// =====================================================================
async function autoSplitScript() {
    state.script = dom.scriptEditor.value.trim();
    if (!state.script) return;
    if (state.scriptProjectId !== state.projectId || state.scriptVideo !== state.selectedVideo) {
        showToast('Kịch bản hiện tại không thuộc video đang chọn. Hãy tạo lại bằng Gemini.', 'error');
        return;
    }
    const scriptToSplit = state.script;
    
    dom.sentencesList.innerHTML = '<div class="text-muted p-3"><i class="fa-solid fa-spinner fa-spin"></i> Đang phân tích cấu trúc kịch bản...</div>';
    
    try {
        const res = await fetch('/api/split-sentences', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: scriptToSplit })
        });
        const payload = await res.json();
        if (!res.ok) {
            throw new Error(payload.message || `HTTP ${res.status}`);
        }
        if (!Array.isArray(payload)) {
            throw new Error('Máy chủ trả dữ liệu phân đoạn không đúng định dạng.');
        }
        const sentences = payload.filter(item => typeof item === 'string' && item.trim());
        if (sentences.length === 0) {
            throw new Error('Kịch bản không tạo được phân đoạn hợp lệ.');
        }
        if (state.projectId !== state.scriptProjectId || dom.scriptEditor.value.trim() !== scriptToSplit) return;
        state.sentences = sentences;
        state.splitScriptSnapshot = scriptToSplit;
        renderSentences();
        updateSentenceStats();
        enableStep(4);
    } catch (err) {
        const message = err && err.message ? err.message : 'Lỗi không xác định';
        dom.sentencesList.innerHTML = '';
        const errorBox = document.createElement('div');
        errorBox.className = 'text-red p-3';
        errorBox.textContent = `Lỗi khi phân đoạn câu: ${message}`;
        dom.sentencesList.appendChild(errorBox);
    }
}

function updateSentenceStats() {
    const count = state.sentences.length;
    const wordCount = state.sentences.reduce((sum, s) => sum + (s.trim() ? s.trim().split(/\s+/).length : 0), 0);
    // Uoc tinh: ~150 tu/phut voi TTS tieng Viet toc do +10%
    const estMinutes = Math.max(1, Math.round(wordCount / 150));
    
    const elCount = document.getElementById('stat-sentence-count');
    const elWords = document.getElementById('stat-word-count');
    const elDuration = document.getElementById('stat-est-duration');
    if (elCount) elCount.textContent = count;
    if (elWords) elWords.textContent = wordCount;
    if (elDuration) elDuration.textContent = `~${estMinutes}p`;
}

function renderSentences() {
    dom.sentencesList.innerHTML = '';
    
    if (state.sentences.length === 0) {
        dom.sentencesList.innerHTML = '<div class="text-muted p-3">Kịch bản trống hoặc chưa được tách câu.</div>';
        return;
    }
    
    state.sentences.forEach((sentence, idx) => {
        const item = document.createElement('div');
        item.className = 'sentence-item';
        item.innerHTML = `
            <div class="sentence-index">${idx + 1}</div>
            <div class="sentence-text-wrapper">
                <textarea class="sentence-textarea" rows="2">${sentence}</textarea>
            </div>
            <div class="sentence-actions">
                <button type="button" class="btn-preview-item" title="Nghe thử giọng đọc">
                    <i class="fa-solid fa-play"></i>
                </button>
                <button type="button" class="btn-delete-item" title="Xóa câu này">
                    <i class="fa-solid fa-trash"></i>
                </button>
            </div>
        `;
        
        // Sự kiện nghe thử TTS từng câu
        const btnPlay = item.querySelector('.btn-preview-item');
        const textarea = item.querySelector('.sentence-textarea');
        
        // Dong bo nguoc lai state khi sua cau
        textarea.addEventListener('input', () => {
            state.sentences[idx] = textarea.value.trim();
            updateSentenceStats();
        });
        
        // Nut xoa cau
        const btnDelete = item.querySelector('.btn-delete-item');
        btnDelete.addEventListener('click', () => {
            state.sentences.splice(idx, 1);
            renderSentences();
            updateSentenceStats();
        });
        
        let audioPlayer = null;
        btnPlay.addEventListener('click', async () => {
            if (btnPlay.classList.contains('playing')) {
                // Đang chơi nhạc thì dừng lại
                if (audioPlayer) {
                    audioPlayer.pause();
                }
                return;
            }
            
            const txt = textarea.value.trim();
            if (!txt) return;
            
            // Đặt trạng thái loading
            btnPlay.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
            btnPlay.disabled = true;
            
            try {
                const previewRes = await fetch('/api/preview-tts', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        text: txt,
                        voice: dom.voiceSelect.value,
                        rate: `${parseInt(dom.voiceRateSlider.value) >= 0 ? '+' : ''}${dom.voiceRateSlider.value}%`
                    })
                });
                const previewData = await previewRes.json();
                
                if (previewData.success) {
                    btnPlay.innerHTML = '<i class="fa-solid fa-volume-high"></i>';
                    btnPlay.classList.add('playing');
                    btnPlay.disabled = false;
                    
                    audioPlayer = new Audio(previewData.url);
                    audioPlayer.play();
                    
                    audioPlayer.onended = () => {
                        btnPlay.classList.remove('playing');
                        btnPlay.innerHTML = '<i class="fa-solid fa-play"></i>';
                        audioPlayer = null;
                    };
                    
                    audioPlayer.onerror = () => {
                        btnPlay.classList.remove('playing');
                        btnPlay.innerHTML = '<i class="fa-solid fa-play"></i>';
                        showToast('Lỗi phát âm thanh.', 'error');
                        audioPlayer = null;
                    };
                } else {
                    btnPlay.innerHTML = '<i class="fa-solid fa-play"></i>';
                    btnPlay.disabled = false;
                    showToast(previewData.message, 'error');
                }
            } catch (err) {
                btnPlay.innerHTML = '<i class="fa-solid fa-play"></i>';
                btnPlay.disabled = false;
                showToast('Lỗi gọi API tổng hợp giọng nói.', 'error');
            }
        });
        
        dom.sentencesList.appendChild(item);
    });
}

// =====================================================================
// STEP 5: RENDER SUMMARY & VIDEO GENERATION
// =====================================================================
function updateRenderSummary() {
    dom.summaryVideo.textContent = state.selectedVideo;
    dom.summarySentences.textContent = `${state.sentences.length} câu phân cảnh`;
    
    const voiceText = dom.voiceSelect.options[dom.voiceSelect.selectedIndex].text;
    dom.summaryVoice.textContent = `${voiceText} (${dom.voiceRateSlider.value >= 0 ? '+' : ''}${dom.voiceRateSlider.value}%)`;
    
    dom.summaryBgm.textContent = dom.bgmSelect.value ? `${dom.bgmSelect.value} (Âm lượng: ${dom.bgmVolumeSlider.value}%)` : 'Không dùng';
    
    // Thu thap bo loc lach ban quyen
    const filters = [];
    if (dom.filterMirror.checked) filters.push('Lật ngang');
    if (dom.filterZoom.checked) filters.push('Zoom-in 8%');
    if (dom.filterContrast.value !== '1.0') filters.push(`Tương phản ${parseInt(parseFloat(dom.filterContrast.value)*100)}%`);
    if (dom.filterSpeed.value !== '1.0') filters.push(`Tốc độ chạy hình ${dom.filterSpeed.value}x`);
    
    dom.summaryFilters.textContent = filters.length > 0 ? filters.join(', ') : 'Không sử dụng bộ lọc';
    
    // Subtitle info
    const subSummary = document.getElementById('summary-subtitle');
    if (subSummary) {
        subSummary.textContent = `${dom.subSize.value}px, ${dom.subColor.value}, Nền mờ: ${dom.subOpacity.value}`;
    }
}

// Render timer
let renderTimerInterval = null;
let renderStartTime = null;

function startRenderTimer() {
    renderStartTime = Date.now();
    const timerEl = document.getElementById('render-timer');
    renderTimerInterval = setInterval(() => {
        const elapsed = Math.floor((Date.now() - renderStartTime) / 1000);
        const mins = Math.floor(elapsed / 60).toString().padStart(2, '0');
        const secs = (elapsed % 60).toString().padStart(2, '0');
        if (timerEl) timerEl.innerHTML = `<i class="fa-regular fa-clock"></i> ${mins}:${secs}`;
    }, 1000);
}

function stopRenderTimer() {
    if (renderTimerInterval) {
        clearInterval(renderTimerInterval);
        renderTimerInterval = null;
    }
}

async function startRenderVideo() {
    let renderedBackendLogCount = 0;
    if (!state.projectId || state.scriptProjectId !== state.projectId || state.scriptVideo !== state.selectedVideo) {
        showToast('Dữ liệu kịch bản không thuộc video đang chọn. Hãy tạo lại kịch bản Gemini.', 'error');
        return;
    }
    if (state.splitScriptSnapshot !== dom.scriptEditor.value.trim()) {
        showToast('Kịch bản đã thay đổi nhưng chưa tách lại câu. Hãy quay lại bước 3 và bấm Tách lại.', 'error');
        return;
    }
    const renderingProjectId = state.projectId;
    const renderingVideo = state.selectedVideo;
    // Thu thap cac cau da sua doi tu giao dien
    const textareas = dom.sentencesList.querySelectorAll('.sentence-textarea');
    const finalSentences = [];
    textareas.forEach(ta => {
        if (ta.value.trim()) finalSentences.push(ta.value.trim());
    });
    
    if (finalSentences.length === 0) {
        showToast('Kịch bản thuyết minh trống!', 'error');
        return;
    }
    
    state.sentences = finalSentences;
    
    dom.btnStartRender.disabled = true;
    dom.btnStartRender.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> ĐANG XỬ LÝ...';
    dom.btnRenderBack.disabled = true;
    dom.renderProgressBox.classList.remove('hidden');
    dom.renderLogTerminal.innerHTML = '<div class="log-line text-cyan">[*] Bắt đầu gửi lệnh xuất video sang GPU NVENC render...</div>';
    
    // Bat dau dem thoi gian
    startRenderTimer();
    
    const payload = {
        video_name: renderingVideo,
        project_id: renderingProjectId,
        bgm_name: dom.bgmSelect.value,
        bgm_volume: parseInt(dom.bgmVolumeSlider.value),
        voice_name: dom.voiceSelect.value,
        voice_rate: `${dom.voiceRateSlider.value >= 0 ? '+' : ''}${dom.voiceRateSlider.value}%`,
        sentences: state.sentences,
        mirror: dom.filterMirror.checked,
        zoom_factor: dom.filterZoom.checked ? 1.08 : 1.0,
        enable_zoom_crop: dom.filterZoom.checked,
        enable_ken_burns: dom.filterKenBurns ? dom.filterKenBurns.checked : false,
        speed_factor: parseFloat(dom.filterSpeed.value),
        contrast: parseFloat(dom.filterContrast.value),
        brightness: 1.0,
        aspect_ratio: document.querySelector('input[name="aspect_ratio"]:checked')?.value || "9:16",
        enable_subtitles: document.getElementById('enable-subtitles') ? document.getElementById('enable-subtitles').checked : true,
        enable_karaoke_sub: document.getElementById('enable-karaoke-sub') ? document.getElementById('enable-karaoke-sub').checked : true,
        font_size: parseInt(dom.subSize.value),
        font_color: dom.subColor.value,
        bg_opacity: parseInt(dom.subOpacity.value),
        export_mode: document.getElementById('export-mode').value
    };
    
    try {
        const res = await fetch('/api/render-video', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (state.projectId !== renderingProjectId || state.selectedVideo !== renderingVideo) return;
        
        if (data.success) {
            localStorage.setItem('active_render_task_id', data.task_id);
            pollTaskStatus(data.task_id, (statusData) => {
                if (state.projectId !== renderingProjectId || state.selectedVideo !== renderingVideo) return;
                // Cap nhat tien do render
                dom.renderStatusPercent.textContent = `${statusData.progress}%`;
                dom.renderProgressFill.style.width = `${statusData.progress}%`;
                dom.renderStatusLbl.textContent = statusData.message;
                
                // Hien thi nhat ky that tu backend, khong lap lai status moi 2 giay.
                const backendLogs = Array.isArray(statusData.logs) ? statusData.logs : [];
                backendLogs.slice(renderedBackendLogCount).forEach(entry => {
                    const logLine = document.createElement('div');
                    const level = entry.level || 'INFO';
                    logLine.className = `log-line ${level === 'ERROR' ? 'text-red' : (level === 'SUCCESS' ? 'text-green' : '')}`;
                    logLine.textContent = `[${entry.time}] [${level}] ${entry.message}`;
                    dom.renderLogTerminal.appendChild(logLine);
                    if (entry.details) {
                        const detailLine = document.createElement('pre');
                        detailLine.className = 'log-line text-red';
                        detailLine.textContent = entry.details;
                        dom.renderLogTerminal.appendChild(detailLine);
                    }
                });
                renderedBackendLogCount = backendLogs.length;
                dom.renderLogTerminal.scrollTop = dom.renderLogTerminal.scrollHeight;
            }, (videoUrl, taskData) => {
                localStorage.removeItem('active_render_task_id');
                if (state.projectId !== renderingProjectId || state.selectedVideo !== renderingVideo) return;
                // Render thanh cong!
                stopRenderTimer();
                dom.btnStartRender.disabled = false;
                dom.btnStartRender.innerHTML = '<i class="fa-solid fa-circle-play"></i> BẮT ĐẦU XUẤT VIDEO REVIEW';
                dom.btnRenderBack.disabled = false;
                
                dom.videoPreviewPlaceholder.classList.add('hidden');
                dom.videoPreviewWrapper.classList.remove('hidden');
                
                if (videoUrl.endsWith('.zip')) {
                    // Che do xuat CapCut (ZIP)
                    dom.finalVideoPlayer.style.display = 'none';
                    dom.btnDownloadVideo.href = videoUrl;
                    dom.btnDownloadVideo.innerHTML = '<i class="fa-solid fa-file-zipper"></i> TẢI GÓI NGUYÊN LIỆU CAPCUT (ZIP)';
                    showToast('Đã đóng gói nguyên liệu CapCut thành công!', 'success');
                } else {
                    // Che do xuat Full Video (MP4)
                    dom.finalVideoPlayer.style.display = 'block';
                    dom.finalVideoPlayer.src = videoUrl;
                    dom.finalVideoPlayer.load();
                    dom.btnDownloadVideo.href = videoUrl;
                    dom.btnDownloadVideo.innerHTML = '<i class="fa-solid fa-circle-down"></i> TẢI VIDEO REVIEW NGAY (MP4)';
                    showToast('Render và xuất video MP4 hoàn thành!', 'success');
                }
                if (taskData.export_path) {
                    showToast('Đã lưu thêm vào: ' + taskData.export_path, 'success');
                } else if (taskData.export_error) {
                    showToast('Video đã render nhưng chưa chép được: ' + taskData.export_error, 'error');
                }
            }, (errMessage) => {
                localStorage.removeItem('active_render_task_id');
                if (state.projectId !== renderingProjectId || state.selectedVideo !== renderingVideo) return;
                stopRenderTimer();
                dom.btnStartRender.disabled = false;
                dom.btnStartRender.innerHTML = '<i class="fa-solid fa-rotate-right"></i> THỬ LẠI / TIẾP TỤC XUẤT VIDEO';
                dom.btnRenderBack.disabled = false;
                showToast(errMessage, 'error');
            });
        } else {
            stopRenderTimer();
            dom.btnStartRender.disabled = false;
            dom.btnStartRender.innerHTML = '<i class="fa-solid fa-circle-play"></i> BẮT ĐẦU XUẤT VIDEO REVIEW';
            dom.btnRenderBack.disabled = false;
            showToast(data.message, 'error');
        }
    } catch (err) {
        if (state.projectId !== renderingProjectId || state.selectedVideo !== renderingVideo) return;
        stopRenderTimer();
        dom.btnStartRender.disabled = false;
        dom.btnStartRender.innerHTML = '<i class="fa-solid fa-circle-play"></i> BẮT ĐẦU XUẤT VIDEO REVIEW';
        dom.btnRenderBack.disabled = false;
        showToast('Lỗi hệ thống khi gửi tác vụ render.', 'error');
    }
}

// =====================================================================
// POLLING GENERAL SYSTEM TASK
// =====================================================================
async function resumePersistedRender() {
    const taskId = localStorage.getItem('active_render_task_id');
    if (!taskId) return;

    try {
        const response = await fetch(`/api/task-status/${taskId}`);
        const task = await response.json();
        if (task.status === 'not_found') {
            localStorage.removeItem('active_render_task_id');
            return;
        }

        state.projectId = task.project_id || state.projectId || createProjectId();
        state.selectedVideo = task.video_name || state.selectedVideo;
        state.highestStepReached = 5;
        switchPanel(5);
        dom.renderProgressBox.classList.remove('hidden');
        dom.btnStartRender.disabled = true;
        dom.btnRenderBack.disabled = true;
        dom.btnStartRender.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> ĐANG TIẾP TỤC XỬ LÝ...';
        dom.renderLogTerminal.innerHTML = '';
        startRenderTimer();
        let shownLogCount = 0;

        const showProgress = (statusData) => {
            dom.renderStatusPercent.textContent = `${statusData.progress || 0}%`;
            dom.renderProgressFill.style.width = `${statusData.progress || 0}%`;
            dom.renderStatusLbl.textContent = statusData.message || 'Worker đang xử lý...';
            const logs = Array.isArray(statusData.logs) ? statusData.logs : [];
            logs.slice(shownLogCount).forEach(entry => {
                const line = document.createElement('div');
                line.className = `log-line ${entry.level === 'ERROR' ? 'text-red' : (entry.level === 'SUCCESS' ? 'text-green' : '')}`;
                line.textContent = `[${entry.time}] [${entry.level || 'INFO'}] ${entry.message}`;
                dom.renderLogTerminal.appendChild(line);
            });
            shownLogCount = logs.length;
            dom.renderLogTerminal.scrollTop = dom.renderLogTerminal.scrollHeight;
        };

        pollTaskStatus(taskId, showProgress, (videoUrl, taskData) => {
            localStorage.removeItem('active_render_task_id');
            stopRenderTimer();
            dom.btnStartRender.disabled = false;
            dom.btnRenderBack.disabled = false;
            dom.btnStartRender.innerHTML = '<i class="fa-solid fa-circle-play"></i> BẮT ĐẦU XUẤT VIDEO REVIEW';
            dom.videoPreviewPlaceholder.classList.add('hidden');
            dom.videoPreviewWrapper.classList.remove('hidden');
            dom.btnDownloadVideo.href = videoUrl;
            if (videoUrl.endsWith('.zip')) {
                dom.finalVideoPlayer.style.display = 'none';
                dom.btnDownloadVideo.innerHTML = '<i class="fa-solid fa-file-zipper"></i> TẢI GÓI NGUYÊN LIỆU CAPCUT (ZIP)';
            } else {
                dom.finalVideoPlayer.style.display = 'block';
                dom.finalVideoPlayer.src = videoUrl;
                dom.finalVideoPlayer.load();
                dom.btnDownloadVideo.innerHTML = '<i class="fa-solid fa-circle-down"></i> TẢI VIDEO REVIEW NGAY (MP4)';
            }
            showToast('Đã khôi phục và hoàn thành tác vụ xuất video.', 'success');
        }, (message) => {
            localStorage.removeItem('active_render_task_id');
            stopRenderTimer();
            dom.btnStartRender.disabled = false;
            dom.btnRenderBack.disabled = false;
            dom.btnStartRender.innerHTML = '<i class="fa-solid fa-rotate-right"></i> THỬ LẠI / TIẾP TỤC XUẤT VIDEO';
            showToast(message, 'error');
        });
    } catch (error) {
        // Keep the id: the web server may still be restarting while the worker runs.
        showToast('Chưa kết nối lại được giao diện. Tác vụ render vẫn được giữ để thử lại.', 'error');
    }
}

function pollTaskStatus(taskId, onProgress, onSuccess, onError) {
    let errorCount = 0;
    const timer = setInterval(async () => {
        try {
            const res = await fetch(`/api/task-status/${taskId}`);
            const data = await res.json();
            
            const recoveredAfterDisconnect = errorCount >= 3;
            errorCount = 0;
            if (recoveredAfterDisconnect) {
                showToast('Đã kết nối lại máy chủ. Worker vẫn tiếp tục công việc.', 'success');
            }
            
            if (data.status === 'success') {
                clearInterval(timer);
                // Cap nhat progress len 100% truoc khi goi onSuccess
                onProgress({ ...data, progress: 100, message: 'Hoan thanh!' });
                onSuccess(data.result, data);
            } else if (data.status === 'failed' || data.status === 'not_found') {
                clearInterval(timer);
                // Đẩy log lỗi cuối cùng lên terminal trước khi hiện thông báo.
                onProgress(data);
                onError(data.message || 'Lỗi tác vụ không xác định.');
            } else {
                // Vẫn đang chạy
                onProgress(data);
            }
        } catch (err) {
            errorCount++;
            // Do not declare the render failed just because Flask is temporarily
            // unavailable. The detached worker may still be rendering normally.
            if (errorCount === 3) {
                showToast('Tạm mất kết nối giao diện; worker vẫn tiếp tục chạy. Đang tự kết nối lại…', 'error');
            }
        }
    }, 2000);
}

// =====================================================================
// EVENT LISTENERS BINDING
// =====================================================================
function setupEventListeners() {
    // Stepper click chuyển tab
    dom.steps.forEach(step => {
        step.addEventListener('click', () => {
            const stepNum = parseInt(step.getAttribute('data-step'));
            if (stepNum <= state.highestStepReached) {
                switchPanel(stepNum);
            }
        });
    });
    
    // Quay lại
    dom.backButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            const target = parseInt(btn.getAttribute('data-target'));
            switchPanel(target);
        });
    });
    
    // Toggle hiển thị API Key
    dom.btnToggleApiKey.addEventListener('click', () => {
        const type = dom.apiKeyInput.type === 'password' ? 'text' : 'password';
        [dom.apiKeyInput, dom.apiKeyInput2, dom.apiKeyInput3].forEach(input => {
            if (input) input.type = type;
        });
        dom.btnToggleApiKey.querySelector('i').className = type === 'password' ? 'fa-solid fa-eye' : 'fa-solid fa-eye-slash';
    });

    dom.btnExpandApiKeys.addEventListener('click', () => {
        const expanded = dom.btnExpandApiKeys.getAttribute('aria-expanded') === 'true';
        setAdditionalApiKeysExpanded(!expanded);
        if (!expanded && dom.apiKeyInput2 && !dom.apiKeyInput2.value) {
            dom.apiKeyInput2.focus();
        }
    });

    dom.apiPriorityButtons.forEach(button => {
        button.addEventListener('click', () => {
            prioritizeApiSlot(Number(button.dataset.apiSlot));
        });
    });

    // API verification button
    dom.btnVerifyApi.addEventListener('click', () => verifyApiKey(true));
    
    // Check key on text input change
    [dom.apiKeyInput, dom.apiKeyInput2, dom.apiKeyInput3].forEach(input => {
        if (!input) return;
        input.addEventListener('input', () => {
            const currentKeys = collectApiKeys();
            if (!currentKeys || JSON.stringify(currentKeys) !== JSON.stringify(state.apiKeys)) {
                updateAPIIndicator(false);
            }
            checkNextStep1Availability();
        });
    });
    
    // Select video input change
    dom.videoSelect.addEventListener('change', () => {
        const nextVideo = dom.videoSelect.value;
        if (nextVideo !== state.selectedVideo) resetProjectData(nextVideo);
        state.selectedVideo = nextVideo;
        checkNextStep1Availability();
    });
    
    // Drag/Drop Video Upload Setup
    setupDragAndDrop(dom.videoDropZone, dom.videoFileInput, dom.videoUploadProgress, 'video', (filename) => {
        // Tải lại list và chọn video mới upload
        loadVideosList().then(() => {
            dom.videoSelect.value = filename;
            resetProjectData(filename);
            checkNextStep1Availability();
        });
    });
    
    // Button Step 1 -> Step 2
    dom.btnToStep2.addEventListener('click', continueToScriptStep);
    
    // AI Scriptwriter Button
    dom.btnGenerateScript.addEventListener('click', generateScript);
    
    // Script Editor changes
    dom.scriptEditor.addEventListener('input', updateCharCount);
    
    // Button Step 2 -> Step 3
    dom.btnToStep3.addEventListener('click', () => {
        // Lưu giá trị kịch bản cuối cùng và chuyển bước
        state.script = dom.scriptEditor.value.trim();
        if (!state.script) {
            showToast('Nội dung kịch bản trống!', 'error');
            return;
        }
        if (state.script !== state.splitScriptSnapshot) {
            state.sentences = [];
        }
        switchPanel(3);
    });
    
    // Re-split Sentences Button
    dom.btnReSplit.addEventListener('click', autoSplitScript);
    
    // Add new sentence Button
    dom.btnAddSentence.addEventListener('click', () => {
        state.sentences.push('(Nhập câu mới tại đây)');
        renderSentences();
        updateSentenceStats();
        // Scroll to bottom
        dom.sentencesList.scrollTop = dom.sentencesList.scrollHeight;
        showToast('Đã thêm 1 câu mới!', 'success');
    });
    
    // Voice Sliders Display Sync
    dom.voiceRateSlider.addEventListener('input', () => {
        const val = parseInt(dom.voiceRateSlider.value);
        dom.voiceRateVal.textContent = `${val >= 0 ? '+' : ''}${val}%`;
    });
    
    // Button Step 3 -> Step 4
    dom.btnToStep4.addEventListener('click', () => {
        // Đảm bảo lấy danh sách câu đã chỉnh sửa
        const textareas = dom.sentencesList.querySelectorAll('.sentence-textarea');
        const updated = [];
        textareas.forEach(ta => {
            if (ta.value.trim()) updated.push(ta.value.trim());
        });
        
        if (updated.length === 0) {
            showToast('Vui lòng tạo kịch bản và phân câu!', 'error');
            return;
        }
        state.sentences = updated;
        switchPanel(4);
        updateSubtitlePreviewBackground(state.selectedVideo);
    });
    
    // Su kien toggle hien thi phu de
    const toggleSubtitles = document.getElementById('enable-subtitles');
    if (toggleSubtitles) {
        toggleSubtitles.addEventListener('change', (e) => {
            const previewBox = document.getElementById('subtitle-preview-box');
            if(e.target.checked) {
                if(previewBox) previewBox.style.opacity = '1';
                dom.subSize.disabled = false;
                dom.subColor.disabled = false;
            } else {
                if(previewBox) previewBox.style.opacity = '0.3';
                dom.subSize.disabled = true;
                dom.subColor.disabled = true;
            }
        });
    }
    
    // Drag/Drop BGM Upload Setup
    setupDragAndDrop(dom.bgmDropZone, dom.bgmFileInput, dom.bgmUploadProgress, 'bgm', (filename) => {
        loadBgmsList().then(() => {
            dom.bgmSelect.value = filename;
        });
    });
    
    // BGM Volume Slider Sync
    dom.bgmVolumeSlider.addEventListener('input', () => {
        dom.bgmVolumeVal.textContent = `${dom.bgmVolumeSlider.value}%`;
    });
    
    // ===== Subtitle Live Preview =====
    function updateSubtitlePreview() {
        const previewText = document.getElementById('preview-subtitle-text');
        if (!previewText) return;
        
        const size = parseInt(dom.subSize.value) || 42;
        const color = dom.subColor.value || '#FFFFFF';
        const opacity = parseInt(dom.subOpacity.value) || 150;
        
        // Tinh toan nhu backend: ASS Fontsize = user_px * 384 / 1080
        // Preview scale: preview box ~200px cao, video that 1080px => ty le ~0.185
        // => preview_px = user_px * (384/1080) * (200/384) = user_px * 200/1080 ≈ user_px * 0.185
        const scaledSize = Math.max(8, Math.round(size * 0.185));
        const bgAlpha = (opacity / 255).toFixed(2);
        
        previewText.style.fontSize = `${scaledSize}px`;
        previewText.style.color = color;
        previewText.style.background = `rgba(0, 0, 0, ${bgAlpha})`;
    }
    
    dom.subSize.addEventListener('input', updateSubtitlePreview);
    dom.subColor.addEventListener('input', updateSubtitlePreview);
    dom.subOpacity.addEventListener('input', updateSubtitlePreview);
    
    // Subtitle Preset Chips
    document.querySelectorAll('.preset-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            const size = chip.getAttribute('data-size');
            const color = chip.getAttribute('data-color');
            const opacity = chip.getAttribute('data-opacity');
            
            dom.subSize.value = size;
            dom.subColor.value = color;
            dom.subOpacity.value = opacity;
            
            // Cap nhat active state
            document.querySelectorAll('.preset-chip').forEach(c => c.classList.remove('active'));
            chip.classList.add('active');
            
            updateSubtitlePreview();
            showToast('Đã áp dụng mẫu phụ đề!', 'success');
        });
    });
    
    // Initial preview
    updateSubtitlePreview();
    
    // Button Step 4 -> Step 5
    dom.btnToStep5.addEventListener('click', () => {
        enableStep(5);
        switchPanel(5);
    });
    
    // Start Rendering Button
    dom.btnStartRender.addEventListener('click', startRenderVideo);
    if (dom.btnChooseExportDirectory) {
        dom.btnChooseExportDirectory.addEventListener('click', chooseExportDirectory);
    }
}

// =====================================================================
// DOCUMENT ON READY
// =====================================================================
document.addEventListener('DOMContentLoaded', () => {
    loadInitialData().then(resumePersistedRender);
    loadSfxList();
    loadExportDirectory();
    setupEventListeners();
    
    // Prompt Template Chips - nhan de dien vao o "Yeu Cau Chi Tiet"
    document.querySelectorAll('.prompt-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            const promptText = chip.getAttribute('data-prompt');
            if (promptText && dom.customPrompt) {
                if (dom.customPrompt.value.trim() !== '') {
                    dom.customPrompt.value += '\n- ' + promptText;
                } else {
                    dom.customPrompt.value = promptText;
                }
                dom.customPrompt.focus();
                // Hieu ung visual: chip sang len
                chip.style.borderColor = 'var(--accent-green)';
                chip.style.color = 'var(--accent-green)';
                setTimeout(() => {
                    chip.style.borderColor = '';
                    chip.style.color = '';
                }, 800);
                showToast('Đã điền mẫu prompt!', 'success');
            }
        });
    });
    
    // Bắt sự kiện cho các thẻ Cảm xúc
    document.querySelectorAll('.emotion-tag').forEach(tag => {
        tag.addEventListener('click', () => {
            const insertText = tag.getAttribute('data-insert');
            if (insertText && dom.customPrompt) {
                if (dom.customPrompt.value.trim() !== '') {
                    dom.customPrompt.value += '\n- [Cảm xúc]: ' + insertText;
                } else {
                    dom.customPrompt.value = '- [Cảm xúc]: ' + insertText;
                }
                dom.customPrompt.focus();
                showToast('Đã chèn lệnh cảm xúc!', 'success');
            }
        });
    });

    // Bat su kien Goi Y Nhac Nen
    const btnSuggestBgm = document.getElementById('btn-suggest-bgm');
    if (btnSuggestBgm) {
        btnSuggestBgm.addEventListener('click', () => {
            const style = dom.styleSelect ? dom.styleSelect.value : "";
            const options = Array.from(dom.bgmSelect.options);
            
            let targetKeywords = [];
            if (style === "dramatic") targetKeywords = ["epic", "battle", "action", "cinematic", "dramatic"];
            else if (style === "humorous") targetKeywords = ["funny", "sneaky", "clown", "quirky", "comedy"];
            else if (style === "shorts") targetKeywords = ["epic", "action", "suspense", "cyberpunk"];
            else if (style === "news") targetKeywords = ["news", "corporate", "upbeat", "electronic"];
            else if (style === "tutorial") targetKeywords = ["lofi", "chill", "tutorial", "ambient"];
            
            let found = false;
            for (let i = 0; i < options.length; i++) {
                let val = options[i].value.toLowerCase();
                if (val && targetKeywords.some(kw => val.includes(kw))) {
                    dom.bgmSelect.selectedIndex = i;
                    found = true;
                    // Kich hoat event change
                    dom.bgmSelect.dispatchEvent(new Event('change'));
                    break;
                }
            }
            
            if (found) {
                showToast(`Đã tự động chọn nhạc nền cho phong cách: ${style}`, 'success');
            } else {
                showToast('Không tìm thấy nhạc phù hợp! Hãy tải các file nhạc có tên như epic, funny, lofi...', 'warning');
            }
        });
    }
});


// Canvas Subtitle Preview (Step 4)
function updateSubtitlePreviewBackground(videoFilename) {
    const previewBox = document.getElementById('subtitle-preview-box');
    if (!previewBox || !videoFilename) return;

    // Tạo một video tag ẩn trong bộ nhớ để load video gốc
    const tempVideo = document.createElement('video');
    tempVideo.crossOrigin = "anonymous";
    tempVideo.src = `/static/output/${videoFilename.replace('.mp4', '_gemini_temp.mp4')}`;
    
    // Fallback neu compressed chua co
    tempVideo.onerror = function() {
        tempVideo.onerror = null;
        tempVideo.src = `/source-video/${encodeURIComponent(videoFilename)}`;
    };

    tempVideo.currentTime = 2.0; // Seek đến giây thứ 2 để lấy hình

    tempVideo.addEventListener('loadeddata', () => {
        // Dùng Canvas chụp lại frame hình ở giây thứ 2
        const canvas = document.createElement('canvas');
        canvas.width = tempVideo.videoWidth || 1280;
        canvas.height = tempVideo.videoHeight || 720;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(tempVideo, 0, 0, canvas.width, canvas.height);

        // Biến frame hình thành ảnh nền cho hộp Preview Phụ Đề
        const frameDataUrl = canvas.toDataURL('image/jpeg');
        previewBox.style.backgroundImage = `url(${frameDataUrl})`;
        previewBox.style.backgroundSize = "cover";
        previewBox.style.backgroundPosition = "center";
        previewBox.style.boxShadow = "inset 0 0 100px rgba(0,0,0,0.6)";
        
        // Gỡ icon film đi
        const icon = previewBox.querySelector('.preview-bg-icon');
        if(icon) icon.style.opacity = "0.1";
    });
}

// =====================================================================
// VOICE PREVIEW - Nghe thu giong doc
// =====================================================================
(function() {
    const btn = document.getElementById('btn-preview-voice');
    if (!btn) return;
    
    const audio = document.getElementById('voice-preview-audio');
    const status = document.getElementById('voice-preview-status');
    const voiceSelect = document.getElementById('voice-select');
    const rateSlider = document.getElementById('voice-rate-slider');
    
    const sampleText = "Trong thế giới đầy bí ẩn này, một cuộc phiêu lưu mới đang bắt đầu. Liệu nhân vật chính có thể vượt qua mọi thử thách và tìm ra sự thật?";
    
    btn.onclick = async () => {
        const voice = voiceSelect.value;
        const rateVal = parseInt(rateSlider?.value || 10);
        const rate = (rateVal >= 0 ? '+' : '') + rateVal + '%';
        
        // UI loading
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Đang tạo...';
        status.style.display = 'inline';
        status.textContent = '';
        audio.style.display = 'none';
        audio.pause();
        
        try {
            const res = await fetch('/api/preview-tts', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text: sampleText, voice, rate })
            });
            const data = await res.json();
            
            if (data.success) {
                audio.src = data.url + '?t=' + Date.now();
                audio.style.display = 'block';
                audio.play();
                status.textContent = '✅ Đang phát';
                status.style.color = '#4ade80';
            } else {
                status.textContent = '❌ ' + (data.message || 'Lỗi');
                status.style.color = '#f87171';
            }
        } catch(e) {
            status.textContent = '❌ Lỗi kết nối';
            status.style.color = '#f87171';
        }
        
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-headphones"></i> Nghe thử giọng';
        
        setTimeout(() => { status.style.display = 'none'; }, 5000);
    };
})();
