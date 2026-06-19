// ── State ──────────────────────────────────────────────────────
let state = {
    currentSessionId: null,
    mode: 'build',
    model: 'deepseek-v4-pro',
    provider: 'deepseek',
    projectPath: document.getElementById('project-path-label')?.textContent || '',
    pendingPermission: null,
    knownModels: [],
};

const messageList = document.getElementById('message-list');
const promptInput = document.getElementById('prompt-input');
const submitBtn = document.getElementById('submit-btn');
const modeToggle = document.getElementById('mode-toggle');
const modalOverlay = document.getElementById('modal-overlay');
const modalContainer = document.getElementById('modal-container');

// ── Session management ────────────────────────────────────────

async function newSession(customPath) {
    const path = customPath || state.projectPath;
    const resp = await fetch('/api/sessions', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mode: state.mode, model: state.model, project_path: path}),
    });
    const data = await resp.json();
    state.currentSessionId = data.id;
    clearMessages();
    hideWelcome();
    updateSidebar();
    updateStatusBar();
    enableInput();
    appendMessage({role: 'system', content: `Session started · ${path} · ${state.model}`});
}

async function switchSession(sessionId) {
    state.currentSessionId = sessionId;
    const resp = await fetch(`/api/sessions/${sessionId}`);
    const data = await resp.json();
    hideWelcome();
    renderMessages(data.messages || []);
    state.mode = data.mode || 'build';
    state.model = data.model || state.model;
    state.projectPath = data.project_path || state.projectPath;
    updateModeButton();
    updateStatusBar(data);
    enableInput();
    document.querySelectorAll('.session-item').forEach(el => {
        el.classList.toggle('active', el.dataset.id === sessionId);
    });
}

// ── Welcome screen ────────────────────────────────────────────

function hideWelcome() {
    const welcome = document.getElementById('welcome-screen');
    if (welcome) welcome.style.display = 'none';
}

// ── Project picker ────────────────────────────────────────────

async function showProjectPicker() {
    const overlay = document.getElementById('project-picker-overlay');
    overlay.classList.remove('hidden');
    await navigateFolder(state.projectPath || '~');
}

function hideProjectPicker() {
    document.getElementById('project-picker-overlay').classList.add('hidden');
}

async function navigateFolder(path) {
    const el = document.getElementById('folder-contents');
    el.innerHTML = '<p>Loading...</p>';
    document.getElementById('picker-current-path').value = path;
    try {
        const resp = await fetch(`/api/folder/list?path=${encodeURIComponent(path)}`);
        const data = await resp.json();
        if (data.error) {
            el.innerHTML = `<p class="error">${data.error}</p>`;
            return;
        }
        el.innerHTML = '';
        if (path !== '/') {
            const parent = document.createElement('div');
            parent.className = 'folder-item folder-up';
            parent.innerHTML = '<span class="folder-icon">📁</span> ..';
            parent.onclick = () => navigateFolder(data.path + '/..');
            el.appendChild(parent);
        }
        for (const entry of data.entries) {
            const div = document.createElement('div');
            div.className = `folder-item folder-${entry.type}`;
            div.innerHTML = `<span class="folder-icon">${entry.type === 'directory' ? '📁' : '📄'}</span> ${escapeHtml(entry.name)}`;
            if (entry.type === 'directory') {
                div.onclick = () => navigateFolder(entry.path);
            }
            el.appendChild(div);
        }
    } catch (e) {
        el.innerHTML = `<p class="error">Error: ${e.message}</p>`;
    }
}

async function confirmProjectPath() {
    const path = document.getElementById('picker-current-path').value;
    state.projectPath = path;
    const label = document.getElementById('project-path-label');
    if (label) label.textContent = path;
    const projectEl = document.getElementById('welcome-project');
    if (projectEl) projectEl.style.display = 'block';
    hideProjectPicker();
    await newSession(path);
}

// ── Provider setup ────────────────────────────────────────────

async function showProviderSetup() {
    const overlay = document.getElementById('provider-setup-overlay');
    const body = document.getElementById('provider-setup-body');
    overlay.classList.remove('hidden');
    body.innerHTML = '<p>Loading providers...</p>';
    try {
        const resp = await fetch('/api/providers');
        const providers = await resp.json();
        body.innerHTML = providers.map(p => `
            <div class="provider-card ${p.configured ? 'configured' : ''}">
                <div class="provider-info">
                    <div class="provider-name">${escapeHtml(p.name)}</div>
                    <div class="provider-url">${escapeHtml(p.base_url)}</div>
                    <div class="provider-models">${p.models.length} models cached</div>
                </div>
                <div class="provider-actions">
                    ${p.configured ? `
                        <button class="btn btn-small" onclick="fetchModels('${p.id}')">Fetch Models</button>
                        <button class="btn btn-small btn-danger" onclick="disconnectProvider('${p.id}')">Disconnect</button>
                    ` : `
                        <button class="btn btn-small btn-primary" onclick="showConnectForm('${p.id}')">Connect</button>
                    `}
                </div>
            </div>
        `).join('');
    } catch (e) {
        body.innerHTML = `<p class="error">Error: ${e.message}</p>`;
    }
}

function hideProviderSetup() {
    document.getElementById('provider-setup-overlay').classList.add('hidden');
    loadProvidersStatus();
}

let connectProviderId = null;

function showConnectForm(providerId) {
    connectProviderId = providerId;
    const body = document.getElementById('provider-setup-body');
    body.innerHTML = `
        <h3>Connect ${providerId}</h3>
        <div class="connect-form">
            <div class="setting-row">
                <label>API Key</label>
                <input type="password" id="connect-api-key" class="setting-input" placeholder="sk-...">
            </div>
            <div class="setting-row">
                <label>Base URL</label>
                <input type="text" id="connect-base-url" class="setting-input">
            </div>
            <div class="connect-actions">
                <button class="btn btn-primary" onclick="saveProviderConnection()">Save & Connect</button>
                <button class="btn" onclick="showProviderSetup()">Cancel</button>
            </div>
        </div>
    `;
    // Fetch base URL
    fetch('/api/providers').then(r => r.json()).then(providers => {
        const p = providers.find(x => x.id === providerId);
        if (p) document.getElementById('connect-base-url').value = p.base_url;
    });
}

async function saveProviderConnection() {
    const apiKey = document.getElementById('connect-api-key').value;
    const baseUrl = document.getElementById('connect-base-url').value;
    await fetch('/api/providers/configure', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({provider: connectProviderId, api_key: apiKey, base_url: baseUrl}),
    });
    await fetchModels(connectProviderId);
    showProviderSetup();
}

async function disconnectProvider(providerId) {
    await fetch(`/api/providers/${providerId}/disconnect`, {method: 'POST'});
    showProviderSetup();
}

async function fetchModels(providerId) {
    const body = document.getElementById('provider-setup-body');
    body.innerHTML = `<p>Fetching models from ${providerId}...</p>`;
    try {
        const resp = await fetch(`/api/providers/${providerId}/fetch-models`, {method: 'POST'});
        const data = await resp.json();
        const count = data.count || data.models?.length || 0;
        body.innerHTML = `<p>✅ Found ${count} models from ${providerId}.</p><button class="btn" onclick="showProviderSetup()">Back</button>`;
        if (count > 0) {
            state.knownModels = data.models;
            updateModelSelector();
        }
    } catch (e) {
        body.innerHTML = `<p class="error">Error: ${e.message}</p><button class="btn" onclick="showProviderSetup()">Back</button>`;
    }
}

// ── Model picker ──────────────────────────────────────────────

async function showModelPicker() {
    const overlay = document.getElementById('model-picker-overlay');
    const body = document.getElementById('model-list-body');
    overlay.classList.remove('hidden');
    body.innerHTML = '<p>Loading models...</p>';
    try {
        const resp = await fetch('/api/providers');
        const providers = await resp.json();
        let html = '';
        for (const p of providers) {
            const models = p.models || [];
            if (models.length === 0) continue;
            html += `<div class="model-group"><div class="model-group-name">${escapeHtml(p.name)}</div>`;
            for (const m of models) {
                const mid = typeof m === 'string' ? m : m.id || m.name;
                const active = mid === state.model ? 'active' : '';
                html += `<div class="model-item ${active}" onclick="selectModel('${escapeAttr(p.id)}', '${escapeAttr(mid)}')">
                    <span class="model-id">${escapeHtml(mid)}</span>
                    <span class="model-owned">${escapeHtml(m.owned_by || '')}</span>
                </div>`;
            }
            html += '</div>';
        }
        if (!html) {
            html = '<p>No models found. Connect a provider first.</p>';
        }
        body.innerHTML = html;
    } catch (e) {
        body.innerHTML = `<p class="error">${e.message}</p>`;
    }
}

function hideModelPicker() {
    document.getElementById('model-picker-overlay').classList.add('hidden');
}

function selectModel(providerId, modelId) {
    state.provider = providerId;
    state.model = modelId;
    hideModelPicker();
    updateModelBadge();
    updateStatusBar();
    updateProviderDot();
    if (state.currentSessionId) {
        fetch(`/api/sessions/${state.currentSessionId}`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({model: modelId, provider: providerId}),
        }).catch(() => {});
    }
    // Update the <select> on settings page too
    const defaultSel = document.getElementById('default-model');
    if (defaultSel) defaultSel.value = modelId;
}

// ── Message rendering ─────────────────────────────────────────

function clearMessages() {
    messageList.innerHTML = '';
}

function renderMessages(messages) {
    clearMessages();
    for (const msg of messages) appendMessage(msg);
}

function appendMessage(msg) {
    const el = document.createElement('div');
    el.className = 'message';
    el.dataset.role = msg.role || 'assistant';

    let html = `<div class="message-role">${msg.role || 'assistant'}</div>`;
    html += `<div class="message-content">${escapeHtml(msg.content || '')}</div>`;

    if (msg.reasoning_content) {
        html += `<div class="thinking-accordion">
            <div class="thinking-header" onclick="toggleThinking(this)">🤔 Thinking...</div>
            <div class="thinking-content">${escapeHtml(msg.reasoning_content)}</div>
        </div>`;
    }

    if (msg.tool_calls_json) {
        try {
            const calls = JSON.parse(msg.tool_calls_json);
            for (const tc of calls) {
                html += `<div class="tool-call-card">
                    <div class="tool-call-header" onclick="toggleToolCall(this)">
                        <span>🔧 ${escapeHtml(tc.function?.name || 'tool')}</span>
                        <span class="tc-id">${tc.id ? tc.id.slice(0, 8) : ''}</span>
                    </div>
                    <div class="tool-call-body">${escapeHtml(JSON.stringify(tc.function?.arguments || {}, null, 2))}</div>
                </div>`;
            }
        } catch(e) {}
    }

    el.innerHTML = html;
    messageList.appendChild(el);
    messageList.scrollTop = messageList.scrollHeight;
}

function appendToken(text) {
    const last = messageList.lastElementChild;
    if (last && last.dataset.role === 'assistant') {
        const content = last.querySelector('.message-content');
        if (content) {
            content.textContent += text;
            messageList.scrollTop = messageList.scrollHeight;
        }
    }
}

function addToolCallCard(data) {
    const el = document.createElement('div');
    el.className = 'message';
    el.dataset.role = 'assistant';
    el.innerHTML = `<div class="tool-call-card">
        <div class="tool-call-header" onclick="toggleToolCall(this)">
            <span>🔧 ${escapeHtml(data.name)}</span>
            <span class="spinner">⏳</span>
        </div>
        <div class="tool-call-body">${escapeHtml(JSON.stringify(data.arguments, null, 2))}</div>
    </div>`;
    messageList.appendChild(el);
    messageList.scrollTop = messageList.scrollHeight;
}

function updateToolCallResult(data) {
    const cards = messageList.querySelectorAll('.tool-call-card');
    for (const card of cards) {
        const header = card.querySelector('.tool-call-header');
        if (header && header.textContent.includes(data.name)) {
            const spinner = card.querySelector('.spinner');
            if (spinner) spinner.textContent = data.permission === 'deny' ? '✗' : '✓';
            const resultEl = document.createElement('div');
            resultEl.className = 'tool-call-body open';
            resultEl.textContent = typeof data.result === 'string' ? data.result.slice(0, 2000) : JSON.stringify(data.result);
            card.appendChild(resultEl);
            break;
        }
    }
    messageList.scrollTop = messageList.scrollHeight;
}

// ── SSE streaming ─────────────────────────────────────────────

async function sendMessage() {
    let text = promptInput.value.trim();
    if (!text || !state.currentSessionId) return;

    if (text.startsWith('/')) {
        handleSlashCommand(text);
        promptInput.value = '';
        return;
    }

    disableInput();
    appendMessage({role: 'user', content: text});
    promptInput.value = '';
    autoResizeInput();

    const typingEl = document.createElement('div');
    typingEl.className = 'message';
    typingEl.dataset.role = 'assistant';
    typingEl.innerHTML = '<div class="message-content"><em>Thinking...</em></div>';
    messageList.appendChild(typingEl);
    messageList.scrollTop = messageList.scrollHeight;

    try {
        const resp = await fetch(`/api/sessions/${state.currentSessionId}/messages`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({content: text}),
        });

        typingEl.remove();
        if (!resp.ok) {
            appendMessage({role: 'tool', content: `Error: ${resp.statusText}`});
            enableInput();
            return;
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let thinking = false;

        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, {stream: true});
            const events = buffer.split('\n\n');
            buffer = events.pop() || '';

            for (const event of events) {
                let eventType = '', eventData = '';
                for (const line of event.split('\n')) {
                    if (line.startsWith('event: ')) eventType = line.slice(7);
                    if (line.startsWith('data: ')) eventData = line.slice(6);
                }
                if (!eventType || !eventData) continue;
                try {
                    const data = JSON.parse(eventData);
                    switch (eventType) {
                        case 'thinking': thinking = true; break;
                        case 'token':
                            if (thinking) {
                                const typing = messageList.querySelector('[data-role="assistant"]:last-child .message-content em');
                                if (typing) typing.parentElement.innerHTML = '';
                                thinking = false;
                            }
                            appendToken(data.content);
                            break;
                        case 'tool_call': addToolCallCard(data); break;
                        case 'tool_result': updateToolCallResult(data); break;
                        case 'permission_required': showPermissionModal(data); break;
                        case 'error': appendMessage({role: 'tool', content: `Error: ${data.message}`}); break;
                        case 'done':
                            updateStatusBar(data);
                            enableInput();
                            updateSidebar();
                            break;
                    }
                } catch(e) {}
            }
        }
    } catch(e) {
        typingEl.remove();
        appendMessage({role: 'tool', content: `Connection error: ${e.message}`});
    }
    enableInput();
}

// ── Permissions ──────────────────────────────────────────────

function showPermissionModal(data) {
    const prompt = data.prompt || 'Allow this action?';
    modalContainer.innerHTML = `<div class="modal permission-modal">
        <div class="modal-header"><h3>🔒 Permission Required</h3></div>
        <div class="modal-body"><pre>${escapeHtml(prompt)}</pre></div>
        <div class="modal-footer">
            <button class="btn btn-allow-once" onclick="resolvePermission(true)"><kbd>Y</kbd> Allow Once</button>
            <button class="btn btn-allow-all" onclick="resolvePermission(true, true)"><kbd>A</kbd> Allow All</button>
            <button class="btn btn-deny" onclick="resolvePermission(false)"><kbd>N</kbd> Deny</button>
        </div>
    </div>`;
    modalOverlay.classList.remove('hidden');
}

function resolvePermission(approved, remember = false) {
    modalOverlay.classList.add('hidden');
    if (remember) state.autoApprove = true;
}

// ── Mode toggle ───────────────────────────────────────────────

function toggleMode() {
    state.mode = state.mode === 'build' ? 'plan' : 'build';
    updateModeButton();
}

function updateModeButton() {
    if (modeToggle) {
        modeToggle.textContent = state.mode.toUpperCase();
        modeToggle.className = `mode-btn ${state.mode}`;
    }
    const el = document.getElementById('status-mode');
    if (el) {
        el.textContent = state.mode.toUpperCase();
        el.className = `mode-badge ${state.mode}`;
    }
}

// ── Slash commands ────────────────────────────────────────────

function handleSlashCommand(text) {
    const parts = text.split(' ');
    const cmd = parts[0].toLowerCase();
    switch (cmd) {
        case '/undo':
            if (!state.currentSessionId) return;
            fetch(`/api/sessions/${state.currentSessionId}/undo`, {method: 'POST'})
                .then(r => r.json()).then(d => appendMessage({role: 'tool', content: `↩ Undid ${d.reverted_count} message(s)`}));
            break;
        case '/redo':
            if (!state.currentSessionId) return;
            fetch(`/api/sessions/${state.currentSessionId}/redo`, {method: 'POST'})
                .then(r => r.json()).then(d => appendMessage({role: 'tool', content: d.restored ? `↪ Restored message` : 'Nothing to redo'}));
            break;
        case '/new': newSession(); break;
        case '/models': showModelPicker(); break;
        case '/settings': window.location.href = '/settings'; break;
        case '/connect': showProviderSetup(); break;
        case '/project': showProjectPicker(); break;
        case '/help':
            appendMessage({role: 'tool', content: `Commands:
  /undo     — Undo last changes
  /redo     — Redo last undo
  /new      — New session
  /models   — Switch model
  /connect  — Add API key
  /project  — Choose folder
  /settings — Open settings
  /help     — This message`});
            break;
        default:
            appendMessage({role: 'tool', content: `Unknown: ${cmd}. Try /help`});
    }
}

// ── Input handling ────────────────────────────────────────────

function autoResizeInput() {
    if (promptInput) {
        promptInput.style.height = 'auto';
        promptInput.style.height = Math.min(promptInput.scrollHeight, 200) + 'px';
    }
}

function disableInput() {
    if (promptInput) promptInput.disabled = true;
    if (submitBtn) submitBtn.disabled = true;
}

function enableInput() {
    if (promptInput) promptInput.disabled = false;
    if (submitBtn) submitBtn.disabled = false;
    if (promptInput) promptInput.focus();
}

// ── Event listeners ───────────────────────────────────────────

document.addEventListener('DOMContentLoaded', function() {
    if (promptInput) {
        promptInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
            if (e.key === 'Tab') {
                e.preventDefault();
                toggleMode();
            }
            if (e.key === '@' && !e.ctrlKey) {
                e.preventDefault();
                // Trigger file autocomplete
            }
            if (e.key === '/' && !this.value) {
                const hints = document.getElementById('slash-hints');
                if (hints) hints.classList.remove('hidden');
            }
        });
        promptInput.addEventListener('input', function() {
            autoResizeInput();
            const hints = document.getElementById('slash-hints');
            if (hints && !this.value.startsWith('/')) hints.classList.add('hidden');
        });
        promptInput.addEventListener('blur', function() {
            setTimeout(() => {
                const hints = document.getElementById('slash-hints');
                if (hints) hints.classList.add('hidden');
            }, 200);
        });
    }

    document.addEventListener('keydown', function(e) {
        if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); sendMessage(); }
        if (e.ctrlKey && e.key === 'u') { e.preventDefault(); handleSlashCommand('/undo'); }
        if (e.ctrlKey && e.key === 'r') { e.preventDefault(); handleSlashCommand('/redo'); }
        if (e.key === 'Escape') {
            document.querySelectorAll('.modal-overlay').forEach(el => el.classList.add('hidden'));
        }
        if (e.key === 'y' && !modalOverlay.classList.contains('hidden')) resolvePermission(true);
        if (e.key === 'n' && !modalOverlay.classList.contains('hidden')) resolvePermission(false);
    });

    updateModeButton();
    updateModelBadge();
    // Load providers and known models on startup
    loadProvidersStatus();
});

// ── Status bar ────────────────────────────────────────────────

function updateStatusBar(data) {
    const modelEl = document.getElementById('status-model');
    const modeEl = document.getElementById('status-mode');
    const projectEl = document.getElementById('status-project');
    const tokensEl = document.getElementById('status-tokens');
    const costEl = document.getElementById('status-cost');
    const cacheEl = document.getElementById('status-cache');
    const connEl = document.getElementById('status-connection');

    if (modelEl) modelEl.textContent = state.model;
    updateModelBadge();
    updateProviderDot();
    if (modeEl) { modeEl.textContent = state.mode.toUpperCase(); modeEl.className = `mode-badge ${state.mode}`; }
    if (projectEl) projectEl.textContent = state.projectPath.split('/').filter(Boolean).pop() || '~';
    if (tokensEl && data?.usage) tokensEl.textContent = `${data.usage.tokens_in || 0} / ${data.usage.tokens_out || 0}`;
    if (costEl && data?.usage) costEl.textContent = `$${(data.usage.cost_usd || 0).toFixed(4)}`;
    if (cacheEl && data?.usage) cacheEl.textContent = `${Math.round((data.usage.cache_hit_rate || 0) * 100)}%`;
    if (connEl) connEl.className = 'status-dot connected';
}

// ── Sidebar ───────────────────────────────────────────────────

async function updateSidebar() {
    const resp = await fetch('/api/sessions?limit=50');
    const sessions = await resp.json();
    const list = document.getElementById('session-list');
    if (!list) return;
    list.innerHTML = sessions.map(s => `
        <div class="session-item ${s.id === state.currentSessionId ? 'active' : ''}"
             data-id="${s.id}" onclick="switchSession('${s.id}')">
            <div class="session-title">${escapeHtml(s.title || '(untitled)')}</div>
            <div class="session-meta">
                <span class="model-badge">${escapeHtml(s.model.split('/').pop().slice(0, 18))}</span>
                <span class="session-time">${(s.updated_at || '').slice(0, 10)}</span>
            </div>
        </div>
    `).join('');
}

// ── Providers status (for status bar) ─────────────────────────

async function loadProvidersStatus() {
    try {
        const resp = await fetch('/api/providers');
        const providers = await resp.json();
        const configured = providers.filter(p => p.configured);
        state.knownModels = providers.flatMap(p => p.models || []);
        updateModelSelector();
    } catch(e) {}
}

function updateModelBadge() {
    const badge = document.getElementById('current-model-badge');
    if (badge) badge.textContent = state.model;
}

function updateProviderDot() {
    const dot = document.getElementById('provider-dot');
    if (!dot) return;
    dot.className = 'provider-dot' + (state.provider ? ' connected' : '');
    dot.title = state.provider ? `Using ${state.provider}` : 'No provider';
}

function updateModelSelector() {
    const sel = document.getElementById('default-model');
    if (sel && state.knownModels.length > 0) {
        sel.innerHTML = state.knownModels.map(m => {
            const id = typeof m === 'string' ? m : m.id || m.name;
            return `<option value="${escapeAttr(id)}">${escapeHtml(id)}</option>`;
        }).join('');
        sel.value = state.model;
    }
    updateModelBadge();
    updateProviderDot();
}

// ── Settings page functions ───────────────────────────────────

async function loadSettingsPage() {
    await loadProvidersList();
    await loadMcpServers();
    await loadPlugins();
    await loadProvidersStatus();
    // Set the default model selector
    const sel = document.getElementById('default-model');
    if (sel) sel.value = state.model;
}

async function loadProvidersList() {
    const el = document.getElementById('provider-list');
    if (!el) return;
    try {
        const resp = await fetch('/api/providers');
        const providers = await resp.json();
        el.innerHTML = providers.map(p => `
            <div class="provider-card ${p.configured ? 'configured' : ''}">
                <div class="provider-info">
                    <div class="provider-name">${escapeHtml(p.name)}</div>
                    <div class="provider-baseurl">${escapeHtml(p.base_url)}</div>
                    ${p.configured ? '<span class="badge badge-ok">Connected</span>' : '<span class="badge">Not connected</span>'}
                    ${p.models.length ? `<span class="badge">${p.models.length} models</span>` : ''}
                </div>
                <div class="provider-actions">
                    ${p.configured ? `
                        <button class="btn btn-small" onclick="fetchModelsSettings('${p.id}')">🔄 Refresh Models</button>
                        <button class="btn btn-small btn-danger" onclick="disconnectProviderSettings('${p.id}')">Disconnect</button>
                    ` : `
                        <button class="btn btn-small btn-primary" onclick="showConnectFormSettings('${p.id}')">Connect</button>
                    `}
                </div>
            </div>
        `).join('');
    } catch(e) {
        if (el) el.innerHTML = `<p class="error">${e.message}</p>`;
    }
}

async function showConnectFormSettings(providerId) {
    const apiKey = prompt(`Enter API key for ${providerId}:`);
    if (!apiKey) return;
    await fetch('/api/providers/configure', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({provider: providerId, api_key: apiKey}),
    });
    await fetchModelsSettings(providerId);
    loadProvidersList();
}

async function fetchModelsSettings(providerId) {
    await fetch(`/api/providers/${providerId}/fetch-models`, {method: 'POST'});
    loadProvidersList();
}

async function disconnectProviderSettings(providerId) {
    if (!confirm(`Disconnect ${providerId}?`)) return;
    await fetch(`/api/providers/${providerId}/disconnect`, {method: 'POST'});
    loadProvidersList();
}

async function loadPlugins() {
    const el = document.getElementById('plugin-list');
    if (!el) return;
    const resp = await fetch('/api/plugins');
    const plugins = await resp.json();
    el.innerHTML = plugins.length === 0
        ? '<p class="dim">No plugins installed.</p>'
        : plugins.map(p => `<div class="setting-row">
            <span>${escapeHtml(p.name)} <span class="dim">v${escapeHtml(p.version)}</span></span>
            <button class="btn btn-small btn-danger" onclick="uninstallPlugin('${escapeHtml(p.name)}')">Remove</button>
        </div>`).join('');
}

async function installPlugin() {
    const input = document.getElementById('plugin-file');
    if (!input || !input.files[0]) return;
    const formData = new FormData();
    formData.append('file', input.files[0]);
    await fetch('/api/plugins/install', {method: 'POST', body: formData});
    loadPlugins();
}

async function uninstallPlugin(name) {
    await fetch(`/api/plugins/${name}`, {method: 'DELETE'});
    loadPlugins();
}

async function loadMcpServers() {
    const el = document.getElementById('mcp-list');
    if (!el) return;
    const resp = await fetch('/api/mcp');
    const servers = await resp.json();
    el.innerHTML = servers.length === 0
        ? '<p class="dim">No MCP servers configured.</p>'
        : servers.map(s => `<div class="setting-row">
            <span>${escapeHtml(s.name)} <span class="dim">(${escapeHtml(s.transport)})</span></span>
            <span class="dim">${(s.tools || []).length} tools</span>
            <button class="btn btn-small btn-danger" onclick="removeMcpServer('${s.id}')">Remove</button>
        </div>`).join('');
}

async function addMcpServer() {
    const name = document.getElementById('mcp-name')?.value;
    const transport = document.getElementById('mcp-transport')?.value;
    const body = {name, transport};
    if (transport === 'stdio') {
        body.command = document.getElementById('mcp-command')?.value;
        body.args = (document.getElementById('mcp-args')?.value || '').split(' ').filter(Boolean);
    } else {
        body.url = document.getElementById('mcp-url')?.value;
    }
    await fetch('/api/mcp/add', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    loadMcpServers();
}

async function removeMcpServer(id) {
    await fetch(`/api/mcp/${id}`, {method: 'DELETE'});
    loadMcpServers();
}

function toggleMcpFields() {
    const t = document.getElementById('mcp-transport')?.value;
    document.getElementById('mcp-stdio-fields').classList.toggle('hidden', t !== 'stdio');
    document.getElementById('mcp-sse-fields').classList.toggle('hidden', t !== 'sse');
}

// ── Helpers ───────────────────────────────────────────────────

function escapeHtml(text) {
    if (!text) return '';
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
}

function escapeAttr(text) {
    return (text || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function toggleThinking(el) {
    const c = el.nextElementSibling;
    if (c) c.classList.toggle('open');
}

function toggleToolCall(el) {
    const body = el.parentElement.querySelector('.tool-call-body');
    if (body) body.classList.toggle('open');
}

function toggleTheme(value) {
    document.documentElement.dataset.theme = value;
}

function filterSessions() {
    const filter = document.getElementById('model-filter')?.value;
    document.querySelectorAll('.session-item').forEach(item => {
        const badge = item.querySelector('.model-badge');
        item.style.display = (!filter || (badge && badge.textContent.includes(filter))) ? '' : 'none';
    });
}
