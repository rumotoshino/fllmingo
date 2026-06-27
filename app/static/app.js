/* ═══════════════════════════════════════════════════════════════
   FLLMingo Dashboard — app.js
   ═══════════════════════════════════════════════════════════════ */

// ─── State ───
let ws = null;
let currentPage = 'status';
let refreshInterval = null;
let catalogData = [];
let _addModelTargetTier = '';
let usagePeriod = '7d';

// ─── First-time password change ───
async function handleFirstRun() {
    const newPw = document.getElementById('firstRunNew').value.trim();
    const confirmPw = document.getElementById('firstRunConfirm').value.trim();
    const err = document.getElementById('firstRunError');
    if (newPw.length < 6) { err.textContent = '> password must be 6+ characters'; return; }
    if (newPw !== confirmPw) { err.textContent = '> passwords do not match'; return; }
    try {
        const res = await fetch('/api/settings/password', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Dashboard-Token': getDashboardToken(),
            },
            body: JSON.stringify({ current: getDashboardToken(), new: newPw }),
        });
        if (!res.ok) {
            const d = await res.json();
            err.textContent = '> error: ' + (d.detail || res.status);
            return;
        }
        // Success — update stored token, hide overlay, boot
        setDashboardToken(newPw);
        document.getElementById('firstRunOverlay').style.display = 'none';
        hideLogin();
        initApp();
    } catch (e) {
        err.textContent = '> connection failed';
    }
}

// ─── Settings: change password ───
async function changePassword() {
    const current = document.getElementById('settingsCurrent').value.trim();
    const newPw = document.getElementById('settingsNew').value.trim();
    const confirmPw = document.getElementById('settingsConfirm').value.trim();
    const status = document.getElementById('passwordStatus');
    if (newPw.length < 6) { status.innerHTML = '<span class="text-red">> password must be 6+ characters</span>'; return; }
    if (newPw !== confirmPw) { status.innerHTML = '<span class="text-red">> passwords do not match</span>'; return; }
    try {
        const res = await fetch('/api/settings/password', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Dashboard-Token': getDashboardToken(),
            },
            body: JSON.stringify({ current, new: newPw }),
        });
        if (!res.ok) {
            const d = await res.json();
            status.innerHTML = '<span class="text-red">> error: ' + escapeHtml(d.detail || res.status) + '</span>';
            return;
        }
        status.innerHTML = '<span class="text-green">> password updated. Please log in again with new token.</span>';
        // Force re-login with new token
        setTimeout(() => {
            setDashboardToken(newPw);
            location.reload();
        }, 2000);
    } catch (e) {
        status.innerHTML = '<span class="text-red">> connection failed</span>';
    }
}



// ─── Settings: Integrations (rate limit + webhook) ───
async function loadIntegrations() {
    try {
        const data = await api('/api/settings/integrations');
        document.getElementById('rateLimitEnabled').checked = data.rate_limit.enabled;
        document.getElementById('rateLimitRpm').value = data.rate_limit.requests_per_minute;
        document.getElementById('rateLimitPer').value = data.rate_limit.per;
        document.getElementById('webhookEnabled').checked = data.webhook.enabled;
        document.getElementById('webhookType').value = data.webhook.type;
        const hint = document.getElementById('webhookUrlHint');
        if (data.webhook.url_set) {
            hint.textContent = '> URL configured (' + (data.webhook.url_preview || '***') + ') — leave blank to keep';
            document.getElementById('webhookUrl').placeholder = '(unchanged — paste new to replace)';
        } else {
            hint.textContent = '> no URL set yet';
        }
    } catch (e) { console.error('Load integrations failed:', e); }
}

async function saveRateLimit() {
    const status = document.getElementById('rateLimitStatus');
    try {
        await api('/api/settings/integrations', {
            method: 'PUT',
            body: JSON.stringify({
                rate_limit: {
                    enabled: document.getElementById('rateLimitEnabled').checked,
                    requests_per_minute: parseInt(document.getElementById('rateLimitRpm').value, 10) || 60,
                    per: document.getElementById('rateLimitPer').value,
                },
            }),
        });
        status.innerHTML = '<span class="text-green">> rate limit saved ✓</span>';
        setTimeout(() => { status.textContent = ''; }, 3000);
    } catch (e) {
        status.innerHTML = '<span class="text-red">> error: ' + escapeHtml(e.message) + '</span>';
    }
}

async function saveWebhook() {
    const status = document.getElementById('webhookStatus');
    try {
        const body = {
            webhook: {
                enabled: document.getElementById('webhookEnabled').checked,
                type: document.getElementById('webhookType').value,
            },
        };
        const url = document.getElementById('webhookUrl').value.trim();
        if (url) body.webhook.url = url;
        await api('/api/settings/integrations', { method: 'PUT', body: JSON.stringify(body) });
        status.innerHTML = '<span class="text-green">> webhook saved ✓</span>';
        document.getElementById('webhookUrl').value = '';
        await loadIntegrations();
        setTimeout(() => { status.textContent = ''; }, 3000);
    } catch (e) {
        status.innerHTML = '<span class="text-red">> error: ' + escapeHtml(e.message) + '</span>';
    }
}

async function testWebhook() {
    const status = document.getElementById('webhookStatus');
    status.innerHTML = '<span class="muted">> sending test...</span>';
    try {
        await api('/api/webhook/test', { method: 'POST' });
        status.innerHTML = '<span class="text-green">> test sent ✓ check your channel</span>';
    } catch (e) {
        status.innerHTML = '<span class="text-red">> error: ' + escapeHtml(e.message) + '</span>';
    }
}




// ─── Settings: Budget ───
async function loadBudget() {
    try {
        const data = await api('/api/budget/status');
        if (!data.enabled) {
            document.getElementById('budgetEnabled').checked = false;
            return;
        }
        document.getElementById('budgetEnabled').checked = true;
        document.getElementById('budgetDaily').value = data.daily.limit;
        document.getElementById('budgetMonthly').value = data.monthly.limit;
        document.getElementById('budgetAlertAt').value = data.alert_at_percent;
        // Render usage
        const dailyPct = data.daily.limit > 0 ? (data.daily.used / data.daily.limit * 100).toFixed(1) : 0;
        const monthlyPct = data.monthly.limit > 0 ? (data.monthly.used / data.monthly.limit * 100).toFixed(1) : 0;
        document.getElementById('budgetUsage').innerHTML = `
            <div class="muted" style="font-size:11px">DAILY: $${data.daily.used.toFixed(2)} / $${data.daily.limit.toFixed(2)} (${dailyPct}%)</div>
            <div class="muted" style="font-size:11px;margin-top:4px">MONTHLY: $${data.monthly.used.toFixed(2)} / $${data.monthly.limit.toFixed(2)} (${monthlyPct}%)</div>
        `;
    } catch (e) { console.error('Budget load failed:', e); }
}

async function saveBudget() {
    const status = document.getElementById('budgetStatus');
    try {
        await api('/api/budget', {
            method: 'PUT',
            body: JSON.stringify({
                enabled: document.getElementById('budgetEnabled').checked,
                daily_limit: parseFloat(document.getElementById('budgetDaily').value) || 0,
                monthly_limit: parseFloat(document.getElementById('budgetMonthly').value) || 0,
                alert_at_percent: parseInt(document.getElementById('budgetAlertAt').value, 10) || 80,
                auto_pause: document.getElementById('budgetAutoPause').checked,
            }),
        });
        status.innerHTML = '<span class="text-green">> budget saved ✓</span>';
        await loadBudget();
        setTimeout(() => { status.textContent = ''; }, 3000);
    } catch (e) {
        status.innerHTML = '<span class="text-red">> error: ' + escapeHtml(e.message) + '</span>';
    }
}

// ─── Settings: Health Probes ───
async function saveHealthProbe() {
    const status = document.getElementById('probeStatus');
    try {
        // Health probe stored in raw config — use config endpoint to update
        const cfg = await api('/api/config');
        cfg.health_probe = {
            enabled: document.getElementById('probeEnabled').checked,
            interval_seconds: parseInt(document.getElementById('probeInterval').value, 10) || 300,
        };
        await api('/api/config', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/x-yaml' },
            body: jsonToYaml(cfg),
        });
        status.innerHTML = '<span class="text-green">> health probe saved ✓</span>';
        setTimeout(() => { status.textContent = ''; }, 3000);
    } catch (e) {
        status.innerHTML = '<span class="text-red">> error: ' + escapeHtml(e.message) + '</span>';
    }
}

// Simple JSON to YAML helper for the config endpoint
function jsonToYaml(obj, indent) {
    indent = indent || 0;
    const pad = '  '.repeat(indent);
    let yaml = '';
    if (Array.isArray(obj)) {
        for (const item of obj) {
            if (typeof item === 'object' && item !== null) {
                yaml += pad + '-\n' + jsonToYaml(item, indent + 1);
            } else {
                yaml += pad + '- ' + JSON.stringify(item) + '\n';
            }
        }
    } else if (typeof obj === 'object' && obj !== null) {
        for (const [k, v] of Object.entries(obj)) {
            if (Array.isArray(v) || (typeof v === 'object' && v !== null)) {
                yaml += pad + k + ':\n' + jsonToYaml(v, indent + 1);
            } else {
                yaml += pad + k + ': ' + JSON.stringify(v) + '\n';
            }
        }
    } else {
        yaml += JSON.stringify(obj);
    }
    return yaml;
}


// ─── Settings: Load Health Probe state ───
async function loadHealthProbe() {
    try {
        const cfg = await api('/api/config');
        const hp = (cfg && cfg.health_probe) || {};
        const en = document.getElementById('probeEnabled');
        const iv = document.getElementById('probeInterval');
        if (en) en.checked = !!hp.enabled;
        if (iv) iv.value = hp.interval_seconds || 300;
    } catch (e) { console.error('Health probe load failed:', e); }
}

// ─── Settings: Backup / Restore ───
async function downloadBackup() {
    const status = document.getElementById('backupStatus');
    try {
        const res = await fetch('/api/backup', {
            headers: { 'X-Dashboard-Token': getDashboardToken() },
        });
        if (!res.ok) throw new Error('Backup failed: ' + res.status);
        const data = await res.json();
        const blob = new Blob([data.config], { type: 'text/yaml' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `fllmingo-backup-${data.timestamp}.yaml`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        status.innerHTML = '<span class="text-green">> backup downloaded ✓</span>';
        setTimeout(() => { status.textContent = ''; }, 3000);
    } catch (e) {
        status.innerHTML = '<span class="text-red">> error: ' + escapeHtml(e.message) + '</span>';
    }
}

async function restoreBackup(event) {
    const status = document.getElementById('backupStatus');
    const file = event.target.files[0];
    if (!file) return;
    if (!confirm('Restore config from this file? Current config will be overwritten.')) {
        event.target.value = '';
        return;
    }
    try {
        const text = await file.text();
        await api('/api/restore', {
            method: 'POST',
            body: JSON.stringify({ config: text }),
        });
        status.innerHTML = '<span class="text-green">> config restored ✓ reload page</span>';
        setTimeout(() => { if (status) status.textContent = ''; }, 5000);
    } catch (e) {
        status.innerHTML = '<span class="text-red">> error: ' + escapeHtml(e.message) + '</span>';
        setTimeout(() => { if (status) status.textContent = ''; }, 5000);
    }
    event.target.value = '';
}

// ─── Settings: Export Logs ───
async function exportLogs() {
    const format = document.getElementById('exportFormat').value;
    const days = parseInt(document.getElementById('exportDays').value, 10) || 7;
    try {
        const res = await fetch(`/api/export/logs?format=${format}&days=${days}`, {
            headers: { 'X-Dashboard-Token': getDashboardToken() },
        });
        if (!res.ok) throw new Error('Export failed: ' + res.status);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `fllmingo-logs.${format}`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
    } catch (e) {
        alert('Export failed: ' + e.message);
    }
}

// ─── Settings: Latency Stats ───
async function loadLatency() {
    try {
        const period = document.getElementById('latencyPeriod')?.value || '7d';
        const data = await api(`/api/stats/latency?period=${period}`);
        const tbody = document.getElementById('latencyBody');
        if (!data.providers || !data.providers.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="muted">No data for this period</td></tr>';
            return;
        }
        tbody.innerHTML = data.providers.map(p => `
            <tr>
                <td>${escapeHtml(p.provider)}</td>
                <td>${p.count}</td>
                <td>${p.p50}ms</td>
                <td class="${p.p95 > 5000 ? 'text-amber' : ''}">${p.p95}ms</td>
                <td class="${p.p99 > 10000 ? 'text-red' : ''}">${p.p99}ms</td>
                <td class="muted">${p.avg}ms</td>
            </tr>
        `).join('');
    } catch (e) {
        console.error('Latency load failed:', e);
        const tb = document.getElementById('latencyBody');
        if (tb) tb.innerHTML = '<tr><td colspan="6" class="text-red">Failed to load: ' + escapeHtml(e.message || e) + '</td></tr>';
    }
}

// ─── Settings: Prompt Templates ───
let _editingTemplate = null;

async function loadTemplates() {
    try {
        const templates = await api('/api/templates');
        const tbody = document.getElementById('templatesBody');
        if (!templates.length) {
            tbody.innerHTML = '<tr><td colspan="4" class="muted">No templates yet</td></tr>';
            return;
        }
        tbody.innerHTML = templates.map(t => `
            <tr>
                <td class="text-cyan">${escapeHtml(t.name)}</td>
                <td class="muted">${escapeHtml(t.description || '')}</td>
                <td>${escapeHtml(t.model || '—')}</td>
                <td>
                    <button class="term-btn-sm" onclick="editTemplate('${escapeAttr(t.name)}')">[EDIT]</button>
                    <button class="term-btn-sm btn-danger" onclick="deleteTemplate('${escapeAttr(t.name)}')">[DEL]</button>
                </td>
            </tr>
        `).join('');
    } catch (e) {
        console.error('Templates load failed:', e);
        const tb = document.getElementById('templatesBody');
        if (tb) tb.innerHTML = '<tr><td colspan="4" class="text-red">Failed to load: ' + escapeHtml(e.message || e) + '</td></tr>';
    }
}

function showCreateTemplate() {
    _editingTemplate = null;
    document.getElementById('templateModalTitle').textContent = 'NEW TEMPLATE';
    document.getElementById('tplName').value = '';
    document.getElementById('tplName').disabled = false;
    document.getElementById('tplDesc').value = '';
    document.getElementById('tplSystem').value = '';
    document.getElementById('tplModel').value = '';
    document.getElementById('tplTemp').value = '';
    document.getElementById('tplMaxTokens').value = '';
    document.getElementById('templateModal').style.display = 'flex';
}

async function editTemplate(name) {
    const templates = await api('/api/templates');
    const t = templates.find(x => x.name === name);
    if (!t) return;
    _editingTemplate = name;
    document.getElementById('templateModalTitle').textContent = 'EDIT TEMPLATE';
    document.getElementById('tplName').value = t.name;
    document.getElementById('tplName').disabled = true;
    document.getElementById('tplDesc').value = t.description || '';
    document.getElementById('tplSystem').value = t.system_prompt || '';
    document.getElementById('tplModel').value = t.model || '';
    document.getElementById('tplTemp').value = t.temperature ?? '';
    document.getElementById('tplMaxTokens').value = t.max_tokens ?? '';
    document.getElementById('templateModal').style.display = 'flex';
}

async function saveTemplate() {
    const name = document.getElementById('tplName').value.trim();
    if (!name) return alert('Name is required');
    const body = {
        name,
        description: document.getElementById('tplDesc').value,
        system_prompt: document.getElementById('tplSystem').value,
        model: document.getElementById('tplModel').value,
    };
    const t = document.getElementById('tplTemp').value;
    if (t !== '') body.temperature = parseFloat(t);
    const mt = document.getElementById('tplMaxTokens').value;
    if (mt !== '') body.max_tokens = parseInt(mt, 10);
    try {
        if (_editingTemplate) {
            await api(`/api/templates/${encodeURIComponent(_editingTemplate)}`, {
                method: 'PUT',
                body: JSON.stringify(body),
            });
        } else {
            await api('/api/templates', { method: 'POST', body: JSON.stringify(body) });
        }
        closeModal('templateModal');
        loadTemplates();
    } catch (e) { alert('Failed: ' + e.message); }
}

async function deleteTemplate(name) {
    if (!confirm(`Delete template "${name}"?`)) return;
    try {
        await api(`/api/templates/${encodeURIComponent(name)}`, { method: 'DELETE' });
        loadTemplates();
    } catch (e) { alert('Failed: ' + e.message); }
}




// ─── Hamburger nav menu ───
function toggleNavMenu() {
    const menu = document.getElementById('navMoreMenu');
    if (!menu) return;
    menu.style.display = menu.style.display === 'none' ? 'flex' : 'none';
}

// Close menu when clicking outside
document.addEventListener('click', (e) => {
    const wrap = document.querySelector('.nav-more-wrap');
    const menu = document.getElementById('navMoreMenu');
    if (wrap && menu && !wrap.contains(e.target)) {
        menu.style.display = 'none';
    }
});

// Highlight ≡ when one of its pages is active
function updateNavMoreState() {
    const moreBtn = document.querySelector('.nav-more-btn');
    const menu = document.getElementById('navMoreMenu');
    if (!moreBtn || !menu) return;
    const hasActive = menu.querySelector('.nav-tab.active');
    moreBtn.classList.toggle('has-active', !!hasActive);
}




// ─── Theme selector ───
function selectTheme(name) {
    document.documentElement.setAttribute('data-theme', name);
    localStorage.setItem('fllmin' + 'go-theme', name);
    document.querySelectorAll('.theme-card').forEach(c => {
        c.classList.toggle('active', c.dataset.themeVal === name);
    });
}

function initTheme() {
    const saved = localStorage.getItem('fllmin' + 'go-theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
    document.querySelectorAll('.theme-card').forEach(c => {
        c.classList.toggle('active', c.dataset.themeVal === saved);
    });
}

// Override the old toggleTheme to cycle through main themes
function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    selectTheme(next);
}




// ─── Public Model Aliases (direct only — specific provider+model) ───
let _editingAlias = null;

async function loadAliases() {
    try {
        const aliases = await api('/api/aliases');
        const tbody = document.getElementById('aliasesBody');
        if (!aliases.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="muted">No aliases yet. Click [+ NEW ALIAS] to create one — direct routes to a specific provider+model, retries on transient failures, no fallback.</td></tr>';
            return;
        }
        tbody.innerHTML = aliases.map(a => `
            <tr>
                <td class="text-cyan"><code>${escapeHtml(a.name)}</code></td>
                <td><code>${escapeHtml(a.provider || '?')}/${escapeHtml(a.model || '?')}</code></td>
                <td>${a.max_retries ?? 2}</td>
                <td>${escapeHtml(a.display_name || a.name)}</td>
                <td class="muted">${escapeHtml(a.description || '—')}</td>
                <td>
                    <button class="term-btn-sm" onclick="editAlias('${escapeAttr(a.name)}')">[EDIT]</button>
                    <button class="term-btn-sm btn-danger" onclick="deleteAlias('${escapeAttr(a.name)}')">[DEL]</button>
                </td>
            </tr>
        `).join('');
    } catch (e) {
        console.error('Aliases load failed:', e);
        const tb = document.getElementById('aliasesBody');
        if (tb) tb.innerHTML = '<tr><td colspan="6" class="text-red">Failed to load: ' + escapeHtml(e.message || e) + '</td></tr>';
    }
}

async function _populateAliasProviderDropdown(currentProvider) {
    const sel = document.getElementById('aliasProvider');
    if (!sel) return;
    try {
        const provs = await api('/api/providers');
        const names = provs.map(p => p.name);
        sel.innerHTML = '<option value="">-- select provider --</option>' +
            names.map(n => `<option value="${escapeHtml(n)}"${n === currentProvider ? ' selected' : ''}>${escapeHtml(n)}</option>`).join('');
    } catch (e) {
        sel.innerHTML = '<option value="">(failed to load providers)</option>';
    }
}

async function populateAliasModelDropdown() {
    const prov = document.getElementById('aliasProvider').value;
    const dl = document.getElementById('aliasModelOptions');
    if (!prov || !dl) { if (dl) dl.innerHTML = ''; return; }
    if (!catalogData || !catalogData.length) {
        try { catalogData = await api('/api/catalog'); } catch (_) {}
    }
    const models = (catalogData || []).filter(m => m.provider === prov).map(m => m.id);
    dl.innerHTML = models.map(id => `<option value="${escapeHtml(id)}"></option>`).join('');
}

async function showCreateAlias() {
    _editingAlias = null;
    document.getElementById('aliasModalTitle').textContent = 'NEW ALIAS';
    document.getElementById('aliasName').value = '';
    document.getElementById('aliasName').disabled = false;
    document.getElementById('aliasDisplay').value = '';
    document.getElementById('aliasOwnedBy').value = '';
    document.getElementById('aliasDescription').value = '';
    document.getElementById('aliasModel').value = '';
    document.getElementById('aliasMaxRetries').value = 2;
    document.getElementById('aliasStatus').textContent = '';
    await _populateAliasProviderDropdown('');
    document.getElementById('aliasModal').style.display = 'flex';
}

async function editAlias(name) {
    try {
        const aliases = await api('/api/aliases');
        const a = aliases.find(x => x.name === name);
        if (!a) return alert('Alias not found');
        _editingAlias = name;
        document.getElementById('aliasModalTitle').textContent = `EDIT ALIAS — ${name}`;
        document.getElementById('aliasName').value = a.name;
        document.getElementById('aliasName').disabled = false;
        document.getElementById('aliasDisplay').value = a.display_name || '';
        document.getElementById('aliasOwnedBy').value = a.owned_by || '';
        document.getElementById('aliasDescription').value = a.description || '';
        document.getElementById('aliasStatus').textContent = '';
        await _populateAliasProviderDropdown(a.provider || '');
        document.getElementById('aliasModel').value = a.model || '';
        document.getElementById('aliasMaxRetries').value = a.max_retries ?? 2;
        await populateAliasModelDropdown();
        document.getElementById('aliasModal').style.display = 'flex';
    } catch (e) { alert('Failed: ' + e.message); }
}

async function saveAlias() {
    const name = document.getElementById('aliasName').value.trim();
    const status = document.getElementById('aliasStatus');
    if (!name) { status.innerHTML = '<span class="text-red">> name is required</span>'; return; }

    const provider = document.getElementById('aliasProvider').value;
    const model = document.getElementById('aliasModel').value.trim();
    const maxRetries = parseInt(document.getElementById('aliasMaxRetries').value, 10);
    if (!provider) { status.innerHTML = '<span class="text-red">> provider required</span>'; return; }
    if (!model) { status.innerHTML = '<span class="text-red">> model required</span>'; return; }

    const body = {
        provider,
        model,
        max_retries: Number.isFinite(maxRetries) ? maxRetries : 2,
        display_name: document.getElementById('aliasDisplay').value.trim(),
        owned_by: document.getElementById('aliasOwnedBy').value.trim() || 'fllmingo',
        description: document.getElementById('aliasDescription').value.trim(),
    };

    try {
        if (_editingAlias) {
            if (name !== _editingAlias) body.rename = name;
            await api(`/api/aliases/${encodeURIComponent(_editingAlias)}`, {
                method: 'PUT',
                body: JSON.stringify(body),
            });
        } else {
            body.name = name;
            await api('/api/aliases', { method: 'POST', body: JSON.stringify(body) });
        }
        closeModal('aliasModal');
        loadAliases();
    } catch (e) {
        status.innerHTML = '<span class="text-red">> ' + escapeHtml(e.message) + '</span>';
    }
}

async function deleteAlias(name) {
    if (!confirm(`Delete alias "${name}"? Clients calling this name will start getting 404s.`)) return;
    try {
        await api(`/api/aliases/${encodeURIComponent(name)}`, { method: 'DELETE' });
        loadAliases();
    } catch (e) { alert('Failed: ' + e.message); }
}


// ─── Init ───
function initApp() {
    initTheme();
    initNav();
    connectWS();
    loadStatus();
    loadUsage();
    refreshInterval = setInterval(() => { loadStatus(); loadUsage(); }, 5000);
}

document.addEventListener('DOMContentLoaded', () => {
    // Restore theme preference
    const saved = localStorage.getItem('fllmingo-theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);

    // Check if already authenticated (e.g. page refresh with session token)
    const existingToken = getDashboardToken();
    if (existingToken) {
        // Verify token is still valid
        fetch('/api/status', { headers: { 'X-Dashboard-Token': existingToken } })
            .then(res => {
                if (res.ok) {
                    hideLogin();
                    initApp();
                } else {
                    sessionStorage.removeItem('llm-router-dashboard-token');
                    showLogin();
                }
            })
            .catch(() => showLogin());
    } else {
        showLogin();
    }
});

// ─── Theme Toggle ───
// ─── Navigation ───
function initNav() {
    document.querySelectorAll('.nav-tab, .bn-tab').forEach(btn => {
        // Skip the hamburger button (has no data-page)
        if (btn.classList.contains('nav-more-btn')) return;
        btn.addEventListener('click', () => switchPage(btn.dataset.page));
    });
}

function switchPage(page) {
 if (!page) return; // Guard against undefined (e.g. hamburger button)
 currentPage = page;
 document.querySelectorAll('.nav-tab, .bn-tab').forEach(t => {
 if (t.classList.contains('nav-more-btn')) return;
 t.classList.toggle('active', t.dataset.page === page);
 });
 document.querySelectorAll('.page').forEach(p => {
 p.classList.toggle('active', p.id === `page-${page}`);
 });
 switch (page) {
 case 'status': loadStatus(); loadUsage(); rehydrateLiveFeed(); break;
 case 'providers': loadProviders(); break;
 case 'tiers': loadTiers(); loadAliases(); break;
 case 'logs': loadLogs(); break;
 case 'catalog': loadCatalog(); break;
 case 'leaderboard': loadLeaderboard(); break;
 case 'config': loadConfig(); break;
 case 'settings':
  loadApiKeys();
  loadIntegrations();
  loadBudget();
  loadLatency();
  loadTemplates();
  loadHealthProbe();
            loadCircuitBreaker();
  break;
 }
 if (typeof updateNavMoreState === 'function') updateNavMoreState();
 // Close hamburger menu after a tab is picked
 const m = document.getElementById('navMoreMenu');
 if (m) m.style.display = 'none';
}

// ─── WebSocket live feed ───
function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.onopen = () => {
        document.getElementById('statusDot').className = 'status-dot online';
        document.getElementById('statusText').textContent = 'SYSTEM ACTIVE';
    };
    ws.onmessage = (e) => { try { handleWSMessage(JSON.parse(e.data)); } catch (err) { console.warn('Bad WS payload:', err, e.data); } };
    ws.onclose = () => {
        document.getElementById('statusDot').className = 'status-dot error';
        document.getElementById('statusText').textContent = 'DISCONNECTED';
        setTimeout(connectWS, 3000);
    };
    ws.onerror = () => ws.close();
}

// Buffer of recent live events so feed survives page switches
const _liveBuffer = [];
const _LIVE_BUFFER_MAX = 100;

function handleWSMessage(msg) {
    const { type, data } = msg;
    let cls = '', text = '';

    switch (type) {
        case 'status':
            if (data.phase === 'attempt') { text = `→ trying ${data.model} via ${data.provider} (#${data.attempt})`; }
            else if (data.phase === 'failed') { text = `✗ ${data.provider} ${data.status}: ${data.error}`; cls = 'fail'; }
            else if (data.phase === 'retry_strip') { text = `↻ retrying, stripped: ${(data.stripped || []).join(', ')}`; cls = 'retry'; }
            else if (data.phase === 'skip') { text = `⊘ ${data.provider} quarantined`; cls = 'fail'; }
            else return;
            break;
        case 'done':
            text = `✓ ${data.model} via ${data.provider} — ${data.latency_ms}ms`;
            if (typeof data.cost === 'number') text += ` $${data.cost.toFixed(4)}`;
            if (typeof data.completion_tokens === 'number' && data.completion_tokens > 0) {
                text += ` (${data.completion_tokens} tok)`;
            }
            if (data.retried) text += ' [RETRIED]';
            cls = 'success';
            // Refresh status numbers — wrapped in try so a failure won't kill WS
            try { loadStatus(); } catch (_) {}
            try { if (typeof loadUsage === 'function') loadUsage(); } catch (_) {}
            break;
        case 'error': text = `✗ FAILED: ${data.message || data.detail || 'unknown'}`; cls = 'fail'; break;
        default: return;
    }

    const now = new Date().toLocaleTimeString('en-US', { hour12: false });
    const lineText = `${now} ${text}`;
    // Always buffer
    _liveBuffer.push({ text: lineText, cls });
    if (_liveBuffer.length > _LIVE_BUFFER_MAX) _liveBuffer.shift();

    // Render only if liveStream is currently mounted
    const stream = document.getElementById('liveStream');
    if (!stream) return;
    const line = document.createElement('div');
    line.className = `log-line ${cls}`;
    line.textContent = lineText;
    if (stream.children.length > _LIVE_BUFFER_MAX) stream.removeChild(stream.firstChild);
    stream.appendChild(line);
    stream.scrollTop = stream.scrollHeight;
}

// Re-render buffered events when STATUS page is re-entered
function rehydrateLiveFeed() {
    const stream = document.getElementById('liveStream');
    if (!stream) return;
    stream.innerHTML = '';
    if (!_liveBuffer.length) {
        const empty = document.createElement('div');
        empty.className = 'log-line muted';
        empty.textContent = '$ awaiting data...';
        stream.appendChild(empty);
        return;
    }
    for (const { text, cls } of _liveBuffer) {
        const line = document.createElement('div');
        line.className = `log-line ${cls}`;
        line.textContent = text;
        stream.appendChild(line);
    }
    stream.scrollTop = stream.scrollHeight;
}

// ─── Auth: Login flow ───
const TOKEN_KEY = 'fllmingo-dashboard-token';

function getDashboardToken() {
    return localStorage.getItem(TOKEN_KEY) || sessionStorage.getItem(TOKEN_KEY) || '';
}

function setDashboardToken(token, remember) {
    clearDashboardToken();
    if (remember) {
        localStorage.setItem(TOKEN_KEY, token);
    } else {
        sessionStorage.setItem(TOKEN_KEY, token);
    }
}

function clearDashboardToken() {
    localStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(TOKEN_KEY);
    // Migrate old key if present
    localStorage.removeItem('llm-router-dashboard-token');
    sessionStorage.removeItem('llm-router-dashboard-token');
}

function logout() {
    clearDashboardToken();
    location.reload();
}

function showLogin(message) {
    document.getElementById('loginOverlay').style.display = 'flex';
    document.getElementById('app').style.display = 'none';
    const err = document.getElementById('loginError');
    err.textContent = message || '';
    const input = document.getElementById('loginToken');
    input.value = '';
    input.focus();
}

function hideLogin() {
    document.getElementById('loginOverlay').style.display = 'none';
    document.getElementById('app').style.display = '';
}

async function handleLogin() {
    const input = document.getElementById('loginToken');
    const err = document.getElementById('loginError');
    const token = input.value.trim();
    if (!token) {
        err.textContent = '> token required';
        return;
    }
    // Test token against health endpoint (no auth required, but test against a protected one)
    try {
        const res = await fetch('/api/status', {
            headers: { 'X-Dashboard-Token': token },
        });
        if (res.status === 401) {
            err.textContent = '> access denied: invalid token';
            input.select();
            return;
        }
        if (!res.ok) {
            err.textContent = '> server error: ' + res.status;
            return;
        }
        // Success
        const remember = document.getElementById('loginRemember')?.checked || false;
        setDashboardToken(token, remember);
        // Check if first-time (default token) before entering
        try {
            const sr = await fetch('/api/auth/status');
            const sd = await sr.json();
            if (sd.needs_password_change) {
                document.getElementById('loginOverlay').style.display = 'none';
                document.getElementById('firstRunOverlay').style.display = 'flex';
                document.getElementById('firstRunNew').focus();
                return;
            }
        } catch (_) {}
        hideLogin();
        initApp();
    } catch (e) {
        err.textContent = '> connection failed';
    }
}

// Enter key submits login
document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && document.getElementById('loginOverlay').style.display !== 'none') {
        handleLogin();
    }
});

// ─── API helper ───
async function api(path, opts = {}) {
    const headers = { 'Content-Type': 'application/json', ...opts.headers };
    const dashToken = getDashboardToken();
    if (dashToken) headers['X-Dashboard-Token'] = dashToken;
    const res = await fetch(path, { headers, ...opts });
    if (res.status === 401) {
        showLogin('session expired — re-authenticate');
        throw new Error('Authentication required');
    }
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
}

// ─── Status page ───
async function loadStatus() {
    try {
        const data = await api('/api/status');
        document.getElementById('statRequests').textContent = data.stats.total.toLocaleString();
        document.getElementById('statCost').textContent = `$${data.stats.total_cost.toFixed(2)}`;

        const errEl = document.getElementById('statErrors');
        errEl.textContent = `${data.stats.error_rate}%`;
        errEl.className = `stat-value ${data.stats.error_rate > 5 ? 'red' : data.stats.error_rate > 2 ? 'amber' : ''}`;

        const tokens = data.stats.prompt_tokens + data.stats.completion_tokens;
        document.getElementById('statTokens').textContent = tokens > 1000 ? `${(tokens/1000).toFixed(1)}k` : tokens.toLocaleString();

        // Health table
        const tbody = document.getElementById('healthBody');
        if (!data.providers.length) {
            tbody.innerHTML = '<tr><td colspan="5" class="muted">No provider data yet</td></tr>';
        } else {
            tbody.innerHTML = data.providers.map(p => {
                const bc = p.status === 'healthy' ? 'badge-online' : p.status === 'quarantined' ? 'badge-offline' : 'badge-degraded';
                return `<tr>
                    <td>${escapeHtml(p.provider)}</td>
                    <td><span class="badge ${bc}">${escapeHtml(p.status).toUpperCase()}</span></td>
                    <td>${p.total_requests || 0}</td>
                    <td>${p.total_failures || 0}</td>
                    <td class="muted">${p.last_failure ? new Date(p.last_failure).toLocaleTimeString() : '—'}</td>
                </tr>`;
            }).join('');
        }
    } catch (e) { console.error('Status load failed:', e); }
}

// ─── Providers page ───
let _editingProvider = null;

async function loadProviders() {
    try {
        const providers = await api('/api/providers');
        const container = document.getElementById('providersList');

        if (providers.length === 0) {
            container.innerHTML = '<div class="muted" style="padding:16px">No providers configured. Click [+ ADD PROVIDER] to get started.</div>';
            return;
        }

        container.innerHTML = providers.map(p => {
            const statusBadge = p.status === 'healthy' ? 'badge-online'
                : p.status === 'quarantined' ? 'badge-offline' : 'badge-degraded';
            const statusLabel = (p.status || 'unknown').toUpperCase();
            return `<div class="provider-card">
                <div class="provider-card-header">
                    <span class="provider-card-name">${escapeHtml(p.name)}</span>
                    <div class="provider-card-actions">
                        <span class="badge ${statusBadge}">${statusLabel}</span>
                        <button class="term-btn-sm" onclick="editProvider('${escapeAttr(p.name)}')">[EDIT]</button>
                        <button class="term-btn-sm btn-danger" onclick="deleteProvider('${escapeAttr(p.name)}')">[DELETE]</button>
                    </div>
                </div>
                <div class="provider-card-details">
                    <div><span class="provider-detail-label">ENDPOINT </span><span class="provider-detail-value">${escapeHtml(p.endpoint)}</span></div>
                    <div><span class="provider-detail-label">KEY </span><span class="provider-detail-value">${escapeHtml(p.key_masked || '—')}</span></div>
                    <div><span class="provider-detail-label">TYPE </span><span class="provider-detail-value">${escapeHtml(p.type)}</span></div>
                    <div><span class="provider-detail-label">TIMEOUT </span><span class="provider-detail-value">${p.timeout}s</span></div>
                    <div><span class="provider-detail-label">RETRIES </span><span class="provider-detail-value">${p.max_retries}</span></div>
                    <div><span class="provider-detail-label">MODELS </span><span class="provider-detail-value">${p.model_count}</span></div>
                    <div><span class="provider-detail-label">OVERRIDES </span><span class="provider-detail-value">${p.overrides || 0}</span></div>
                    <div><span class="provider-detail-label">REQUESTS </span><span class="provider-detail-value">${p.total_requests}</span></div>
                    <div><span class="provider-detail-label">FAILURES </span><span class="provider-detail-value">${p.total_failures}</span></div>
                </div>
            </div>`;
        }).join('');
    } catch (e) { console.error('Providers load failed:', e); }
}

function showAddProvider() {
    _editingProvider = null;
    document.getElementById('providerModalTitle').textContent = 'ADD PROVIDER';
    document.getElementById('provName').value = '';
    document.getElementById('provName').disabled = false;
    document.getElementById('provEndpoint').value = '';
    document.getElementById('provKey').value = '';
    document.getElementById('provKey').placeholder = 'sk-... (stored in .env)';
    document.getElementById('provType').value = 'openai';
    document.getElementById('provTimeout').value = '60';
    document.getElementById('provRetries').value = '2';
    document.getElementById('providerModal').style.display = '';
}

async function editProvider(name) {
    _editingProvider = name;
    const providers = await api('/api/providers');
    const p = providers.find(x => x.name === name);
    if (!p) return;

    document.getElementById('providerModalTitle').textContent = `EDIT: ${name}`;
    document.getElementById('provName').value = name;
    document.getElementById('provName').disabled = true;
    document.getElementById('provEndpoint').value = p.endpoint;
    document.getElementById('provKey').value = '';
    document.getElementById('provKey').placeholder = p.key_masked || 'enter new key to update...';
    document.getElementById('provType').value = p.type;
    document.getElementById('provTimeout').value = p.timeout;
    document.getElementById('provRetries').value = p.max_retries;
    document.getElementById('providerModal').style.display = '';
}

function closeProviderModal() {
    document.getElementById('providerModal').style.display = 'none';
    _editingProvider = null;
}

async function saveProvider() {
    const name = document.getElementById('provName').value.trim();
    const endpoint = document.getElementById('provEndpoint').value.trim();
    const key = document.getElementById('provKey').value.trim();
    const type = document.getElementById('provType').value;
    const timeout = parseInt(document.getElementById('provTimeout').value) || 60;
    const retries = parseInt(document.getElementById('provRetries').value) || 2;

    if (!name) return alert('Provider name is required');
    if (!endpoint) return alert('Endpoint URL is required');

    const body = { endpoint, type, timeout, max_retries: retries };
    if (key) body.key = key;

    try {
        if (_editingProvider) {
            await api(`/api/providers/${_editingProvider}`, {
                method: 'PUT',
                body: JSON.stringify(body),
            });
        } else {
            await api(`/api/providers/${name}`, {
                method: 'POST',
                body: JSON.stringify(body),
            });
        }
        closeProviderModal();
        loadProviders();
    } catch (e) {
        alert(`Error: ${e.message}`);
    }
}

async function deleteProvider(name) {
    if (!confirm(`Delete provider "${name}"?\nThis will also remove it from any tiers.`)) return;
    try {
        const result = await api(`/api/providers/${name}`, { method: 'DELETE' });
        if (result.tier_refs_removed?.length) {
            alert(`Removed from tiers: ${result.tier_refs_removed.join(', ')}`);
        }
        loadProviders();
    } catch (e) {
        alert(`Error: ${e.message}`);
    }
}

// ═══════════════════════════════════════════════════════════════
//  TIERS — full CRUD
// ═══════════════════════════════════════════════════════════════

async function loadTiers() {
    try {
        const tiers = await api('/api/tiers');
        const cfg = await api('/api/config');
        const aliases = cfg.routing?.aliases || {};
        const container = document.getElementById('tiersContainer');

        // Clear previous render to prevent stacking
        container.innerHTML = '';

        // Reverse lookup
        const tierAliases = {};
        for (const [a, t] of Object.entries(aliases)) { (tierAliases[t] ??= []).push(a); }

        for (const [name, tier] of Object.entries(tiers)) {
          const models = (tier.models || []).map((m, i) => `
            <div class="tier-model" data-index="${i}" data-tier="${name}">
              <span class="drag-handle" title="Drag to reorder">⋮⋮</span>
              <span class="priority">#${i + 1}</span>
              <span class="model-name">${escapeHtml(m.model)}</span>
              <span class="tier-model-right">
                <span class="provider">via ${escapeHtml(m.provider)}</span>
                <button class="remove-btn" data-tier="${name}" data-index="${i}" title="Remove">✕</button>
              </span>
            </div>`).join('');

          const aliasStr = tierAliases[name] ? ` <span class="muted">(${tierAliases[name].join(', ')})</span>` : '';

          const card = document.createElement('div');
          card.className = 'tier-card';
          card.dataset.tierName = name;
          card.innerHTML = `
            <div class="tier-header">
              <span>${name.toUpperCase()}${aliasStr}</span>
              <span>
                <button class="term-btn-sm" data-add="${name}">[+ MODEL]</button>
                <button class="term-btn-sm text-red" data-delete="${name}">[DELETE]</button>
              </span>
            </div>
            ${models || '<div class="tier-model muted" style="padding:12px">No models — add one!</div>'}`;

          if (tier.models && tier.models.length) {
            container.appendChild(card);
          }
        }

        initTierDelegation();
    } catch (e) { console.error('Tiers load failed:', e); }
}

function initTierDelegation() {
 const c = document.getElementById('tiersContainer');
 if (!c) return;
 // Remove old listeners
 if (c._clickHandler) c.removeEventListener('click', c._clickHandler);
 if (c._pointerHandler) c.removeEventListener('pointerdown', c._pointerHandler);
 // Click handler for buttons (add/delete/remove)
 const clickHandler = async (e) => {
  const addBtn = e.target.closest('button[data-add]');
  if (addBtn) { _addModelTargetTier = addBtn.dataset.add; showAddModelModal(_addModelTargetTier); return; }
  const deleteBtn = e.target.closest('button[data-delete]');
  if (deleteBtn) { deleteTier(deleteBtn.dataset.delete); return; }
  const removeBtn = e.target.closest('button.remove-btn');
  if (removeBtn) { await removeModelFromTier(removeBtn.dataset.tier, parseInt(removeBtn.dataset.index, 10)); return; }
 };
 c._clickHandler = clickHandler;
 c.addEventListener('click', clickHandler);
 // Pointer drag handler (handles both mouse + touch via Pointer Events API)
 c._pointerHandler = startTierDrag;
 c.addEventListener('pointerdown', startTierDrag);
}

// ─── Drag-and-drop reorder via handle ───
let _tierDrag = null;

function startTierDrag(e) {
 const handle = e.target.closest('.drag-handle');
 if (!handle) return;
 const row = handle.closest('.tier-model');
 if (!row) return;
 e.preventDefault();

 const tier = row.dataset.tier;
 const index = parseInt(row.dataset.index, 10);
 const rect = row.getBoundingClientRect();
 const offsetY = e.clientY - rect.top;

 // Create floating clone
 const clone = row.cloneNode(true);
 clone.classList.add('drag-clone');
 clone.style.width = rect.width + 'px';
 clone.style.left = rect.left + 'px';
 clone.style.top = rect.top + 'px';
 document.body.appendChild(clone);

 row.classList.add('dragging');

 _tierDrag = {
  sourceRow: row,
  sourceTier: tier,
  sourceIndex: index,
  clone,
  offsetY,
  targetTier: tier,
  targetIndex: index,
  indicator: null,
 };

 // Capture pointer so we keep getting events even outside the handle
 try { handle.setPointerCapture(e.pointerId); } catch (_) {}
 handle.addEventListener('pointermove', onTierDragMove);
 handle.addEventListener('pointerup', onTierDragEnd);
 handle.addEventListener('pointercancel', onTierDragEnd);
 _tierDrag.handle = handle;
}

function onTierDragMove(e) {
 if (!_tierDrag) return;
 e.preventDefault();
 _tierDrag.clone.style.top = (e.clientY - _tierDrag.offsetY) + 'px';
 _tierDrag.clone.style.left = _tierDrag.sourceRow.getBoundingClientRect().left + 'px';

 // Find the row under the pointer (excluding the clone)
 _tierDrag.clone.style.pointerEvents = 'none';
 const el = document.elementFromPoint(e.clientX, e.clientY);
 _tierDrag.clone.style.pointerEvents = '';
 if (!el) return;
 const overRow = el.closest('.tier-model');
 const overCard = el.closest('.tier-card');
 if (!overCard) return;

 // Clear previous indicator
 document.querySelectorAll('.drop-above, .drop-below').forEach(n => {
  n.classList.remove('drop-above', 'drop-below');
 });

 if (overCard && overCard.querySelector) {
  // Find target position by comparing pointer Y to each row's midpoint
  _tierDrag.targetTier = overCard.dataset.tierName;
  const rows = Array.from(overCard.querySelectorAll('.tier-model'))
   .filter(r => r !== _tierDrag.sourceRow);
  if (rows.length === 0) {
   _tierDrag.targetIndex = 0;
  } else {
   let inserted = false;
   for (const r of rows) {
    const rect = r.getBoundingClientRect();
    if (e.clientY < rect.top + rect.height / 2) {
     r.classList.add('drop-above');
     _tierDrag.targetIndex = parseInt(r.dataset.index, 10);
     inserted = true;
     break;
    }
   }
   if (!inserted) {
    // Below all rows — append at end
    const last = rows[rows.length - 1];
    last.classList.add('drop-below');
    _tierDrag.targetIndex = parseInt(last.dataset.index, 10) + 1;
   }
  }
 }
}

async function onTierDragEnd(e) {
 if (!_tierDrag) return;
 const drag = _tierDrag;
 _tierDrag = null;

 // Cleanup visual state
 drag.sourceRow.classList.remove('dragging');
 drag.clone.remove();
 document.querySelectorAll('.drop-above, .drop-below').forEach(n => {
  n.classList.remove('drop-above', 'drop-below');
 });
 if (drag.handle) {
  drag.handle.removeEventListener('pointermove', onTierDragMove);
  drag.handle.removeEventListener('pointerup', onTierDragEnd);
  drag.handle.removeEventListener('pointercancel', onTierDragEnd);
 }

 // Skip if no real change
 if (drag.sourceTier === drag.targetTier && drag.sourceIndex === drag.targetIndex) return;

 // Commit via API
 try {
  await api(`/api/tiers/${encodeURIComponent(drag.sourceTier)}/models/move`, {
   method: 'POST',
   body: JSON.stringify({
    source_tier: drag.sourceTier,
    source_index: drag.sourceIndex,
    target_tier: drag.targetTier,
    target_index: drag.targetIndex,
   }),
  });
  loadTiers();
 } catch (e) {
  alert('Move failed: ' + e.message);
  loadTiers();
 }
}

async function deleteTier(name) {
    if (!confirm(`Delete tier "${name}"?`)) return;
    try {
        await api(`/api/tiers/${name}`, { method: 'DELETE' });
        loadTiers();
    } catch (e) { alert(`Failed: ${e.message}`); }
}

async function removeModelFromTier(tierName, index) {
    try {
        const tiers = await api('/api/tiers');
        const tier = tiers[tierName];
        if (!tier) return;
        tier.models.splice(index, 1);
        await api(`/api/tiers/${tierName}`, {
            method: 'PUT',
            body: JSON.stringify({ models: tier.models }),
        });
        loadTiers();
    } catch (e) { alert(`Failed: ${e.message}`); }
}

function showAddTierModal() {
    document.getElementById('newTierName').value = '';
    document.getElementById('addTierModal').style.display = 'flex';
}

async function createTier() {
    const name = document.getElementById('newTierName').value.trim().toLowerCase();
    if (!name) return alert('Enter a tier name');
    try {
        await api(`/api/tiers/${name}`, {
            method: 'POST',
            body: JSON.stringify({ models: [], strategy: 'fallback' }),
        });
        closeModal('addTierModal');
        loadTiers();
    } catch (e) { alert(`Failed: ${e.message}`); }
}

// ─── Add model to tier modal ───
async function showAddModelModal(tierName) {
    _addModelTargetTier = tierName;
    document.getElementById('addModelTierName').textContent = tierName.toUpperCase();

    // Load catalog if empty
    if (!catalogData.length) {
        try { catalogData = await api('/api/catalog'); } catch {}
    }

    // Populate provider dropdown
    const provSelect = document.getElementById('addModelProvider');
    const providers = [...new Set(catalogData.map(m => m.provider))];
    provSelect.innerHTML = '<option value="">-- select --</option>' +
        providers.map(p => `<option value="${p}">${p}</option>`).join('');

    document.getElementById('addModelSelect').innerHTML = '<option value="">-- select provider first --</option>';
    document.getElementById('addModelModal').style.display = 'flex';
}

function filterModelsByProvider() {
    const prov = document.getElementById('addModelProvider').value;
    const select = document.getElementById('addModelSelect');
    if (!prov) {
        select.innerHTML = '<option value="">-- select provider first --</option>';
        return;
    }
    const models = catalogData.filter(m => m.provider === prov);
    select.innerHTML = '<option value="">-- select --</option>' +
        models.map(m => `<option value="${m.id}">${m.id}${m.in_tier ? ' ★' : ''}</option>`).join('');
}

async function addModelToTier() {
    const provider = document.getElementById('addModelProvider').value;
    const model = document.getElementById('addModelSelect').value;
    if (!provider || !model) return alert('Select provider and model');

    try {
        const tiers = await api('/api/tiers');
        const tier = tiers[_addModelTargetTier];
        if (!tier) return alert('Tier not found');
        tier.models.push({ provider, model });
        await api(`/api/tiers/${_addModelTargetTier}`, {
            method: 'PUT',
            body: JSON.stringify({ models: tier.models }),
        });
        closeModal('addModelModal');
        loadTiers();
    } catch (e) { alert(`Failed: ${e.message}`); }
}

function closeModal(id) {
 document.getElementById(id).style.display = 'none';
}


// ─── Logs page
async function loadLogs() {
 try {
        const rows = await api('/api/requests?limit=100');
        const tbody = document.getElementById('logsBody');
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="8" class="muted">No requests logged yet</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map(r => {
            const ts = r.timestamp ? new Date(r.timestamp).toLocaleTimeString('en-US', { hour12: false }) : '—';
            const sc = r.status_code < 300 ? 'badge-200' : r.status_code < 500 ? 'badge-400' : 'badge-500';
            return `<tr class="clickable" onclick="inspectRequest(${r.id})">
                <td class="muted">${r.id}</td>
                <td>${ts}</td>
                <td class="text-cyan">${r.resolved_model || r.incoming_model || '—'}</td>
                <td>${r.resolved_provider || '—'}</td>
                <td class="${sc}">${r.status_code || '—'}</td>
                <td>${r.latency_ms ? r.latency_ms + 'ms' : '—'}</td>
                <td>${r.cost ? '$' + r.cost.toFixed(4) : '—'}</td>
                <td>${r.retried ? '↻' : ''}</td>
            </tr>`;
        }).join('');
    } catch (e) { console.error('Logs load failed:', e); }
}

// ─── Inspect page ───
function inspectRequest(id) {
    document.getElementById('inspectId').value = id;
    switchPage('inspect');
    loadInspect();
}

async function loadInspect() {
    const id = document.getElementById('inspectId').value;
    if (!id) return;
    const el = document.getElementById('inspectContent');
    try {
        const d = await api(`/api/requests/${id}`);
        let out = `╔══ REQUEST #${d.id} ══╗\n`;
        out += `  Timestamp   : ${d.timestamp}\n`;
        out += `  Incoming    : ${d.incoming_model}\n`;
        out += `  Resolved    : ${d.resolved_model} via ${d.resolved_provider}\n`;
        out += `  Tier        : ${d.tier}\n`;
        out += `  Status      : ${d.status_code}\n`;
        out += `  Latency     : ${d.latency_ms}ms\n`;
        out += `  Tokens      : ${d.prompt_tokens} in / ${d.completion_tokens} out\n`;
        out += `  Cost        : $${(d.cost || 0).toFixed(6)}\n`;
        out += `  Retried     : ${d.retried ? 'YES' : 'no'}\n`;
        if (d.stripped_params) out += `  Stripped    : ${d.stripped_params}\n`;
        if (d.error) out += `\n╔══ ERROR ══╗\n${d.error}\n`;
        if (d.request_body) {
            try { out += `\n╔══ REQUEST BODY ══╗\n${JSON.stringify(JSON.parse(d.request_body), null, 2)}\n`; }
            catch { out += `\n╔══ REQUEST BODY ══╗\n${d.request_body}\n`; }
        }
        if (d.response_excerpt) out += `\n╔══ RESPONSE ══╗\n${d.response_excerpt}\n`;
        el.textContent = out;
        el.className = '';
    } catch (e) {
        el.textContent = `$ error: request #${id} not found`;
        el.className = 'text-red';
    }
}

// ═══════════════════════════════════════════════════════════════
// USAGE + LEADERBOARD
// ═══════════════════════════════════════════════════════════════

function setUsagePeriod(value) {
 usagePeriod = value;
 loadUsage();
}

async function loadUsage() {
 try {
 const data = await api(`/api/stats/tokens`);
 const periods = data.periods || {};
 const current = periods[usagePeriod] || { total: 0, prompt: 0, completion: 0 };
 document.getElementById('usageTokens').textContent = formatNumber(current.total);
 document.getElementById('usagePrompt').textContent = formatNumber(current.prompt);
 document.getElementById('usageCompletion').textContent = formatNumber(current.completion);
 // usageCost card removed — backend doesn't track cost in /api/stats/tokens
 } catch (e) { console.error('Usage load failed:', e); }
}

async function loadLeaderboard() {
 try {
 const sort = document.getElementById('leaderboardSort')?.value || 'requests';
 const period = document.getElementById('leaderboardPeriod')?.value || usagePeriod;
 const rows = await api(`/api/stats/leaderboard?sort=${sort}&period=${period}&dir=desc`);
 const tbody = document.getElementById('leaderboardBody');
 if (!rows.length) {
 tbody.innerHTML = '<tr><td colspan="7" class="muted">No usage data yet</td></tr>';
 return;
 }
 tbody.innerHTML = rows.map((row, idx) => {
 return `<tr>
 <td class="muted">${idx + 1}</td>
 <td class="text-cyan">${escapeHtml(row.model)}</td>
 <td>${escapeHtml(row.provider)}</td>
 <td class="text-cyan">${formatNumber(row.requests)}</td>
 <td class="text-green">${formatNumber(row.prompt_tokens)}</td>
 <td class="text-amber">${formatNumber(row.completion_tokens)}</td>
 <td class="text-cyan">${formatNumber(row.total_tokens)}</td>
 </tr>`;
 }).join('');
 } catch (e) { console.error('Leaderboard load failed:', e); }
}

// ═══════════════════════════════════════════════════════════════
// CATALOG — enhanced rendering
// ═══════════════════════════════════════════════════════════════

let _catalogPage = 1;

function renderCatalog() {
 const search = (document.getElementById('catalogSearch')?.value || '').toLowerCase();
 const providerFilter = document.getElementById('catalogProviderFilter')?.value || '';
 const sortMode = document.getElementById('catalogSort')?.value || 'az';
 const pageSize = document.getElementById('catalogPageSize')?.value || '50';
 const tbody = document.getElementById('catalogBody');
 const paginationEl = document.getElementById('catalogPagination');

 // Populate provider dropdown once
 const providers = [...new Set((catalogData || []).map(m => m.provider))].sort();
 const provSelect = document.getElementById('catalogProviderFilter');
 if (provSelect && !provSelect.dataset.populated) {
 providers.forEach(p => {
 const opt = document.createElement('option');
 opt.value = p;
 opt.textContent = p;
 provSelect.appendChild(opt);
 });
 provSelect.dataset.populated = '1';
 }

 // Filter
 let filtered = (catalogData || []).filter(m => {
 const matchesSearch = !search || [m.id, m.provider].some(v => String(v).toLowerCase().includes(search));
 const matchesProvider = !providerFilter || m.provider === providerFilter;
 return matchesSearch && matchesProvider;
 });
 if (sortMode === 'za') filtered.sort((a, b) => String(b.id).localeCompare(String(a.id)));
 else filtered.sort((a, b) => String(a.id).localeCompare(String(b.id)));

 if (!filtered.length) {
 tbody.innerHTML = `<tr><td colspan="7" class="muted">${catalogData.length ? 'No matches' : 'Click refresh to load'}</td></tr>`;
 if (paginationEl) paginationEl.style.display = 'none';
 return;
 }

 // Pagination
 let pageRows = filtered;
 if (pageSize !== 'all') {
 const size = parseInt(pageSize, 10) || 50;
 const totalPages = Math.max(1, Math.ceil(filtered.length / size));
 if (_catalogPage > totalPages) _catalogPage = totalPages;
 if (_catalogPage < 1) _catalogPage = 1;
 const start = (_catalogPage - 1) * size;
 pageRows = filtered.slice(start, start + size);
 // Update pagination UI
 if (paginationEl) {
 paginationEl.style.display = 'flex';
 document.getElementById('catalogPageInfo').textContent =
 `Page ${_catalogPage} / ${totalPages} · ${filtered.length} models`;
 document.getElementById('catalogPrev').disabled = _catalogPage === 1;
 document.getElementById('catalogNext').disabled = _catalogPage === totalPages;
 }
 } else if (paginationEl) {
 paginationEl.style.display = 'none';
 }

 tbody.innerHTML = pageRows.map((m, idx) => {
 const fmt = (n) => n == null ? '—' : formatNumber(n);
 const fmtCurrency = (n) => n == null ? '—' : `$${n.toFixed(4)}`;
 const rowId = `cat-row-${idx}`;
 const detailId = `cat-detail-${idx}`;
 // Build expandable detail HTML
 const details = [];
 if (m.description) details.push(['Description', escapeHtml(m.description)]);
 if (m.owned_by) details.push(['Owned by', escapeHtml(m.owned_by)]);
 if (m.canonical_slug) details.push(['Canonical slug', '<code>' + escapeHtml(m.canonical_slug) + '</code>']);
 if (m.hugging_face_id) details.push(['HuggingFace ID', '<code>' + escapeHtml(m.hugging_face_id) + '</code>']);
 if (m.modality) details.push(['Modality', escapeHtml(m.modality)]);
 if (m.input_modalities?.length) details.push(['Input modalities', m.input_modalities.map(escapeHtml).join(', ')]);
 if (m.tokenizer) details.push(['Tokenizer', escapeHtml(m.tokenizer)]);
 if (m.instruct_type) details.push(['Instruct type', escapeHtml(m.instruct_type)]);
 if (m.supported_parameters?.length) {
   details.push(['Supported params', m.supported_parameters.map(p => '<span class="param-pill">' + escapeHtml(p) + '</span>').join(' ')]);
 }
 if (m.pricing_raw) {
   const pricingRows = Object.entries(m.pricing_raw)
     .map(([k, v]) => `<tr><td class="muted">${escapeHtml(k)}</td><td><code>${escapeHtml(String(v))}</code></td></tr>`)
     .join('');
   details.push(['Full pricing', `<table class="detail-mini-table">${pricingRows}</table>`]);
 }
 if (m.per_request_limits) {
   details.push(['Per-request limits', '<code>' + escapeHtml(JSON.stringify(m.per_request_limits)) + '</code>']);
 }
 if (m.created) {
   const date = new Date(m.created * 1000).toLocaleDateString();
   details.push(['Created', date]);
 }
 if (m.deprecated) details.push(['Deprecated', '<span class="text-amber">⚠ yes</span>']);

 const detailHtml = details.map(([k, v]) =>
   `<div class="catalog-detail-row"><span class="catalog-detail-label">${k}</span><span class="catalog-detail-value">${v}</span></div>`
 ).join('');

 return `<tr class="cat-row" id="${rowId}" data-detail="${detailId}" onclick="toggleCatalogDetail('${detailId}')" style="cursor:pointer">
 <td class="text-cyan">${escapeHtml(m.id)} <span class="expand-icon">▸</span></td>
 <td>${escapeHtml(m.provider)}</td>
 <td>${fmt(m.max_context_length)}</td>
 <td>${fmt(m.max_output_tokens)}</td>
 <td>${fmtCurrency(m.input_price)}</td>
 <td>${fmtCurrency(m.output_price)}</td>
 <td>${m.in_tier ? '<span class="text-green">★ yes</span>' : '<span class="muted">—</span>'}</td>
 </tr>
 <tr class="cat-detail-row" id="${detailId}" style="display:none">
 <td colspan="7"><div class="catalog-detail-panel">${detailHtml || '<span class="muted">No additional metadata</span>'}</div></td>
 </tr>`;
 }).join('');
}

function toggleCatalogDetail(id) {
 const row = document.getElementById(id);
 if (!row) return;
 const isOpen = row.style.display !== 'none';
 row.style.display = isOpen ? 'none' : 'table-row';
 // Toggle expand icon
 const triggerRow = document.querySelector(`tr[data-detail="${id}"]`);
 if (triggerRow) {
   const icon = triggerRow.querySelector('.expand-icon');
   if (icon) icon.textContent = isOpen ? '▸' : '▾';
 }
}

function catalogPrevPage() { if (_catalogPage > 1) { _catalogPage--; renderCatalog(); } }
function catalogNextPage() { _catalogPage++; renderCatalog(); }
function changeCatalogPageSize() { _catalogPage = 1; renderCatalog(); }

function filterCatalog() { _catalogPage = 1; renderCatalog(); }

function loadCatalog() { return catalogData.length ? renderCatalog() : refreshCatalog(); }

async function refreshCatalog() {
 try {
 catalogData = await api('/api/catalog?refresh=true');
 renderCatalog();
 } catch (e) { console.error('Catalog refresh failed:', e); }
}


function escapeAttr(s) {
    return String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])).replace(/'/g, "\\'");
}

function escapeHtml(text) {
 return String(text ?? '').replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
}

function formatNumber(n) {
 if (n == null) return '—';
 const num = Number(n);
 if (!Number.isFinite(num)) return String(n);
 return num.toLocaleString();
}

async function loadConfig() {
    try {
        const cfg = await api('/api/config');
        document.getElementById('configEditor').value = JSON.stringify(cfg, null, 2);
        document.getElementById('configStatus').textContent = '$ config loaded (keys redacted)';
        document.getElementById('configStatus').className = 'muted';
    } catch (e) {
        document.getElementById('configStatus').textContent = `$ error: ${e.message}`;
        document.getElementById('configStatus').className = 'text-red';
    }
}

async function saveConfig() {
    try {
        await api('/api/config', { method: 'PUT', body: document.getElementById('configEditor').value });
        document.getElementById('configStatus').textContent = '$ config saved ✓';
        document.getElementById('configStatus').className = 'text-green';
    } catch (e) {
        document.getElementById('configStatus').textContent = `$ error: ${e.message}`;
        document.getElementById('configStatus').className = 'text-red';
    }
}

// ═══════════════════════════════════════════════════════════════
// API KEY MANAGEMENT
// ═══════════════════════════════════════════════════════════════

async function loadApiKeys() {
    try {
        const keys = await api('/api/keys');
        const tbody = document.getElementById('keysBody');
        if (!keys.length) {
            tbody.innerHTML = '<tr><td colspan="8" class="muted">No API keys. Click [+ GENERATE KEY] to create one.</td></tr>';
            return;
        }
        tbody.innerHTML = keys.map(k => {
            const masked = k.key_prefix + '••••••••';
            const expiry = k.expires_at ? new Date(k.expires_at).toLocaleDateString('en-US') : 'Never';
            const isExpiringSoon = k.expires_at && new Date(k.expires_at).getTime() - Date.now() < 7 * 86400000;
            const quota = k.quota_requests ? `${k.quota_requests} / ${k.quota_period || '—'}` : 'Unlimited';
            const usage = k.usage_count || 0;
            const scopes = (k.allowed_tiers && k.allowed_tiers.length)
                ? k.allowed_tiers.map(t => `<span class="param-pill">${escapeHtml(t)}</span>`).join(' ')
                : '<span class="muted">all</span>';
            const statusBadge = !k.is_active
                ? '<span class="badge badge-offline">REVOKED</span>'
                : isExpiringSoon
                ? '<span class="badge badge-degraded">EXPIRING</span>'
                : '<span class="badge badge-online">ACTIVE</span>';
            return `<tr>
                <td>${escapeHtml(k.name)}</td>
                <td class="muted" style="font-size:11px">${masked}</td>
                <td>${scopes}</td>
                <td class="${isExpiringSoon ? 'text-amber' : ''}">${expiry}</td>
                <td>${quota}</td>
                <td>${usage}</td>
                <td>${statusBadge}</td>
                <td>
                    <button class="term-btn-sm" onclick="renewKey(${k.id})">[RENEW]</button>
                    <button class="term-btn-sm btn-danger" onclick="revokeKey(${k.id})">[REVOKE]</button>
                </td>
            </tr>`;
        }).join('');
    } catch (e) { console.error('Keys load failed:', e); }
}

async function showGenerateKeyModal() {
    document.getElementById('newKeyName').value = '';
    document.getElementById('newKeyExpiry').value = '';
    document.getElementById('newKeyQuota').value = '';
    document.getElementById('newKeyPeriod').value = '';
    // Populate tier checkboxes
    const wrap = document.getElementById('newKeyTiersWrap');
    if (wrap) {
        wrap.innerHTML = '<span class="muted">Loading...</span>';
        try {
            const tiers = await api('/api/tiers');
            const names = Object.keys(tiers);
            wrap.innerHTML = names.length
                ? names.map(n => `<label class="tier-check"><input type="checkbox" value="${escapeHtml(n)}" /> ${escapeHtml(n)}</label>`).join('')
                : '<span class="muted">No tiers defined</span>';
        } catch (_) {
            wrap.innerHTML = '<span class="text-red">Failed to load tiers</span>';
        }
    }
    document.getElementById('generateKeyModal').style.display = 'flex';
}

async function generateKey() {
    const name = document.getElementById('newKeyName').value.trim();
    if (!name) return alert('Name is required');
    const body = { name };
    const expiry = document.getElementById('newKeyExpiry').value;
    if (expiry) body.expires_at = new Date(expiry).toISOString();
    const quota = parseInt(document.getElementById('newKeyQuota').value);
    if (quota) body.quota_requests = quota;
    const period = document.getElementById('newKeyPeriod').value;
    if (period) body.quota_period = period;
    // Collect checked tier scopes
    const checks = document.querySelectorAll('#newKeyTiersWrap input[type="checkbox"]:checked');
    const tiers = Array.from(checks).map(c => c.value);
    if (tiers.length) body.allowed_tiers = tiers;

    try {
        const result = await api('/api/keys', { method: 'POST', body: JSON.stringify(body) });
        closeModal('generateKeyModal');
        document.getElementById('revealedKey').value = result.key;
        document.getElementById('copyStatus').textContent = '';
        document.getElementById('revealKeyModal').style.display = 'flex';
        loadApiKeys();
    } catch (e) { alert(`Failed: ${e.message}`); }
}

async function copyRevealedKey() {
    const input = document.getElementById('revealedKey');
    const status = document.getElementById('copyStatus');
    let success = false;
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(input.value);
            success = true;
        } else {
            input.select();
            success = document.execCommand('copy');
        }
    } catch (_) {
        try { input.select(); success = document.execCommand('copy'); } catch (_) {}
    }
    if (status) {
        status.innerHTML = success
            ? '<span class="text-green">> copied to clipboard ✓</span>'
            : '<span class="text-red">> copy failed — select and copy manually</span>';
        setTimeout(() => { if (status) status.textContent = ''; }, 3000);
    }
    input.blur();
}

async function revokeKey(id) {
    if (!confirm('Revoke this API key? This cannot be undone.')) return;
    try {
        await api(`/api/keys/${id}`, { method: 'DELETE' });
        loadApiKeys();
    } catch (e) { alert(`Failed: ${e.message}`); }
}

async function renewKey(id) {
    if (!confirm('Rotate this key? The old key will stop working immediately.')) return;
    try {
        const result = await api(`/api/keys/${id}/renew`, { method: 'POST' });
        document.getElementById('revealedKey').value = result.key;
        document.getElementById('revealKeyModal').style.display = 'flex';
        loadApiKeys();
    } catch (e) { alert(`Failed: ${e.message}`); }
}

// ── Circuit Breaker ──
async function loadCircuitBreaker() {
    try {
        const data = await api('/api/settings/circuit-breaker');
        document.getElementById('cbEnabled').checked = data.enabled;
        document.getElementById('cbThreshold').value = data.failure_threshold;
        document.getElementById('cbRecovery').value = data.recovery_timeout;
    } catch (e) { console.error('cb load', e); }
}

async function saveCircuitBreaker() {
    const payload = {
        enabled: document.getElementById('cbEnabled').checked,
        failure_threshold: parseInt(document.getElementById('cbThreshold').value) || 3,
        recovery_timeout: parseInt(document.getElementById('cbRecovery').value) || 60,
    };
    try {
        await api('/api/settings/circuit-breaker', { method: 'PUT', body: JSON.stringify(payload) });
        document.getElementById('cbStatus').innerHTML = '<span class="badge-online">✓ Saved</span>';
        setTimeout(() => { const el = document.getElementById('cbStatus'); if (el) el.innerHTML = ''; }, 3000);
    } catch (e) {
        document.getElementById('cbStatus').innerHTML = '<span class="badge-offline">✗ ' + escapeHtml(e.message) + '</span>';
    }
}
