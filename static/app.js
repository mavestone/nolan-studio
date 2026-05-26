// ══ State ══
let currentProjectId   = null;
let currentProjectName = '';
let activeFileId       = null;
let allFiles           = [];
let allJobs            = {};
let pollTimer          = null;
let searchTimer        = null;
let wasProcessing      = false;
let currentWorkspace   = 'clips';
let chatHistory        = [];
let chatBusy           = false;
let scenesCache        = {};   // fileId → [{scene_num, start_time, thumbnail_path}]
let activeFilter       = 'all';
let offlineMode        = false;
let claudeAvailable    = true;
let viewMode           = 'list';    // 'list' or 'gallery'
let sortMode           = 'status';  // status, name, duration-desc, duration-asc, shot_type, location
let activeHighlightQuery = null;    // search phrase to highlight inside the transcript

try {
  const persisted = JSON.parse(localStorage.getItem('nolanUiPrefs') || '{}');
  if (persisted.viewMode) viewMode = persisted.viewMode;
  if (persisted.sortMode) sortMode = persisted.sortMode;
} catch (_) {}

function persistUiPrefs() {
  try {
    localStorage.setItem('nolanUiPrefs', JSON.stringify({ viewMode, sortMode }));
  } catch (_) {}
}

// ══ Boot ══
window.addEventListener('DOMContentLoaded', async () => {
  showHome();
  initKeyboardShortcuts();
  applyUiPrefsToControls();
  await loadAppSettings();
  // Show onboarding wizard on first visit (no projects + no API keys)
  setTimeout(maybeShowOnboarding, 500);
});

// ════════════════════ SETTINGS / OFFLINE MODE ════════════════════

async function loadAppSettings() {
  try {
    const s = await apiFetch('/api/settings');
    offlineMode     = s.offline_mode;
    claudeAvailable = s.claude_available;
    applyOfflineMode();

    // Telegram bot quick-launch — only show if bot is configured
    const tgBtn = document.getElementById('toolbar-telegram-btn');
    const tgLbl = document.getElementById('toolbar-telegram-label');
    if (tgBtn && s.telegram_url && s.telegram_bot_username) {
      tgBtn.href = s.telegram_url;
      tgBtn.style.display = 'inline-flex';
      if (tgLbl) tgLbl.textContent = `@${s.telegram_bot_username}`;
      tgBtn.title = `Open ${s.telegram_bot_username} in Telegram`;
    } else if (tgBtn) {
      tgBtn.style.display = 'none';
    }
  } catch (_) {}
}

function applyOfflineMode() {
  const btn   = document.getElementById('offline-toggle');
  const dot   = document.getElementById('offline-dot');
  const label = document.getElementById('offline-label');
  if (!btn) return;

  if (offlineMode) {
    btn.classList.add('is-offline');
    label.textContent = 'OFFLINE';
  } else {
    btn.classList.remove('is-offline');
    label.textContent = claudeAvailable ? 'ONLINE' : 'NO AI';
  }
}

// ════════════════════ ONBOARDING WIZARD ════════════════════

let onboardStep = 1;

function showOnboard() {
  onboardStep = 1;
  document.getElementById('onboard-modal').style.display = 'flex';
  _renderOnboardStep();
}

function closeOnboard(skip = false) {
  document.getElementById('onboard-modal').style.display = 'none';
  // Mark complete so we don't show again
  try { localStorage.setItem('nolanOnboarded', '1'); } catch (_) {}
  if (!skip) showToast('Setup complete — happy editing.', 4000);
}

function _renderOnboardStep() {
  document.querySelectorAll('.onboard-step').forEach(el => {
    el.style.display = (parseInt(el.dataset.step, 10) === onboardStep) ? 'block' : 'none';
  });
  document.querySelectorAll('.onboard-step-dot').forEach(el => {
    el.classList.toggle('active',  parseInt(el.dataset.step, 10) === onboardStep);
    el.classList.toggle('done',    parseInt(el.dataset.step, 10) < onboardStep);
  });
}

function onboardNext() { onboardStep++; _renderOnboardStep(); }
function onboardBack() { if (onboardStep > 1) { onboardStep--; _renderOnboardStep(); } }

async function onboardSaveKeysAndNext() {
  const anth = document.getElementById('onboard-anthropic').value.trim();
  const groq = document.getElementById('onboard-groq').value.trim();
  const payload = {};
  if (anth) payload.anthropic = anth;
  if (groq) payload.groq = groq;
  if (Object.keys(payload).length) {
    try {
      await apiFetch('/api/settings/api-keys', 'PATCH', payload);
    } catch (e) {
      showToast('Could not save keys: ' + e.message, 3000);
    }
  }
  onboardNext();
}

async function onboardSaveTelegramAndNext() {
  const token = document.getElementById('onboard-tg-token').value.trim();
  if (token) {
    try {
      await apiFetch('/api/settings', 'PATCH', { telegram_token: token });
    } catch (e) {
      showToast('Could not save token: ' + e.message, 3000);
    }
  }
  onboardNext();
}

async function onboardImportFile(e) {
  const f = e.target.files?.[0];
  if (!f) return;
  showToast('Importing… this may take a minute for large projects.', 4000);
  try {
    const fd = new FormData();
    fd.append('file', f);
    const res = await fetch('/api/projects/import', { method: 'POST', body: fd });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const data = await res.json();
    closeOnboard();
    showToast(`✓ Imported "${data.project_name}" · ${data.file_count} clips (${data.relinked} relinked)`, 6000);
    await loadProjectGrid();
  } catch (err) {
    showToast('Import failed: ' + err.message, 5000);
  }
}

async function onboardFinish() {
  const name = document.getElementById('onboard-project-name').value.trim();
  if (name) {
    try {
      const p = await apiFetch('/api/projects', 'POST', { name });
      closeOnboard();
      showProject(p.id, p.name);
      return;
    } catch (e) {
      showToast(e.message, 4000);
      return;
    }
  }
  // No project name provided — just close
  closeOnboard();
}

// Auto-trigger on first visit (no settings file means fresh install)
async function maybeShowOnboarding() {
  try {
    const seen = localStorage.getItem('nolanOnboarded') === '1';
    if (seen) return;
    const settings = await apiFetch('/api/settings').catch(() => ({}));
    const projects = await apiFetch('/api/projects').catch(() => []);
    // Show onboarding only if (a) no projects yet AND (b) no API keys configured
    const hasKey = settings.api_keys && Object.values(settings.api_keys).some(v => v?.set);
    if (projects.length === 0 && !hasKey) {
      showOnboard();
    } else {
      try { localStorage.setItem('nolanOnboarded', '1'); } catch (_) {}
    }
  } catch (_) {}
}

// ════════════════════ EXPORT / IMPORT ════════════════════

async function exportProject(e, projectId) {
  e.stopPropagation();
  showToast('Building project bundle…', 3000);
  try {
    const res = await fetch(`/api/projects/${projectId}/export`);
    if (!res.ok) throw new Error('Export failed');
    const blob = await res.blob();
    // Use the Content-Disposition filename if provided
    let filename = `project_${projectId}.nolanproj`;
    const disp = res.headers.get('Content-Disposition');
    const m = disp && disp.match(/filename="?([^"]+)"?/);
    if (m) filename = m[1];
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    showToast('✓ Project exported · share this file with collaborators', 4000);
  } catch (err) {
    showToast('Export failed: ' + err.message, 4000);
  }
}

async function importProjectFile(e) {
  const f = e.target.files?.[0];
  if (!f) return;
  e.target.value = '';   // allow re-import of same file
  showToast(`Importing ${f.name}…`, 4000);

  // Ask for footage_root so file paths get relinked on this machine
  const root = prompt(
    "Where are the source MP4s on YOUR Mac?\n\n" +
    "Pick the top-level folder (Nolan will walk it and match filenames).\n" +
    "Leave blank if the paths in the bundle are already valid here.",
    "/Volumes/"
  );

  try {
    const fd = new FormData();
    fd.append('file', f);
    if (root && root.trim()) fd.append('footage_root', root.trim());
    const res = await fetch('/api/projects/import', { method: 'POST', body: fd });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const data = await res.json();
    showToast(`✓ "${data.project_name}" · ${data.file_count} clips · ${data.relinked} relinked, ${data.missing} missing`, 6000);
    await loadProjectGrid();
  } catch (err) {
    showToast('Import failed: ' + err.message, 5000);
  }
}

// ════════════════════ SETTINGS MODAL ════════════════════

async function openSettings() {
  const modal = document.getElementById('settings-modal');
  modal.style.display = 'flex';
  document.getElementById('settings-status').textContent = '';
  loadVersionInfo();

  // Clear inputs (we use placeholders to show current state)
  ['key-anthropic','key-groq','key-groq_2','key-gemini','tg-token','tg-chat-ids'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });

  try {
    const s = await apiFetch('/api/settings');

    // API keys — show masked current value as the "current" hint
    const setKeyHint = (id, info) => {
      const el = document.getElementById(`cur-${id}`);
      if (!el) return;
      if (info?.set) {
        el.textContent = info.masked || '••• set';
        el.className = 'settings-current is-set';
      } else {
        el.textContent = 'not set';
        el.className = 'settings-current is-missing';
      }
    };
    setKeyHint('anthropic', s.api_keys?.anthropic);
    setKeyHint('groq',      s.api_keys?.groq);
    setKeyHint('groq_2',    s.api_keys?.groq_2);
    setKeyHint('gemini',    s.api_keys?.gemini);

    // Telegram
    const tgCur = document.getElementById('cur-telegram');
    if (tgCur) {
      tgCur.textContent = s.telegram_token_masked || 'not set';
      tgCur.className = 'settings-current ' + (s.telegram_token_set ? 'is-set' : 'is-missing');
    }
    document.getElementById('tg-chat-ids').placeholder = (s.telegram_chat_ids || []).join(', ') || '123456, 987654';
    document.getElementById('tg-model').value = s.telegram_model || 'haiku';

    // Project dropdown
    const projSel = document.getElementById('tg-default-project');
    projSel.innerHTML = '<option value="">— none —</option>';
    const projects = await apiFetch('/api/projects');
    for (const p of projects) {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = `${p.name} (${p.file_count} clips)`;
      if (p.id === s.telegram_default_project_id) opt.selected = true;
      projSel.appendChild(opt);
    }

    // Open-bot link
    const linkEl = document.getElementById('settings-tg-link');
    if (s.telegram_url) {
      linkEl.href = s.telegram_url;
      linkEl.textContent = `Open @${s.telegram_bot_username} →`;
      linkEl.style.display = 'inline-flex';
    } else {
      linkEl.style.display = 'none';
    }
  } catch (e) {
    document.getElementById('settings-status').textContent = 'Failed to load: ' + e.message;
  }
}

function closeSettings() {
  document.getElementById('settings-modal').style.display = 'none';
}

async function saveApiKeys() {
  const payload = {};
  const fields = [
    ['key-anthropic', 'anthropic'],
    ['key-groq', 'groq'],
    ['key-groq_2', 'groq_2'],
    ['key-gemini', 'gemini'],
  ];
  for (const [id, name] of fields) {
    const val = document.getElementById(id).value.trim();
    if (val) payload[name] = val;  // only send non-empty values
  }
  if (Object.keys(payload).length === 0) {
    flashSettingsStatus('Nothing to save — fields are blank.', 'err');
    return;
  }
  try {
    await apiFetch('/api/settings/api-keys', 'PATCH', payload);
    flashSettingsStatus('✓ API keys saved', 'ok');
    // Refresh the masked hints
    setTimeout(() => openSettings(), 400);
  } catch (e) {
    flashSettingsStatus('Failed: ' + e.message, 'err');
  }
}

async function saveTelegramSettings() {
  const payload = {};
  const token = document.getElementById('tg-token').value.trim();
  if (token) payload.telegram_token = token;
  const idsRaw = document.getElementById('tg-chat-ids').value.trim();
  if (idsRaw) {
    payload.telegram_chat_ids = idsRaw.split(/[,\s]+/).map(s => parseInt(s, 10)).filter(n => !isNaN(n));
  }
  const projVal = document.getElementById('tg-default-project').value;
  if (projVal) payload.telegram_default_project_id = parseInt(projVal, 10);
  const model = document.getElementById('tg-model').value;
  if (model) payload.telegram_model = model;

  if (Object.keys(payload).length === 0) {
    flashSettingsStatus('No changes to save.', 'err');
    return;
  }
  try {
    await apiFetch('/api/settings', 'PATCH', payload);
    flashSettingsStatus('✓ Telegram settings saved — restart for token changes to take effect.', 'ok');
    setTimeout(() => openSettings(), 400);
  } catch (e) {
    flashSettingsStatus('Failed: ' + e.message, 'err');
  }
}

async function updateFromGithub() {
  if (!confirm('Pull the latest version from GitHub?\n\nNolan will restart automatically — this page will reload when it\'s back.')) return;
  const btn = document.getElementById('settings-update-btn');
  const spinner = '<svg width="11" height="11" viewBox="0 0 11 11" fill="none" style="margin-right:5px;animation:spin 1s linear infinite"><circle cx="5.5" cy="5.5" r="4" stroke="currentColor" stroke-width="1.4" stroke-dasharray="18" stroke-dashoffset="6"/></svg>';
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = spinner + 'Updating…';
  try {
    const res = await apiFetch('/api/admin/update', 'POST', {});

    if (res.old_commit === res.new_commit) {
      btn.innerHTML = original;
      btn.disabled = false;
      flashSettingsStatus(`✓ Already up to date · ${res.new_commit}`, 'ok');
      return;
    }

    // Server is restarting — poll until it comes back, then reload
    btn.innerHTML = spinner + 'Restarting…';
    flashSettingsStatus(`✓ Updated ${res.old_commit} → ${res.new_commit} · "${res.new_message}" — restarting…`, 'ok');

    // Wait for server to go down (give SIGTERM time to propagate)
    await new Promise(r => setTimeout(r, 4000));

    // Poll until the server responds again (up to ~60 s)
    let came_back = false;
    for (let i = 0; i < 30; i++) {
      try {
        await fetch('/api/admin/version', { signal: AbortSignal.timeout(2000) });
        came_back = true;
        break;
      } catch (_) {
        await new Promise(r => setTimeout(r, 2000));
      }
    }

    if (came_back) {
      flashSettingsStatus('✓ Nolan restarted — reloading…', 'ok');
      await new Promise(r => setTimeout(r, 600));
      location.reload();
    } else {
      btn.innerHTML = original;
      btn.disabled = false;
      flashSettingsStatus('Updated but restart timed out — please quit and reopen Nolan manually', 'err');
    }
  } catch (e) {
    flashSettingsStatus('Update failed: ' + e.message, 'err');
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

async function loadVersionInfo() {
  const el = document.getElementById('settings-version');
  if (!el) return;
  try {
    const v = await apiFetch('/api/admin/version');
    if (v.is_git_repo) {
      el.innerHTML = `<code>${escHtml(v.current)}</code> · ${escHtml(v.current_msg || '')}`;
    } else {
      el.textContent = 'Not a git checkout — manual update needed';
    }
  } catch (_) {
    el.textContent = 'Version unknown';
  }
}

async function reclassifyScenes() {
  if (!confirm('Re-run scene classifier on ALL existing scenes? This will rewrite indoor/outdoor + tag data for non-AI-classified scenes.')) return;
  try {
    await apiFetch('/api/admin/reclassify-scenes', 'POST', {});
    flashSettingsStatus('✓ Re-classification started — check terminal for progress', 'ok');
  } catch (e) {
    flashSettingsStatus('Failed: ' + e.message, 'err');
  }
}

function flashSettingsStatus(msg, cls) {
  const el = document.getElementById('settings-status');
  el.textContent = msg;
  el.className = 'settings-status ' + (cls || '');
}

async function toggleOfflineMode() {
  offlineMode = !offlineMode;
  applyOfflineMode();
  try {
    await apiFetch('/api/settings', 'PATCH', { offline_mode: offlineMode });
    if (offlineMode) {
      showToast('Offline mode — transcription & scenes only. AI features disabled.', 5000);
      // If story workspace is open, switch to clips
      if (currentWorkspace === 'story') setWorkspace('clips');
    } else {
      showToast('Online mode — AI features enabled.', 3000);
    }
  } catch (e) {
    showToast('Could not save setting: ' + e.message, 3000);
  }
}

// ════════════════════ KEYBOARD SHORTCUTS ════════════════════

function initKeyboardShortcuts() {
  document.addEventListener('keydown', e => {
    // Escape: close forms / clear search
    if (e.key === 'Escape') {
      hideNewProjectForm();
      if (document.getElementById('search-input') === document.activeElement) {
        document.getElementById('search-input').blur();
        document.getElementById('search-input').value = '';
        renderClipGrid();
      }
    }

    // / or Cmd+F: focus search (only in project view)
    if ((e.key === '/' || (e.metaKey && e.key === 'f')) && currentProjectId) {
      if (document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
        e.preventDefault();
        document.getElementById('search-input')?.focus();
      }
    }

    // Enter: create project
    if (e.key === 'Enter' && document.activeElement?.id === 'new-project-name') {
      createProject();
    }

    // J / ArrowDown: next clip
    if ((e.key === 'j' || e.key === 'ArrowDown') && currentProjectId && !isInputFocused()) {
      e.preventDefault();
      navigateClip(1);
    }
    // K / ArrowUp: prev clip
    if ((e.key === 'k' || e.key === 'ArrowUp') && currentProjectId && !isInputFocused()) {
      e.preventDefault();
      navigateClip(-1);
    }
  });
}

function isInputFocused() {
  const t = document.activeElement?.tagName;
  return t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT';
}

function navigateClip(dir) {
  const rows = [
    ...document.querySelectorAll('.clip-row[data-id]'),
    ...document.querySelectorAll('.clip-card[data-id]'),
  ];
  if (!rows.length) return;
  const curIdx = rows.findIndex(r => Number(r.dataset.id) === activeFileId);
  const newIdx = Math.max(0, Math.min(rows.length - 1, curIdx + dir));
  openClip(Number(rows[newIdx].dataset.id));   // no highlight query
}

// ════════════════════ HOME VIEW ════════════════════

function showHome() {
  document.getElementById('view-home').style.display = 'flex';
  document.getElementById('view-project').style.display = 'none';
  stopPolling();
  currentProjectId = null;
  activeFileId = null;
  loadProjectGrid();
}

async function loadProjectGrid() {
  const grid = document.getElementById('project-grid');
  grid.innerHTML = '<div class="loading-state">Loading…</div>';
  try {
    const projects = await apiFetch('/api/projects');
    if (!projects.length) {
      grid.innerHTML = `<div class="empty-state">No projects yet.<br>Click "+ New Project" to get started.</div>`;
      return;
    }

    // Load posters for each project
    const posterMap = {};
    await Promise.allSettled(projects.map(async p => {
      try {
        const r = await apiFetch(`/api/projects/${p.id}/posters`);
        posterMap[p.id] = r.posters || [];
      } catch (_) {}
    }));

    grid.innerHTML = projects.map(p => {
      const meta = p.file_count
        ? `${p.file_count} clip${p.file_count !== 1 ? 's' : ''}${p.done_count ? ` · ${p.done_count} processed` : ''}`
        : 'No clips yet';

      const posters = posterMap[p.id] || [];
      let thumbHtml = '';
      if (posters.length === 0) {
        thumbHtml = `<div class="project-card-thumb">
          <div class="project-card-thumb-placeholder">
            <svg width="28" height="28" viewBox="0 0 28 28" fill="none" opacity="0.15">
              <rect x="2" y="6" width="18" height="16" rx="2.5" fill="white"/>
              <path d="M20 11l6-4v14l-6-4V11z" fill="white"/>
            </svg>
          </div>
        </div>`;
      } else if (posters.length === 1) {
        thumbHtml = `<div class="project-card-thumb" style="display:block">
          <img class="project-card-thumb-img" src="/static/${escHtml(posters[0])}" alt="" loading="lazy" style="height:100%">
        </div>`;
      } else {
        const imgs = posters.slice(0, 4).map(p =>
          `<img class="project-card-thumb-img" src="/static/${escHtml(p)}" alt="" loading="lazy">`
        ).join('');
        thumbHtml = `<div class="project-card-thumb">${imgs}</div>`;
      }

      return `
        <div class="project-card" data-id="${p.id}" data-name="${escHtml(p.name)}" onclick="openProjectCard(this)">
          ${thumbHtml}
          <div class="project-card-toolbar">
            <button class="project-card-iconbtn" onclick="exportProject(event,${p.id})" title="Export as .nolanproj">
              <svg width="9" height="9" viewBox="0 0 10 10" fill="none"><path d="M5 1v6M2 4l3-3 3 3M1 8.5h8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </button>
            <button class="project-card-iconbtn project-card-delete" onclick="deleteProject(event,${p.id})" title="Delete project">
              <svg width="9" height="9" viewBox="0 0 9 9" fill="none"><path d="M1 1l7 7M8 1L1 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
            </button>
          </div>
          <div class="project-card-info">
            <div class="project-card-name" title="${escHtml(p.name)}">${escHtml(p.name)}</div>
            <div class="project-card-meta">${meta}</div>
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    grid.innerHTML = `<div class="empty-state">Failed to load: ${escHtml(e.message)}</div>`;
  }
}

function openProjectCard(el) {
  showProject(Number(el.dataset.id), el.dataset.name);
}

function showNewProjectForm() {
  document.getElementById('new-project-form').style.display = 'flex';
  setTimeout(() => document.getElementById('new-project-name').focus(), 40);
}

function hideNewProjectForm() {
  document.getElementById('new-project-form').style.display = 'none';
  document.getElementById('new-project-name').value = '';
}

async function createProject() {
  const name = document.getElementById('new-project-name').value.trim();
  if (!name) return;
  try {
    const p = await apiFetch('/api/projects', 'POST', { name });
    hideNewProjectForm();
    showProject(p.id, p.name);
  } catch (e) {
    alert(e.message);
  }
}

async function deleteProject(e, id) {
  e.stopPropagation();
  if (!confirm('Delete this project? (Source files are untouched.)')) return;
  await apiFetch(`/api/projects/${id}`, 'DELETE');
  loadProjectGrid();
}

// ════════════════════ PROJECT VIEW ════════════════════

async function showProject(id, name) {
  currentProjectId   = id;
  currentProjectName = name;
  activeFileId       = null;
  allFiles           = [];
  allJobs            = {};
  wasProcessing      = false;
  chatHistory        = [];
  scenesCache        = {};

  document.getElementById('view-home').style.display = 'none';
  document.getElementById('view-project').style.display = 'flex';
  document.getElementById('toolbar-project-name').textContent = name;
  document.getElementById('detail-panel').style.display = 'none';
  document.getElementById('search-input').value = '';
  activeFilter = 'all';
  document.querySelectorAll('.pool-tab').forEach(t => t.classList.toggle('active', t.dataset.filter === 'all'));

  setWorkspace('clips', false);
  await Promise.all([loadBins(), loadClips()]);
  startPolling();
}

function goHome() { showHome(); }

// ── Bins ──

async function loadBins() {
  if (!currentProjectId) return;
  const folders = await apiFetch(`/api/projects/${currentProjectId}/folders`).catch(() => []);
  const el = document.getElementById('bins-list');
  if (!folders.length) {
    el.innerHTML = '<div style="font-size:11px;color:var(--text3);padding:8px 4px">No folders — add one above</div>';
    return;
  }
  el.innerHTML = folders.map(f => {
    const name = f.path.split('/').pop() || f.path;
    const meta = f.last_scanned
      ? `${f.files_found} clips · ${timeAgo(f.last_scanned)}`
      : 'Never scanned';
    return `
      <div class="bin-entry">
        <div class="bin-entry-name" title="${escHtml(f.path)}">
          <svg width="10" height="10" viewBox="0 0 10 10" fill="none" style="vertical-align:-1px;margin-right:4px;opacity:0.5"><rect x="0.5" y="2.5" width="9" height="7" rx="1" stroke="currentColor" stroke-width="1"/><path d="M0.5 4.5h9" stroke="currentColor" stroke-width="0.8"/><path d="M1 2.5l1-2h6l1 2" stroke="currentColor" stroke-width="0.8" fill="none"/></svg>
          ${escHtml(name)}
        </div>
        <div class="bin-entry-meta">${meta}</div>
        <div class="bin-actions">
          <button class="bin-scan-btn" onclick="scanFolder(${f.id})">SCAN</button>
          <button class="bin-remove-btn" onclick="removeFolder(event,${f.id})" title="Remove">
            <svg width="8" height="8" viewBox="0 0 8 8" fill="none"><path d="M1 1l6 6M7 1L1 7" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
          </button>
        </div>
      </div>`;
  }).join('');
}

async function addFolder() {
  if (!currentProjectId) return;
  try {
    const res = await apiFetch('/api/pick-folder');
    await apiFetch(`/api/projects/${currentProjectId}/folders`, 'POST', { path: res.path });
    await loadBins();
  } catch (e) {
    // -128 / "No folder selected" = user cancelled the picker — stay silent
    const msg = e.message || '';
    if (!msg.includes('No folder selected') && !msg.includes('-128')) {
      alert('Could not open folder picker:\n\n' + msg + '\n\nTry restarting Nolan.');
    }
  }
}

async function removeFolder(e, folderId) {
  e.stopPropagation();
  if (!confirm('Remove this folder? (Scanned clips stay in the project.)')) return;
  await apiFetch(`/api/projects/${currentProjectId}/folders/${folderId}`, 'DELETE');
  loadBins();
}

async function scanFolder(folderId) {
  if (!currentProjectId) return;
  showScanProgress('Scanning…', true);
  try {
    const res = await apiFetch(`/api/projects/${currentProjectId}/folders/${folderId}/scan`, 'POST', {});
    document.getElementById('scan-bar').classList.remove('indeterminate');
    document.getElementById('scan-bar').style.width = '100%';
    showScanProgress(res.found === 0 ? 'No video files found.' : `${res.found} clips found · ${res.new} new`, false);
    await Promise.all([loadBins(), loadClips()]);
    setTimeout(hideScanProgress, 3500);
  } catch (e) {
    showScanProgress('Scan failed: ' + e.message, false);
    setTimeout(hideScanProgress, 4000);
  }
}

function showScanProgress(label, indeterminate) {
  const wrap = document.getElementById('scan-progress-wrap');
  const bar  = document.getElementById('scan-bar');
  wrap.style.display = 'block';
  document.getElementById('scan-label').textContent = label;
  bar.classList.toggle('indeterminate', indeterminate);
  if (!indeterminate) bar.style.width = '60%';
}

function hideScanProgress() {
  document.getElementById('scan-progress-wrap').style.display = 'none';
  const bar = document.getElementById('scan-bar');
  bar.style.width = '0%';
  bar.classList.remove('indeterminate');
}

// ── Process ──

async function processAll() {
  if (!currentProjectId) return;
  const model  = document.getElementById('model-select').value;
  const btn    = document.getElementById('process-btn');
  const stopBtn = document.getElementById('stop-btn');
  btn.disabled  = true;
  btn.innerHTML = '<svg width="11" height="11" viewBox="0 0 11 11" fill="none" style="margin-right:4px"><rect x="1" y="1" width="9" height="9" rx="1.5" stroke="currentColor" stroke-width="1.4"/><path d="M3.5 3.5l4 4M7.5 3.5l-4 4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>Queuing…';
  try {
    const res = await apiFetch(`/api/projects/${currentProjectId}/process`, 'POST', { model_size: model });
    if (res.queued > 0) stopBtn.style.display = 'inline-flex';
    startPolling();
    setTimeout(() => {
      btn.disabled = false;
      btn.innerHTML = '<svg width="11" height="11" viewBox="0 0 11 11" fill="none" style="margin-right:4px"><polygon points="2,1 10,5.5 2,10" fill="currentColor"/></svg>Process';
    }, 2500);
  } catch (e) {
    alert('Failed: ' + e.message);
    btn.disabled = false;
    btn.innerHTML = '<svg width="11" height="11" viewBox="0 0 11 11" fill="none" style="margin-right:4px"><polygon points="2,1 10,5.5 2,10" fill="currentColor"/></svg>Process';
  }
}

async function stopProcessing() {
  const stopBtn = document.getElementById('stop-btn');
  stopBtn.disabled = true;
  stopBtn.innerHTML = '<svg width="10" height="10" viewBox="0 0 10 10" fill="none" style="margin-right:4px;animation:spin 0.8s linear infinite"><circle cx="5" cy="5" r="4" stroke="currentColor" stroke-width="1.4" stroke-dasharray="15" stroke-dashoffset="5"/></svg>Stopping…';

  try {
    const res = await apiFetch('/api/stop', 'POST', {});
    showToast(`Stopped — reverted ${res.reverted || 0} clips to pending`, 3000);
  } catch (e) {
    showToast('Stop failed: ' + e.message, 3000);
  }

  // Refresh immediately so the UI catches up
  await loadClips();

  setTimeout(() => {
    stopBtn.style.display = 'none';
    stopBtn.innerHTML = '<svg width="10" height="10" viewBox="0 0 10 10" fill="none" style="margin-right:4px"><rect x="1" y="1" width="8" height="8" rx="1" fill="currentColor"/></svg>Stop';
    stopBtn.disabled = false;
  }, 1500);
}

// ── Clip grid ──

async function loadClips() {
  if (!currentProjectId) return;
  const [files, jobs] = await Promise.all([
    apiFetch(`/api/projects/${currentProjectId}/files`).catch(() => []),
    apiFetch('/api/jobs').catch(() => ({})),
  ]);
  allFiles = files;
  allJobs  = jobs;
  renderClipGrid();
  updateNowProcessing();
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(async () => {
    if (!currentProjectId) return;

    const prevStatus = activeFileId
      ? (allJobs[activeFileId]?.status || allFiles.find(f => f.id === activeFileId)?.status)
      : null;

    const [files, jobs] = await Promise.all([
      apiFetch(`/api/projects/${currentProjectId}/files`).catch(() => []),
      apiFetch('/api/jobs').catch(() => ({})),
    ]);
    allFiles = files;
    allJobs  = jobs;

    if (!document.getElementById('search-input').value.trim()) renderClipGrid();
    updateNowProcessing();

    // If active clip changed status, reload its detail panel
    const newStatus = activeFileId
      ? (jobs[activeFileId]?.status || files.find(f => f.id === activeFileId)?.status)
      : null;
    if (activeFileId && prevStatus !== newStatus) openClip(activeFileId);

    const anyActive = files.some(f =>
      ['queued', 'transcribing', 'analyzing', 'transcribed'].includes(jobs[f.id]?.status || f.status)
    );

    if (anyActive) {
      wasProcessing = true;
    } else if (wasProcessing) {
      wasProcessing = false;
      stopPolling();
      hideNowProcessing();
      const stopBtn = document.getElementById('stop-btn');
      if (stopBtn) { stopBtn.style.display = 'none'; stopBtn.disabled = false; }
      showCompletionBanner();
    } else {
      stopPolling();
      hideNowProcessing();
    }
  }, 2200);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

const STAGE_LABEL = {
  pending:         'NO TRANSCRIPT',
  queued:          'QUEUED',
  transcribing:    'TRANSCRIBING',
  extracting_audio:'EXTRACTING',
  transcribed:     'TRANSCRIBED',
  analyzing:       'ANALYSING',
  done:            'DONE',
  silent:          'SILENT',
  error:           'ERROR',
};

const STATUS_ORDER = {
  done: 0, transcribed: 1, analyzing: 2,
  transcribing: 3, extracting_audio: 3, queued: 4,
  silent: 5, pending: 6, error: 7
};

// Shot type sort priority (most editorially useful first)
const SHOT_TYPE_ORDER = { closeup: 0, medium: 1, wide: 2, broll: 3, unknown: 4 };
const SHOT_SIZE_ORDER = {
  extreme_close_up: 0, close_up: 1, medium: 2, full: 3, wide: 4, extreme_wide: 5, aerial: 6,
};
const ROLL_ORDER    = { a_roll: 0, b_roll: 1 };
const SETTING_ORDER = { indoor: 0, outdoor: 1 };

function setFilter(filter) {
  activeFilter = filter;
  document.querySelectorAll('.pool-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.filter === filter);
  });
  renderClipGrid();
}

function setSortMode(mode) {
  sortMode = mode;
  persistUiPrefs();
  renderClipGrid();
}

function setViewMode(mode) {
  viewMode = mode;
  persistUiPrefs();
  document.querySelectorAll('.view-mode-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.view === mode);
  });
  renderClipGrid();
}

// Initialise sort dropdown and view toggle from persisted prefs
function applyUiPrefsToControls() {
  const sortEl = document.getElementById('sort-select');
  if (sortEl) sortEl.value = sortMode;
  document.querySelectorAll('.view-mode-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.view === viewMode);
  });
}

function compareFiles(a, b, mode, jobs) {
  switch (mode) {
    case 'name':
      return (a.filename || '').localeCompare(b.filename || '');
    case 'duration-desc':
      return (b.duration_seconds || 0) - (a.duration_seconds || 0);
    case 'duration-asc':
      return (a.duration_seconds || 0) - (b.duration_seconds || 0);
    case 'shot_type': {
      const aa = SHOT_TYPE_ORDER[a.primary_shot_type] ?? 99;
      const bb = SHOT_TYPE_ORDER[b.primary_shot_type] ?? 99;
      if (aa !== bb) return aa - bb;
      return (a.filename || '').localeCompare(b.filename || '');
    }
    case 'shot_size': {
      const aa = SHOT_SIZE_ORDER[a.primary_shot_size] ?? 99;
      const bb = SHOT_SIZE_ORDER[b.primary_shot_size] ?? 99;
      if (aa !== bb) return aa - bb;
      return (a.filename || '').localeCompare(b.filename || '');
    }
    case 'roll_type': {
      const aa = ROLL_ORDER[a.primary_roll_type] ?? 99;
      const bb = ROLL_ORDER[b.primary_roll_type] ?? 99;
      if (aa !== bb) return aa - bb;
      return (a.filename || '').localeCompare(b.filename || '');
    }
    case 'setting': {
      const aa = SETTING_ORDER[a.primary_setting] ?? 99;
      const bb = SETTING_ORDER[b.primary_setting] ?? 99;
      if (aa !== bb) return aa - bb;
      return (a.filename || '').localeCompare(b.filename || '');
    }
    case 'location': {
      const at = (a.primary_location || parseTags(a.scene_tags)[0] || 'zzz').toLowerCase();
      const bt = (b.primary_location || parseTags(b.scene_tags)[0] || 'zzz').toLowerCase();
      if (at !== bt) return at.localeCompare(bt);
      return (a.filename || '').localeCompare(b.filename || '');
    }
    case 'status':
    default: {
      const sa = jobs[a.id]?.status || a.status;
      const sb = jobs[b.id]?.status || b.status;
      const oa = STATUS_ORDER[sa] ?? 99;
      const ob = STATUS_ORDER[sb] ?? 99;
      if (oa !== ob) return oa - ob;
      return (a.filename || '').localeCompare(b.filename || '');
    }
  }
}

function parseTags(raw) {
  if (!raw) return [];
  if (Array.isArray(raw)) return raw;
  try { return JSON.parse(raw); } catch (_) { return []; }
}

function renderClipGrid(files = allFiles, jobs = allJobs) {
  const el = document.getElementById('clip-grid');

  // Apply view-mode class
  el.classList.toggle('view-gallery', viewMode === 'gallery');

  let filtered = files;
  if (activeFilter === 'done')    filtered = files.filter(f => ['done', 'transcribed'].includes(jobs[f.id]?.status || f.status));
  if (activeFilter === 'silent')  filtered = files.filter(f => (jobs[f.id]?.status || f.status) === 'silent');
  if (activeFilter === 'pending') filtered = files.filter(f => (jobs[f.id]?.status || f.status) === 'pending');
  if (activeFilter === 'error')   filtered = files.filter(f => (jobs[f.id]?.status || f.status) === 'error');

  const countEl = document.getElementById('pool-count');
  if (countEl) countEl.textContent = filtered.length ? `${filtered.length} clips` : '';

  if (!filtered.length) {
    el.innerHTML = files.length
      ? '<div class="empty-state">No clips match this filter.</div>'
      : '<div class="empty-state">Scan a folder to load clips.</div>';
    return;
  }

  const sorted = [...filtered].sort((a, b) => compareFiles(a, b, sortMode, jobs));

  // Only group by status when sorting by status — other sort modes show a flat list
  let html = '';
  if (sortMode === 'status') {
    const sections = [
      { key: 'done',        label: 'PROCESSED',         statuses: ['done', 'transcribed'] },
      { key: 'active',      label: 'IN PROGRESS',       statuses: ['queued', 'transcribing', 'extracting_audio', 'analyzing'] },
      { key: 'silent',      label: 'SILENT (NO SPEECH)', statuses: ['silent'] },
      { key: 'pending',     label: 'PENDING',           statuses: ['pending'] },
      { key: 'error',       label: 'ERRORS',            statuses: ['error'] },
    ];
    for (const section of sections) {
      const group = sorted.filter(f => section.statuses.includes(jobs[f.id]?.status || f.status));
      if (!group.length) continue;
      html += `<div class="clip-section-header">${section.label} · ${group.length}</div>`;
      html += group.map(f => clipRowHtml(f, jobs[f.id]?.status || f.status)).join('');
    }
  } else if (['shot_size', 'roll_type', 'setting', 'shot_type'].includes(sortMode)) {
    // Categorical sort — group by the value with section headers
    const KEY_MAP = {
      shot_size: 'primary_shot_size',
      roll_type: 'primary_roll_type',
      setting:   'primary_setting',
      shot_type: 'primary_shot_type',
    };
    const LABEL_MAP = {
      extreme_close_up: 'EXTREME CLOSE-UP',
      close_up:         'CLOSE-UP',
      medium:           'MEDIUM',
      full:             'FULL',
      wide:             'WIDE',
      extreme_wide:     'EXTREME WIDE',
      a_roll:           'A-ROLL · KEY DIALOGUE',
      b_roll:           'B-ROLL · NO DIALOGUE',
      indoor:           'INDOOR',
      outdoor:          'OUTDOOR',
      closeup:          'CLOSE-UP',
      broll:            'B-ROLL',
    };
    const key = KEY_MAP[sortMode];
    const groups = new Map();
    for (const f of sorted) {
      const v = f[key] || '— unclassified —';
      if (!groups.has(v)) groups.set(v, []);
      groups.get(v).push(f);
    }
    for (const [val, group] of groups) {
      const label = LABEL_MAP[val] || val.toUpperCase().replace(/_/g, ' ');
      html += `<div class="clip-section-header">${escHtml(label)} · ${group.length}</div>`;
      html += group.map(f => clipRowHtml(f, jobs[f.id]?.status || f.status)).join('');
    }
  } else {
    // Flat list — single header showing sort mode
    const SORT_LABELS = {
      name: 'BY NAME',
      'duration-desc': 'BY DURATION (LONGEST FIRST)',
      'duration-asc': 'BY DURATION (SHORTEST FIRST)',
      location: 'BY LOCATION',
    };
    html += `<div class="clip-section-header">${SORT_LABELS[sortMode] || ''} · ${sorted.length}</div>`;
    html += sorted.map(f => clipRowHtml(f, jobs[f.id]?.status || f.status)).join('');
  }
  el.innerHTML = html;
}

function clipRowHtml(f, liveStatus) {
  if (viewMode === 'gallery') return clipCardHtml(f, liveStatus);

  const isActive = ['transcribing', 'extracting_audio', 'queued'].includes(liveStatus);

  const badge = isActive
    ? `<span class="clip-row-badge status-${liveStatus}">${STAGE_LABEL[liveStatus] || liveStatus.toUpperCase()}</span>`
    : liveStatus === 'error'
      ? `<span class="clip-row-badge status-error">ERROR</span>`
      : liveStatus === 'silent'
        ? `<span class="clip-row-badge status-silent">SILENT</span>`
        : '';

  // Poster thumbnail
  let thumbHtml = '';
  if (f.poster_path) {
    thumbHtml = `<div class="clip-row-thumb"><img src="/static/${escHtml(f.poster_path)}" alt="" loading="lazy" decoding="async"></div>`;
  } else {
    thumbHtml = `<div class="clip-row-thumb">
      <svg class="clip-row-thumb-placeholder" width="20" height="14" viewBox="0 0 20 14" fill="none">
        <rect x="0.5" y="0.5" width="13" height="12" rx="1.5" fill="white" opacity="0.4"/>
        <path d="M14 4l5-3v10l-5-3V4z" fill="white" opacity="0.25"/>
      </svg>
    </div>`;
  }

  // Compact attribute chips (text-based, no icons)
  const rollChip = f.primary_roll_type
    ? `<span class="clip-row-attr roll-${escHtml(f.primary_roll_type)}">${f.primary_roll_type === 'a_roll' ? 'A-ROLL' : 'B-ROLL'}</span>`
    : '';
  const sizeChip = f.primary_shot_size
    ? `<span class="clip-row-attr size-${escHtml(f.primary_shot_size)}">${escHtml(SHOT_SIZE_LABEL[f.primary_shot_size] || f.primary_shot_size.toUpperCase())}</span>`
    : '';
  const settingChip = f.primary_setting
    ? `<span class="clip-row-attr setting-${escHtml(f.primary_setting)}">${f.primary_setting === 'indoor' ? 'INDOOR' : 'OUTDOOR'}</span>`
    : '';

  return `
    <div class="clip-row ${f.id === activeFileId ? 'active' : ''}" data-id="${f.id}" onclick="openClip(${f.id})" tabindex="0">
      ${thumbHtml}
      <span class="clip-row-dot dot-${liveStatus}"></span>
      <span class="clip-row-name" title="${escHtml(f.path)}">${escHtml(f.filename)}</span>
      <div class="clip-row-right">
        ${badge}
        ${rollChip}
        ${sizeChip}
        ${settingChip}
        ${f.duration_seconds ? `<span class="clip-row-duration">${formatDuration(f.duration_seconds)}</span>` : ''}
      </div>
    </div>`;
}

// ── Gallery card (Mac Finder icon view) ──────────────────────────────────
function clipCardHtml(f, liveStatus) {
  const thumb = f.poster_path
    ? `<img src="/static/${escHtml(f.poster_path)}" alt="" loading="lazy" decoding="async">`
    : `<div class="clip-card-noimg">
         <svg width="28" height="20" viewBox="0 0 28 20" fill="none"><rect x="1" y="2" width="18" height="16" rx="2" stroke="currentColor" stroke-width="1.2" opacity="0.5"/><path d="M20 6l7-4v16l-7-4V6z" stroke="currentColor" stroke-width="1.2" opacity="0.5" stroke-linejoin="round"/></svg>
       </div>`;

  // Show shot SIZE (more granular) if available, fall back to shot_type
  const sizeOrType = f.primary_shot_size || f.primary_shot_type;
  const shot = sizeOrType
    ? `<span class="clip-card-shot" data-shot="${escHtml(f.primary_shot_size || f.primary_shot_type)}">${escHtml(SHOT_SIZE_LABEL[sizeOrType] || sizeOrType.toUpperCase().replace('CLOSEUP','CLOSE-UP'))}</span>`
    : '';

  // Roll badge — top-left next to status dot, text-based
  const rollBadge = f.primary_roll_type
    ? `<span class="clip-card-rollbadge roll-${escHtml(f.primary_roll_type)}">${f.primary_roll_type === 'a_roll' ? 'A-ROLL' : 'B-ROLL'}</span>`
    : '';

  const dur = f.duration_seconds
    ? `<span class="clip-card-dur">${formatDuration(f.duration_seconds)}</span>`
    : '';

  const stateBadge = ['transcribing','extracting_audio','queued'].includes(liveStatus)
    ? `<span class="clip-card-state state-${liveStatus}">${STAGE_LABEL[liveStatus] || liveStatus.toUpperCase()}</span>`
    : liveStatus === 'error'
      ? `<span class="clip-card-state state-error">ERROR</span>`
      : liveStatus === 'silent'
        ? `<span class="clip-card-state state-silent">SILENT</span>`
        : '';

  // Setting indicator (top-right of thumb)
  const settingChip = f.primary_setting
    ? `<span class="clip-card-setting setting-${escHtml(f.primary_setting)}">${f.primary_setting === 'indoor' ? 'INDOOR' : 'OUTDOOR'}</span>`
    : '';

  return `
    <div class="clip-card ${f.id === activeFileId ? 'active' : ''}" data-id="${f.id}" onclick="openClip(${f.id})" tabindex="0" title="${escHtml(f.filename)}${f.primary_location ? ' · ' + f.primary_location : ''}">
      <div class="clip-card-thumb">
        ${thumb}
        <span class="clip-card-dot dot-${liveStatus}"></span>
        ${dur}
        ${shot}
        ${rollBadge}
        ${settingChip}
        ${stateBadge}
      </div>
      <div class="clip-card-label">${escHtml(f.filename)}</div>
    </div>`;
}

// ── Search ──

// Shot-type / scene-tag keywords that trigger scene search
const SCENE_KEYWORDS = new Set([
  'closeup', 'close-up', 'close up', 'medium', 'wide', 'broll', 'b-roll',
  'interview', 'person', 'group', 'two-shot', 'outdoor', 'indoor',
  'desert', 'night', 'day', 'face',
]);

function debounceSearch(val) {
  clearTimeout(searchTimer);
  if (!val.trim()) { renderClipGrid(); return; }
  searchTimer = setTimeout(() => doSearch(val), 340);
}

async function doSearch(q) {
  if (!currentProjectId) return;
  const qLower = q.trim().toLowerCase();
  try {
    // Run transcript search + scene search in parallel
    const [transcriptResults, sceneResults] = await Promise.all([
      apiFetch(`/api/search?q=${encodeURIComponent(q)}&project_id=${currentProjectId}`).catch(() => []),
      apiFetch(`/api/projects/${currentProjectId}/scenes/search?q=${encodeURIComponent(qLower)}`).catch(() => []),
    ]);
    renderSearchResults(transcriptResults, sceneResults, q);
  } catch (_) {}
}

function renderSearchResults(transcriptResults, sceneResults, q) {
  const el = document.getElementById('clip-grid');
  // Search results use the list-style layout regardless of view mode
  el.classList.remove('view-gallery');
  const regex = new RegExp(`(${escRegex(q)})`, 'gi');

  let html = '';

  // ── Scene results ──
  if (sceneResults.length) {
    html += `<div class="clip-section-header">SCENES · ${sceneResults.length}</div>`;
    html += `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:5px;padding:6px 10px">`;
    html += sceneResults.slice(0, 60).map(s => sceneCardHtml(s, s.filename)).join('');
    html += `</div>`;
  }

  // ── Transcript results ──
  if (transcriptResults.length) {
    html += `<div class="clip-section-header">TRANSCRIPTS · ${transcriptResults.length}</div>`;
    html += transcriptResults.map(r => {
      const highlighted = escHtml(r.text).replace(regex, '<mark class="hit">$1</mark>');
      const qEsc = escHtml(q);
      return `
        <div class="search-result-row" onclick="openClip(${r.file_id}, '${qEsc.replace(/'/g, "\\'")}', ${r.start_time})">
          <div class="search-result-filename">
            <span>${escHtml(r.filename)}</span>
            <span class="search-result-time">${formatTime(r.start_time)}</span>
          </div>
          <div class="search-result-text">${highlighted}</div>
        </div>`;
    }).join('');
  }

  if (!html) {
    el.innerHTML = `<div class="empty-state">No results for "${escHtml(q)}"</div>`;
    return;
  }
  el.innerHTML = html;
}

// ── Clip detail ──

async function openClip(fileId, highlightQuery = null, jumpToTime = null) {
  activeFileId = fileId;
  activeHighlightQuery = highlightQuery;

  document.querySelectorAll('.clip-row').forEach(el => {
    el.classList.toggle('active', Number(el.dataset.id) === fileId);
  });

  const panel = document.getElementById('detail-panel');
  panel.style.display = 'flex';

  // Reset to transcript tab
  showTab('transcript', false);

  let file;
  try {
    file = await apiFetch(`/api/files/${fileId}`);
  } catch (e) {
    document.getElementById('detail-header').innerHTML = '<div class="no-data">Could not load clip.</div>';
    return;
  }

  // Header buttons: scene detect + open in finder
  const canDetect = ['done', 'transcribed', 'silent'].includes(file.status);
  // Check if scenes already exist (for label)
  const hasScenes = (scenesCache[fileId] !== undefined)
    ? scenesCache[fileId].length > 0
    : false; // will be confirmed by prefetch
  const detectBtn = canDetect
    ? `<button class="btn btn-ghost" style="font-size:10px;padding:4px 9px;min-height:24px" id="detect-scenes-btn" onclick="triggerDetectScenes(${file.id})" title="Run or re-run scene detection">
         <svg width="10" height="10" viewBox="0 0 10 10" fill="none" style="margin-right:3px"><rect x="0.5" y="1.5" width="7" height="7" rx="1" stroke="currentColor" stroke-width="1"/><path d="M8 4l1.5-1v4L8 6V4z" stroke="currentColor" stroke-width="0.8" fill="none"/></svg>
         <span id="detect-scenes-label">${hasScenes ? 'Re-scan' : 'Scenes'}</span>
       </button>`
    : '';

  const finderBtn = `<button class="btn btn-ghost" style="font-size:10px;padding:4px 9px;min-height:24px" onclick="revealInFinder(${file.id})" title="Reveal in Finder">
       <svg width="11" height="11" viewBox="0 0 11 11" fill="none" style="margin-right:3px"><path d="M1 3.5L5.5 1L10 3.5V8.5L5.5 11L1 8.5V3.5Z" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/><path d="M1 3.5L5.5 6L10 3.5M5.5 6V11" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/></svg>
       Finder
     </button>`;

  document.getElementById('detail-header').innerHTML = `
    <div class="detail-header-name" title="${escHtml(file.path)}">${escHtml(file.filename)}</div>
    <div class="detail-header-meta">
      ${file.duration_seconds ? `<span>${formatDuration(file.duration_seconds)}</span>` : ''}
      <span class="status-label status-${file.status}">${file.status.toUpperCase()}</span>
      ${file.primary_shot_type ? `<span class="status-label">${(file.primary_shot_type).toUpperCase().replace('CLOSEUP','CLOSE-UP')}</span>` : ''}
    </div>
    <div class="detail-header-actions">${finderBtn}${detectBtn}</div>`;

  if (['pending', 'queued'].includes(file.status)) {
    document.getElementById('detail-transcript').innerHTML = '<div class="no-data">Waiting to process…</div>';
    return;
  }
  if (file.status === 'transcribing') {
    document.getElementById('detail-transcript').innerHTML =
      `<div class="processing-state"><div class="spinner"></div><div class="processing-label">Transcribing…</div></div>`;
    return;
  }

  const segments = await apiFetch(`/api/files/${fileId}/transcript`).catch(() => []);

  if (!segments.length) {
    if (file.status === 'silent') {
      document.getElementById('detail-transcript').innerHTML =
        '<div class="no-data" style="line-height:1.7">No speech in this clip.<br><span style="font-size:10px;color:var(--text3)">This is normal for b-roll, ambient footage, or silent clips.<br>Check the SCENES tab for visual classification.</span></div>';
    } else if (file.status === 'error') {
      document.getElementById('detail-transcript').innerHTML = '<div class="no-data">Transcription failed — try re-processing.</div>';
    } else {
      document.getElementById('detail-transcript').innerHTML = '<div class="no-data">No transcript yet.</div>';
    }
  } else {
    renderTranscript(segments);
  }

  // Auto-load scenes tab data in background — this will also update the Scenes/Re-scan label
  prefetchScenes(fileId).then(() => {
    const labelEl = document.getElementById('detect-scenes-label');
    if (labelEl && activeFileId === fileId) {
      labelEl.textContent = (scenesCache[fileId] || []).length ? 'Re-scan' : 'Scenes';
    }
  });
}

// Reveal source file in Finder
async function revealInFinder(fileId) {
  try {
    await apiFetch(`/api/files/${fileId}/reveal`, 'POST', {});
  } catch (e) {
    showToast('Could not reveal: ' + e.message, 3000);
  }
}

async function triggerDetectScenes(fileId, force = false) {
  const btn      = document.getElementById('detect-scenes-btn');
  const labelEl  = document.getElementById('detect-scenes-label');
  if (btn) btn.disabled = true;
  if (labelEl) labelEl.textContent = 'Detecting…';

  try {
    const res = await apiFetch(`/api/files/${fileId}/detect-scenes`, 'POST', { force });

    // Server says clip already has scenes — confirm
    if (res.status === 'already_detected') {
      if (btn) btn.disabled = false;
      if (labelEl) labelEl.textContent = 'Re-scan';
      const yes = await showConfirm({
        title: 'Already scanned',
        body: `This clip already has ${res.scene_count} scene${res.scene_count !== 1 ? 's' : ''} detected. Re-scan from scratch?`,
        confirmLabel: 'Re-scan',
        cancelLabel: 'Keep existing',
      });
      if (yes) return triggerDetectScenes(fileId, true);
      return;
    }

    showToast(force ? 'Re-scanning scenes…' : 'Scene detection started…', 3000);

    // Poll until done
    const poll = setInterval(async () => {
      const job = await apiFetch(`/api/jobs`).catch(() => ({}));
      const key = `scenes_${fileId}`;
      if (job[key]?.status === 'done') {
        clearInterval(poll);
        delete scenesCache[fileId];
        showToast(`✓ ${job[key].count} scenes detected`, 4000);
        loadClips();
        if (document.getElementById('scene-grid-wrap')?.style.display !== 'none') {
          renderSceneGrid(fileId, 'all');
        }
        if (btn) btn.disabled = false;
        if (labelEl) labelEl.textContent = 'Re-scan';
      } else if (job[key]?.status === 'error') {
        clearInterval(poll);
        showToast('Scene detection failed — try again', 3000);
        if (btn) btn.disabled = false;
        if (labelEl) labelEl.textContent = 'Scenes';
      }
    }, 1500);
  } catch (e) {
    showToast('Failed: ' + e.message, 3000);
    if (btn) btn.disabled = false;
    if (labelEl) labelEl.textContent = 'Scenes';
  }
}

// ── Lightweight confirm modal (Promise-based) ─────────────────────────────
function showConfirm({ title, body, confirmLabel = 'OK', cancelLabel = 'Cancel' }) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.style.display = 'flex';
    overlay.innerHTML = `
      <div class="modal-card" style="max-width:340px">
        <div class="modal-title">${escHtml(title)}</div>
        <div style="font-size:12px;color:var(--text2);line-height:1.6;margin:8px 0 16px">${escHtml(body)}</div>
        <div class="modal-actions">
          <button class="btn btn-ghost" data-action="cancel">${escHtml(cancelLabel)}</button>
          <button class="btn btn-primary" data-action="confirm">${escHtml(confirmLabel)}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const cleanup = (val) => { overlay.remove(); resolve(val); };
    overlay.querySelector('[data-action="confirm"]').addEventListener('click', () => cleanup(true));
    overlay.querySelector('[data-action="cancel"]').addEventListener('click', () => cleanup(false));
    overlay.addEventListener('click', e => { if (e.target === overlay) cleanup(false); });
    setTimeout(() => overlay.querySelector('[data-action="confirm"]').focus(), 50);
  });
}

// Prefetch scenes into cache without showing them yet
async function prefetchScenes(fileId) {
  if (scenesCache[fileId] !== undefined) return;
  try {
    const scenes = await apiFetch(`/api/files/${fileId}/scenes`);
    scenesCache[fileId] = scenes;
    // Update scenes tab label with count
    const tabBtn = document.getElementById('tab-scenes');
    if (tabBtn && activeFileId === fileId) {
      tabBtn.textContent = scenes.length ? `SCENES (${scenes.length})` : 'SCENES';
    }
  } catch (_) {
    scenesCache[fileId] = [];
  }
}

// ── Scene grid ──────────────────────────────────────────────────────────────

let sceneGridFilter = 'all';

const SHOT_SIZE_LABEL = {
  extreme_close_up: 'EXT CLOSE-UP',
  close_up:         'CLOSE-UP',
  medium:           'MEDIUM',
  full:             'FULL',
  wide:             'WIDE',
  extreme_wide:     'EXT WIDE',
  aerial:           'AERIAL',
};
const SHOT_ANGLE_LABEL = {
  eye_level:     'EYE-LEVEL',
  low:           'LOW ANGLE',
  high:          'HIGH ANGLE',
  dutch:         'DUTCH',
  over_shoulder: 'OTS',
  pov:           'POV',
};

function matchesSceneFilter(scene, filter) {
  if (filter === 'all') return true;
  if (filter.startsWith('size:'))   return scene.shot_size  === filter.slice(5);
  if (filter.startsWith('roll:'))   return scene.roll_type  === filter.slice(5);
  if (filter.startsWith('set:'))    return scene.setting    === filter.slice(4);
  // Legacy support
  return scene.shot_type === filter;
}

async function renderSceneGrid(fileId, filter = 'all') {
  sceneGridFilter = filter;
  const grid     = document.getElementById('scene-card-grid');
  const countEl  = document.getElementById('scene-grid-count');

  // Update pill states
  document.querySelectorAll('.scene-pill').forEach(p => {
    p.classList.toggle('active', p.dataset.filter === filter);
  });

  // Load from cache or API
  let scenes = scenesCache[fileId];
  if (scenes === undefined) {
    grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1">Loading scenes…</div>';
    try {
      scenes = await apiFetch(`/api/files/${fileId}/scenes`);
      scenesCache[fileId] = scenes;
    } catch (_) {
      scenes = [];
    }
  }

  const filtered = scenes.filter(s => matchesSceneFilter(s, filter));
  countEl.textContent = `${filtered.length} scene${filtered.length !== 1 ? 's' : ''}`;

  if (!filtered.length) {
    grid.innerHTML = scenes.length
      ? `<div class="empty-state" style="grid-column:1/-1">No scenes match this filter.</div>`
      : `<div class="empty-state" style="grid-column:1/-1">No scenes detected.<br>Click "Scenes" button above to run detection.</div>`;
    return;
  }

  grid.innerHTML = filtered.map(s => sceneCardHtml(s, null)).join('');
}

function sceneCardHtml(s, filenameOverride) {
  const imgHtml = s.thumbnail_path
    ? `<img src="/static/${escHtml(s.thumbnail_path)}" alt="Scene ${s.scene_num}" loading="lazy">`
    : `<div class="scene-card-placeholder"><svg width="20" height="14" viewBox="0 0 20 14" fill="none"><rect x="0.5" y="0.5" width="13" height="12" rx="1.5" fill="white" opacity="0.3"/><path d="M14 4l5-3v10l-5-3V4z" fill="white" opacity="0.2"/></svg></div>`;

  // Shot size badge (top-left)
  const sizeBadge = s.shot_size
    ? `<div class="scene-card-size size-${escHtml(s.shot_size)}">${escHtml(SHOT_SIZE_LABEL[s.shot_size] || s.shot_size)}</div>`
    : `<div class="scene-badge badge-${escHtml(s.shot_type || 'unknown')}">${escHtml((s.shot_type || '').toUpperCase().replace('CLOSEUP','CLOSE-UP') || '?')}</div>`;

  // Roll-type badge (top-right)
  const rollBadge = s.roll_type
    ? `<div class="scene-card-roll roll-${escHtml(s.roll_type)}">${s.roll_type === 'a_roll' ? 'A-ROLL' : 'B-ROLL'}</div>`
    : '';

  // Angle indicator (bottom-right)
  const angleBadge = (s.shot_angle && s.shot_angle !== 'eye_level')
    ? `<div class="scene-card-angle">${escHtml(SHOT_ANGLE_LABEL[s.shot_angle] || s.shot_angle.toUpperCase())}</div>`
    : '';

  // Location strip (separate from thumbnail overlay)
  const locationStrip = s.location
    ? `<div class="scene-card-location" title="${escHtml(s.description || '')}">
         <svg width="9" height="9" viewBox="0 0 9 9" fill="none"><circle cx="4.5" cy="4.5" r="1" fill="currentColor"/><circle cx="4.5" cy="4.5" r="3.5" stroke="currentColor" stroke-width="1"/></svg>
         <span class="scene-card-location-text">${escHtml(s.location)}</span>
       </div>`
    : '';

  // Tags row (still useful for desert/nature/night etc.)
  const tags = (s.tags || []).slice(0, 3).map(t =>
    `<span class="scene-tag">${escHtml(t)}</span>`
  ).join('');

  const footer = filenameOverride
    ? `<div class="scene-card-footer">
         <span class="scene-card-filename">${escHtml(filenameOverride)}</span>
       </div>`
    : '';

  return `
    <div class="scene-card" data-time="${s.start_time}" data-file="${s.file_id || ''}"
         onclick="jumpToSceneCard(this)" title="${formatTime(s.start_time)} – ${formatTime(s.end_time)}${s.description ? ' · ' + s.description : ''}">
      <div class="scene-card-img">
        ${imgHtml}
        ${sizeBadge}
        ${rollBadge}
        ${angleBadge}
        <div class="scene-card-time">${formatTime(s.start_time)}</div>
      </div>
      ${locationStrip}
      ${tags ? `<div class="scene-card-tags">${tags}</div>` : ''}
      ${footer}
    </div>`;
}

// AI refine — runs Claude vision on all scenes for the active clip
async function triggerAiClassify() {
  if (!activeFileId) return;
  if (offlineMode) { showToast('Toggle ONLINE to use AI refine.', 4000); return; }
  const btn = document.getElementById('scene-ai-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner" style="width:9px;height:9px;border-width:1.5px;display:inline-block;vertical-align:-1px;margin-right:4px"></span>Working…'; }

  try {
    const res = await apiFetch(`/api/files/${activeFileId}/classify-with-ai`, 'POST', {});
    showToast(`AI refining ${res.scene_count} scene${res.scene_count !== 1 ? 's' : ''}…`, 3000);
    const poll = setInterval(async () => {
      const jobs = await apiFetch('/api/jobs').catch(() => ({}));
      const job  = jobs[`ai_classify_${activeFileId}`];
      if (!job) return;
      if (btn && job.total) {
        btn.innerHTML = `<span class="spinner" style="width:9px;height:9px;border-width:1.5px;display:inline-block;vertical-align:-1px;margin-right:4px"></span>${job.done}/${job.total}`;
      }
      if (job.status === 'done') {
        clearInterval(poll);
        delete scenesCache[activeFileId];
        await renderSceneGrid(activeFileId, sceneGridFilter);
        loadClips();
        showToast(`✓ ${job.done} scenes refined`, 4000);
        if (btn) { btn.disabled = false; btn.innerHTML = '<svg width="11" height="11" viewBox="0 0 11 11" fill="none" style="margin-right:3px"><path d="M5.5 1L7 4.5H10.5L7.7 6.7 8.8 10.5 5.5 8.2 2.2 10.5 3.3 6.7.5 4.5H4L5.5 1z" fill="currentColor" opacity="0.85"/></svg>AI refine'; }
      } else if (job.status === 'error') {
        clearInterval(poll);
        showToast('AI refine failed: ' + (job.error || ''), 4000);
        if (btn) { btn.disabled = false; btn.innerHTML = 'AI refine'; }
      }
    }, 1500);
  } catch (e) {
    showToast('Failed: ' + e.message, 3000);
    if (btn) { btn.disabled = false; btn.innerHTML = 'AI refine'; }
  }
}

function jumpToSceneCard(el) {
  const startTime = parseFloat(el.dataset.time);
  const fileId    = parseInt(el.dataset.file);

  // If cross-clip scene, open that clip first then jump
  if (fileId && fileId !== activeFileId) {
    openClip(fileId).then(() => {
      setTimeout(() => jumpToTranscriptTime(startTime), 300);
    });
    return;
  }

  // Switch to transcript tab and jump
  showTab('transcript');
  jumpToTranscriptTime(startTime);

  // Highlight active card
  document.querySelectorAll('.scene-card').forEach(c => {
    c.classList.toggle('active', c === el);
  });
}

function jumpToTranscriptTime(startTime) {
  const segments = document.querySelectorAll('.segment');
  let closest = null;
  let closestDiff = Infinity;
  segments.forEach(seg => {
    const t = parseFloat(seg.dataset.time || 0);
    const diff = Math.abs(t - startTime);
    if (diff < closestDiff) { closestDiff = diff; closest = seg; }
  });
  if (closest) {
    closest.scrollIntoView({ behavior: 'smooth', block: 'start' });
    closest.style.background = 'rgba(240,165,0,0.1)';
    setTimeout(() => closest.style.background = '', 1500);
  }
}

function filterSceneGrid(type) {
  if (activeFileId) renderSceneGrid(activeFileId, type);
}

function showTab(name, loadData = true) {
  const isTranscript = name === 'transcript';
  const isScenes     = name === 'scenes';

  document.getElementById('detail-transcript').style.display  = isTranscript ? 'block' : 'none';
  document.getElementById('scene-grid-wrap').style.display    = isScenes ? 'flex' : 'none';
  document.getElementById('tab-transcript').classList.toggle('active', isTranscript);
  document.getElementById('tab-scenes').classList.toggle('active', isScenes);

  if (isScenes && activeFileId && loadData) {
    renderSceneGrid(activeFileId, sceneGridFilter);
  }
}

function renderTranscript(segments) {
  const el = document.getElementById('detail-transcript');
  if (!segments.length) {
    el.innerHTML = '<div class="no-data">No transcript yet.</div>';
    return;
  }

  const q = (activeHighlightQuery || '').trim();
  const regex = q ? new RegExp(`(${escRegex(q)})`, 'gi') : null;

  el.innerHTML = segments.map(seg => {
    const safe = escHtml(seg.text);
    const highlighted = regex ? safe.replace(regex, '<mark class="hit">$1</mark>') : safe;
    const isMatch = regex && regex.test(seg.text);
    return `
      <div class="segment ${isMatch ? 'segment-match' : ''}" data-time="${seg.start_time}">
        <div class="seg-time">${formatTime(seg.start_time)}</div>
        <div class="seg-text">${highlighted}</div>
      </div>`;
  }).join('');

  // Scroll to first match
  if (regex) {
    requestAnimationFrame(() => {
      const firstMatch = el.querySelector('.segment-match');
      if (firstMatch) {
        firstMatch.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    });
  }
}

// ── Now Processing ──

function updateNowProcessing() {
  const active = ['queued', 'transcribing', 'analyzing', 'transcribed', 'extracting_audio', 'silent'];
  const queued  = allFiles.filter(f => active.includes(allJobs[f.id]?.status || f.status));
  const current = allFiles.find(f =>
    ['transcribing', 'extracting_audio', 'analyzing', 'transcribed', 'silent']
    .includes(allJobs[f.id]?.status || f.status)
  );

  if (!queued.length && !current) { hideNowProcessing(); return; }

  document.getElementById('now-processing').style.display = 'block';

  const job = current ? allJobs[current.id] : null;
  const stageText = job?.stage || (job?.status ? job.status.toUpperCase() : 'QUEUED');
  const pct = job?.progress != null ? Math.max(0, Math.min(100, Math.round(job.progress))) : null;

  document.getElementById('np-stage').textContent = stageText;
  document.getElementById('np-pct').textContent = pct != null ? `${pct}%` : '';
  document.getElementById('np-filename').textContent = current ? current.filename : `${queued.length} clips waiting`;

  // Detail line — show extra info per stage
  const STAGE_DETAIL = {
    queued:            'Waiting in queue',
    extracting_audio:  'Reading audio stream from video',
    transcribing:      'Speech-to-text in progress (offline · Whisper)',
    transcribed:       'Transcript saved · running scene detector',
    silent:            'No speech found · still running scene detection',
    analyzing:         'AI analysis',
  };
  document.getElementById('np-detail').textContent =
    job?.status ? (STAGE_DETAIL[job.status] || '') : '';

  // Progress bar
  const bar = document.getElementById('np-bar-fill');
  if (bar) {
    if (pct != null) {
      bar.style.width = pct + '%';
      bar.classList.remove('indeterminate');
    } else {
      bar.style.width = '40%';
      bar.classList.add('indeterminate');
    }
  }

  document.getElementById('np-count').textContent = queued.length > 1 ? `${queued.length} clips remaining` : '';
}

function hideNowProcessing() {
  document.getElementById('now-processing').style.display = 'none';
}

// ── CSV Export ──

function exportCSV() {
  if (!currentProjectId) return;
  const btn = document.getElementById('export-csv-btn');
  btn.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" style="margin-right:4px;animation:spin 0.8s linear infinite"><circle cx="6" cy="6" r="5" stroke="currentColor" stroke-width="1.4" stroke-dasharray="20" stroke-dashoffset="5"/></svg>CSV';
  btn.disabled = true;
  const a = document.createElement('a');
  a.href = `/api/projects/${currentProjectId}/export/transcripts.csv`;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => {
    btn.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" style="margin-right:4px"><path d="M6 1v7M3 6l3 3 3-3M1 10h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>CSV';
    btn.disabled = false;
  }, 2000);
}

// ── Workspace switching ──

function setWorkspace(ws, loadStory = true) {
  // Block story workspace in offline mode
  if (ws === 'story' && offlineMode) {
    showToast('Offline mode — AI features are disabled. Toggle OFFLINE to enable.', 4000);
    return;
  }
  currentWorkspace = ws;
  const isClips = ws === 'clips';
  document.getElementById('workspace-clips').classList.toggle('active', isClips);
  document.getElementById('workspace-story').classList.toggle('active', !isClips);
  document.getElementById('project-body-clips').style.display = isClips ? 'flex' : 'none';
  document.getElementById('project-body-story').style.display = isClips ? 'none' : 'flex';
  if (!isClips && loadStory) loadStoryWorkspace();
}

// ── Completion banner ──

async function showCompletionBanner() {
  if (!currentProjectId) return;
  try {
    const s = await apiFetch(`/api/projects/${currentProjectId}/stats`);
    const msg = `✓ All ${s.done} clips processed — ${s.with_transcript} with transcripts${s.errors ? `, ${s.errors} errors` : ''}`;
    showToast(msg, 8000);
  } catch (_) {
    showToast('✓ Processing complete', 5000);
  }
}

let toastTimer = null;
function showToast(msg, duration = 5000) {
  const el = document.getElementById('completion-toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('visible'), duration);
}

// ════════════════════ STORY BIBLE ════════════════════

let biblePolltimer = null;

function openStoryBible() { setWorkspace('story'); }
function closeStoryBible() { setWorkspace('clips'); }

async function loadStoryWorkspace() {
  // Story workspace is now a Telegram launcher (web chat removed in v35)
  const btn   = document.getElementById('telegram-launch-btn');
  const label = document.getElementById('telegram-launch-btn-label');
  const stat  = document.getElementById('telegram-launch-status');
  if (!btn || !stat) return;

  stat.textContent = '';
  try {
    const s = await apiFetch('/api/settings');
    if (s.telegram_url && s.telegram_bot_username) {
      btn.href = s.telegram_url;
      btn.classList.remove('disabled');
      if (label) label.textContent = `Open @${s.telegram_bot_username}`;
      // Show readiness
      if (!s.telegram_chat_ids || !s.telegram_chat_ids.length) {
        stat.innerHTML = '⚠️ <b>Telegram chat ID not allowlisted.</b> Send <code>/start</code> to the bot first — it\'ll reply with your chat ID. Then add it via Settings.';
      } else if (s.offline_mode) {
        stat.innerHTML = '🔌 <b>Offline mode is ON.</b> AI features are disabled until you toggle ONLINE.';
      } else {
        stat.innerHTML = '✓ Bot online · synced with this Mac.';
      }
    } else {
      btn.classList.add('disabled');
      btn.removeAttribute('href');
      if (label) label.textContent = 'Bot not configured';
      stat.innerHTML = '⚠️ <b>No Telegram token set.</b> Create a bot with @BotFather and add the token to <code>settings.json</code>.';
    }
  } catch (e) {
    stat.textContent = 'Could not check bot status: ' + e.message;
  }
}

function showBibleTab(tab) {
  // Story Bible "analysis" tab removed in v32 — only chat + cut now
  ['chat', 'cut'].forEach(t => {
    const btn  = document.getElementById(`bible-tab-${t}`);
    const pane = document.getElementById(`bible-${t}-pane`);
    if (btn)  btn.classList.toggle('active', t === tab);
    if (pane) pane.style.display = t === tab ? 'flex' : 'none';
  });
  if (tab === 'chat') {
    renderChatMessages();
    setTimeout(() => document.getElementById('chat-input')?.focus(), 50);
  }
}

// Stub: kept for back-compat where older code might call it
function loadBibleAnalysis() { /* no-op since Story Bible removed */ }

function _updateAnalysisProgressBar(status) {
  const wrap  = document.getElementById('analysis-progress-wrap');
  const fill  = document.getElementById('analysis-progress-fill');
  const stage = document.getElementById('analysis-progress-stage');
  const pct   = document.getElementById('analysis-progress-pct');
  if (!wrap) return;

  const isActive = status.status === 'analyzing' || status.status === 'queued';
  wrap.style.display = isActive ? 'block' : 'none';
  if (!isActive) return;

  const bc = status.batch_current || 0;
  const bt = status.batch_total   || 0;
  const p  = bt > 0 ? Math.round((bc / bt) * 100) : 0;
  const MODEL_LABELS = { haiku: 'Claude Haiku', sonnet: 'Claude Sonnet', gemini: 'Gemini 2.5 Pro', groq: 'Groq' };

  fill.style.width  = (bt > 0 ? p : 100) + '%';
  fill.className    = bt > 0 ? 'progress-fill' : 'progress-fill indeterminate';
  stage.textContent = status.stage || 'Starting…';
  pct.textContent   = bt > 0 ? `${bc}/${bt} · ${p}%` : (MODEL_LABELS[status.model] || '');
}

async function _legacy_loadBibleAnalysis_removed() {
  // Legacy Story Bible loader — preserved for reference but no longer invoked
  return;
  const body   = document.getElementById('story-bible-body');
  const status = await apiFetch(`/api/projects/${currentProjectId}/analysis/status`).catch(() => ({ status: 'idle' }));

  _updateAnalysisProgressBar(status);

  const MODEL_LABELS = { haiku: 'Claude Haiku', sonnet: 'Claude Sonnet', gemini: 'Gemini 2.5 Pro', groq: 'Groq' };

  if (status.status === 'analyzing' || status.status === 'queued') {
    const lbl = MODEL_LABELS[status.model] || 'AI';
    body.innerHTML = `
      <div class="processing-state">
        <div class="spinner"></div>
        <div class="processing-label">${escHtml(status.stage || 'Analysing…')}</div>
        <div style="margin-top:10px;font-size:11px;color:var(--text3)">Via ${escHtml(lbl)} — progress saved every batch</div>
      </div>`;
    if (!biblePolltimer) {
      biblePolltimer = setInterval(async () => {
        const s = await apiFetch(`/api/projects/${currentProjectId}/analysis/status`).catch(() => ({ status: 'idle' }));
        _updateAnalysisProgressBar(s);
        if (s.status !== 'analyzing' && s.status !== 'queued') {
          clearInterval(biblePolltimer); biblePolltimer = null;
          loadBibleAnalysis();
        } else {
          const l = MODEL_LABELS[s.model] || 'AI';
          body.innerHTML = `
            <div class="processing-state">
              <div class="spinner"></div>
              <div class="processing-label">${escHtml(s.stage || 'Analysing…')}</div>
              <div style="margin-top:10px;font-size:11px;color:var(--text3)">Via ${escHtml(l)} — progress saved every batch</div>
            </div>`;
        }
      }, 3000);
    }
    return;
  }

  _updateAnalysisProgressBar({ status: 'idle' });

  if (status.status === 'error') {
    const saved = status.batches_saved || 0;
    const total = status.batches_total || 0;
    const isConn = (status.error || '').toLowerCase().includes('connection');
    const resumeMsg = saved > 0
      ? `<div style="color:var(--text2);font-size:12px;margin-bottom:14px">✓ ${saved}/${total} batches saved — resumes from batch ${saved + 1}</div>`
      : '';
    body.innerHTML = `
      <div class="no-data">
        <div style="color:var(--red);margin-bottom:8px">${isConn ? '⚡ Connection lost' : 'Generation failed'}</div>
        <div style="font-size:11px;color:var(--text3);margin-bottom:12px">${escHtml(status.error || 'Unknown error')}</div>
        ${resumeMsg}
        <button class="btn btn-primary" onclick="generateStoryBible()">
          ${saved > 0 ? `Resume from batch ${saved + 1}` : 'Try Again'}
        </button>
      </div>`;
    return;
  }

  try {
    const [data, stale] = await Promise.all([
      apiFetch(`/api/projects/${currentProjectId}/analysis`),
      apiFetch(`/api/projects/${currentProjectId}/analysis/stale`).catch(() => ({ stale: false, new_clips: 0 })),
    ]);
    renderStoryBible(data, stale);
  } catch (_) {
    const done    = allFiles.filter(f => f.status === 'done').length;
    const aiModel = document.getElementById('ai-model-select')?.value || 'haiku';
    const secsPerBatch = aiModel === 'groq' ? 22 : 5;
    const batchEst = Math.ceil(done / 8);
    const timeEst  = Math.ceil(batchEst * secsPerBatch / 60);
    const lbl = MODEL_LABELS[aiModel] || aiModel;

    body.innerHTML = `
      <div class="bible-generate-prompt">
        <div class="bible-generate-icon">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none"><rect x="3" y="4" width="14" height="16" rx="2" stroke="currentColor" stroke-width="1.5"/><path d="M7 8h8M7 12h8M7 16h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M17 4l4 4-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" opacity="0.5"/></svg>
        </div>
        <div class="bible-generate-title">Story Bible</div>
        <div class="bible-generate-desc">
          Analyse all ${done} transcribed clip${done !== 1 ? 's' : ''} — read in batches then synthesised into one full documentary breakdown.
        </div>
        ${done > 0 ? `<div style="font-size:11px;color:var(--text3);margin-bottom:2px">~${batchEst} batches via ${escHtml(lbl)} · est. ${timeEst}–${timeEst + 2} min</div>` : ''}
        ${done === 0 ? '<div style="color:var(--red);font-size:12px;margin-bottom:14px">No transcripts yet — process some clips first.</div>' : ''}
        <button class="btn btn-primary btn-lg" ${done === 0 ? 'disabled' : ''} onclick="generateStoryBible()">Generate Story Bible</button>
      </div>`;
  }
}

async function generateStoryBible() {
  if (offlineMode) { showToast('Offline mode — toggle OFFLINE to use AI features.', 4000); return; }
  const aiModel = document.getElementById('ai-model-select')?.value || 'haiku';
  const MODEL_LABELS = { haiku: 'Claude Haiku', sonnet: 'Claude Sonnet', gemini: 'Gemini 2.5 Pro', groq: 'Groq' };
  const label = MODEL_LABELS[aiModel] || aiModel;
  document.getElementById('story-bible-body').innerHTML = `
    <div class="processing-state"><div class="spinner"></div><div class="processing-label">Starting via ${escHtml(label)}…</div></div>`;
  _updateAnalysisProgressBar({ status: 'analyzing', stage: `Starting via ${label}…`, batch_current: 0, batch_total: 0, model: aiModel });
  try {
    await apiFetch(`/api/projects/${currentProjectId}/analyse`, 'POST', { ai_model: aiModel });
    loadBibleAnalysis();
  } catch (e) {
    _updateAnalysisProgressBar({ status: 'idle' });
    document.getElementById('story-bible-body').innerHTML = `<div class="no-data">Failed: ${escHtml(e.message)}</div>`;
  }
}

function renderStoryBible(data, staleInfo = null) {
  const body = document.getElementById('story-bible-body');

  let staleBanner = '';
  if (staleInfo?.stale && staleInfo.new_clips > 0) {
    const n = staleInfo.new_clips;
    staleBanner = `
      <div class="stale-banner">
        <span>⚡</span>
        <span style="flex:1"><strong>${n} new clip${n !== 1 ? 's' : ''}</strong> since last analysis</span>
        <button class="btn btn-primary stale-update-btn" onclick="generateStoryBible()">Update Bible</button>
      </div>`;
  }

  let html = `<div style="padding:14px 0 0;display:flex;justify-content:flex-end">
    <button class="btn btn-ghost" style="font-size:10px;padding:4px 10px" onclick="generateStoryBible()">↻ Regenerate</button>
  </div>${staleBanner}`;

  if (data.story_arc)
    html += `<div class="bible-section"><div class="bible-section-title">STORY ARC</div><p class="bible-text">${escHtml(data.story_arc)}</p></div>`;
  if (data.central_conflict)
    html += `<div class="bible-section"><div class="bible-section-title">CENTRAL CONFLICT</div><p class="bible-text">${escHtml(data.central_conflict)}</p></div>`;
  if (data.key_characters?.length) {
    html += `<div class="bible-section"><div class="bible-section-title">KEY CHARACTERS</div>`;
    html += data.key_characters.map(c =>
      `<div class="bible-character"><span class="bible-char-name">${escHtml(c.name)}</span><span class="bible-char-role">${escHtml(c.role)}</span></div>`
    ).join('');
    html += `</div>`;
  }
  if (data.themes?.length)
    html += `<div class="bible-section"><div class="bible-section-title">THEMES</div><div class="tags">${data.themes.map(t => `<span class="tag">${escHtml(t)}</span>`).join('')}</div></div>`;
  if (data.three_act_structure) {
    const s = data.three_act_structure;
    html += `<div class="bible-section"><div class="bible-section-title">THREE-ACT STRUCTURE</div>`;
    if (s.act1) html += `<div class="bible-act"><span class="bible-act-label">ACT I</span><p>${escHtml(s.act1)}</p></div>`;
    if (s.act2) html += `<div class="bible-act"><span class="bible-act-label">ACT II</span><p>${escHtml(s.act2)}</p></div>`;
    if (s.act3) html += `<div class="bible-act"><span class="bible-act-label">ACT III</span><p>${escHtml(s.act3)}</p></div>`;
    html += `</div>`;
  }
  if (data.must_use_clips?.length) {
    html += `<div class="bible-section"><div class="bible-section-title">MUST-USE CLIPS</div>`;
    html += data.must_use_clips.map(c =>
      `<div class="bible-clip"><div class="bible-clip-name">${escHtml(c.filename)}</div><div class="bible-clip-reason">${escHtml(c.reason)}</div></div>`
    ).join('');
    html += `</div>`;
  }
  if (data.missing_elements)
    html += `<div class="bible-section"><div class="bible-section-title">MISSING ELEMENTS</div><p class="bible-text">${escHtml(data.missing_elements)}</p></div>`;
  if (data.directors_notes)
    html += `<div class="bible-section"><div class="bible-section-title">DIRECTOR'S VISION</div><p class="bible-text bible-italic">${escHtml(data.directors_notes)}</p></div>`;

  html += `
    <div class="bible-section bible-section-chat-notes">
      <div class="bible-section-title" style="display:flex;justify-content:space-between;align-items:center">
        <span>CHAT NOTES</span>
        <button class="text-btn" style="font-size:10px" onclick="showBibleTab('chat')">+ Add via Chat →</button>
      </div>
      <div id="chat-notes-body" style="margin-top:8px"></div>
    </div>`;

  body.innerHTML = html;
  loadChatNotes();
}

// ════════════════════ CHAT ════════════════════

async function loadChatHistory() {
  if (!currentProjectId) return;
  try {
    const msgs = await apiFetch(`/api/projects/${currentProjectId}/chat`);
    chatHistory = msgs.map(m => ({ id: m.id, role: m.role, content: m.content, pinned: !!m.pinned }));
    renderChatMessages();
  } catch (_) {}
}

// Open a clip from a chat-citation chip — jumps to clips workspace + opens detail
function openClipFromChat(fileId) {
  setWorkspace('clips', false);
  setTimeout(() => openClip(fileId), 80);
}

// Reveal a saved FCPXML in Finder
async function revealCutInFinder(path) {
  if (!path) return;
  try {
    await apiFetch('/api/narrative-cut/reveal', 'POST', { path });
  } catch (e) {
    showToast('Could not reveal: ' + e.message, 3000);
  }
}

// Update chat sync banner when model selector changes; also re-load Bible if appropriate
function onChatModelChange() {
  const sel = document.getElementById('ai-model-select');
  const banner = document.getElementById('chat-sync-model');
  const LABELS = {
    haiku:  'Haiku 4.5',
    sonnet: 'Sonnet 4.5',
    opus:   'Opus 4.7',
    gemini: 'Gemini 2.5 Pro',
    groq:   'Groq Llama',
  };
  if (banner && sel) banner.textContent = LABELS[sel.value] || sel.value;
  loadBibleAnalysis();
}

function _detectClipMentions(text) {
  const matches = [...text.matchAll(/\b([\w\-]+\.(?:MP4|MOV|MXF|AVI|MKV|mp4|mov|mxf|avi|mkv))\b/g)];
  return [...new Set(matches.map(m => m[1]))];
}

function _formatChatContent(content) {
  // The LLM now returns Telegram HTML (<b>, <i>, <code>, <blockquote>).
  // Render those tags and treat any leaked markdown as fallback.
  let s = String(content || '');

  // Preserve allowed HTML tags by stashing
  const stash = [];
  const ALLOWED = /<\/?(?:b|i|code|blockquote|strong|em)>/gi;
  s = s.replace(ALLOWED, m => { stash.push(m); return `${stash.length - 1}`; });

  // Escape everything else
  s = escHtml(s);

  // Restore allowed tags
  s = s.replace(/(\d+)/g, (_, i) => stash[+i]);

  // Markdown fallback for cases LLM leaked **bold** or `code`
  s = s.replace(/\*\*([^\n*]+?)\*\*/g, '<b>$1</b>');
  s = s.replace(/`([^`\n]+?)`/g, '<code>$1</code>');

  // Line breaks
  s = s.replace(/\n/g, '<br>');
  return s;
}

function renderChatMessages() {
  const el = document.getElementById('chat-messages');
  if (!el) return;
  if (!chatHistory.length) {
    el.innerHTML = `<div class="chat-empty">
      <div class="chat-empty-title">What story are we cutting today?</div>
      <div class="chat-empty-sub">Ask for hooks, vulnerable moments, specific themes — I'll surface the strongest takes with clickable file links.</div>
    </div>`;
    return;
  }
  el.innerHTML = chatHistory.map((m, idx) => {
    if (m.content === '…') {
      return `<div class="chat-msg chat-msg-assistant"><div class="chat-bubble chat-thinking">
        <span class="chat-dot"></span><span class="chat-dot"></span><span class="chat-dot"></span>
      </div></div>`;
    }

    // Build cited-clip chips (clickable to open the clip detail panel)
    let citedHtml = '';
    if (m.role === 'assistant' && m.cited_clips && m.cited_clips.length) {
      citedHtml = `<div class="chat-cited">` + m.cited_clips.map(c =>
        `<span class="chat-cited-chip" onclick="openClipFromChat(${c.file_id})">
          <svg width="9" height="9" viewBox="0 0 9 9" fill="none"><rect x="1" y="2" width="5" height="5" rx="0.5" fill="currentColor" opacity="0.7"/><path d="M6 3.5l2.5-1.5v5l-2.5-1.5z" fill="currentColor" opacity="0.5"/></svg>
          ${escHtml(c.filename)}
        </span>`
      ).join('') + `</div>`;
    }

    // Cut download bar — appears when message has a cut payload
    let cutHtml = '';
    if (m.role === 'assistant' && m.cut) {
      const safePath = escHtml(m.cut.xml_path || '');
      cutHtml = `<div class="chat-cut-bar">
        <div class="chat-cut-info">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path d="M3 1h7l3 3v11H3V1z" stroke="currentColor" stroke-width="1.3" fill="none"/>
            <path d="M10 1v3h3" stroke="currentColor" stroke-width="1.3" fill="none"/>
            <path d="M5 8h6M5 11h4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
          </svg>
          <div>
            <div class="chat-cut-title">FCPXML ready · ${m.cut.clip_count} clips · ${m.cut.duration}s</div>
            <div class="chat-cut-path">${safePath}</div>
          </div>
        </div>
        <div class="chat-cut-actions">
          <a class="chat-cut-btn primary" href="${escHtml(m.cut.xml_url)}" download>Download</a>
          <button class="chat-cut-btn" onclick="revealCutInFinder('${safePath.replace(/'/g, "\\'")}')">Reveal in Finder</button>
        </div>
      </div>`;
    }

    const clips   = m.role === 'assistant' ? _detectClipMentions(m.content) : [];
    const cutBtn  = clips.length ? `<button class="chat-action-btn" onclick="sendClipsTocut(${JSON.stringify(clips).replace(/"/g, '&quot;')},${idx})">⚡ Use in Cut</button>` : '';
    const pinBtn  = m.role === 'assistant' && m.id
      ? `<button class="chat-action-btn" onclick="togglePin(${m.id},${idx})">${m.pinned ? '📌 Unpin' : '📌 Pin'}</button>` : '';
    const actions = (cutBtn || pinBtn) ? `<div class="chat-actions">${cutBtn}${pinBtn}</div>` : '';
    return `<div class="chat-msg chat-msg-${m.role}" data-idx="${idx}">
      <div class="chat-bubble">${_formatChatContent(m.content)}${citedHtml}${cutHtml}</div>
      ${actions}
    </div>`;
  }).join('');
  el.scrollTop = el.scrollHeight;
}

async function togglePin(messageId, idx) {
  const msg    = chatHistory[idx];
  const pinned = !msg.pinned;
  msg.pinned   = pinned;
  renderChatMessages();
  try {
    await apiFetch(`/api/projects/${currentProjectId}/chat/${messageId}/pin?pinned=${pinned}`, 'POST');
    if (currentWorkspace === 'story') loadChatNotes();
  } catch (e) {
    msg.pinned = !pinned;
    renderChatMessages();
  }
}

function sendClipsTocut(clips, idx) {
  showBibleTab('cut');
  const msg = chatHistory[idx];
  const firstSentence = (msg.content || '').split(/[.\n]/)[0].slice(0, 120);
  const themeInput = document.getElementById('cut-theme-input');
  if (themeInput && firstSentence) themeInput.value = firstSentence;
  const cutTab = document.getElementById('bible-tab-cut');
  if (cutTab) {
    cutTab.style.background = 'rgba(240,165,0,0.15)';
    setTimeout(() => cutTab.style.background = '', 1200);
  }
}

// Auto-resize textarea as user types
function autoResizeChat(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 240) + 'px';
}

// Enter to send, Shift+Enter for newline
function onChatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChatMessage();
  }
}

// Suggestion pill → fill textarea and send
function useSuggestion(text) {
  const input = document.getElementById('chat-input');
  if (!input) return;
  input.value = text;
  autoResizeChat(input);
  sendChatMessage();
}

// Find Footage button → prompts user for a theme and runs a search
function promptFindFootage() {
  const theme = prompt('What footage are you looking for?\n\ne.g. "vulnerable moments", "shots about purpose", "drone shots in the desert"');
  if (!theme || !theme.trim()) return;
  const input = document.getElementById('chat-input');
  input.value = `Find footage about: ${theme.trim()}`;
  autoResizeChat(input);
  sendChatMessage();
}

// Make XML button → prompts for theme + duration and triggers cut generation
function promptMakeXML() {
  const theme = prompt('What story / theme for the cut?\n\ne.g. "the cost of ambition", "moments of doubt", "freedom"');
  if (!theme || !theme.trim()) return;
  const durStr = prompt('How long should the cut be?\n\nSeconds (default 60):', '60');
  const duration = parseInt(durStr || '60', 10) || 60;
  const input = document.getElementById('chat-input');
  input.value = `Make a ${duration}s cut about ${theme.trim()}`;
  autoResizeChat(input);
  sendChatMessage();
}

async function sendChatMessage() {
  if (chatBusy) return;
  if (offlineMode) { showToast('Offline mode — toggle OFFLINE to use AI chat.', 4000); return; }
  const input = document.getElementById('chat-input');
  const text  = input.value.trim();
  if (!text) return;

  input.value = '';
  input.style.height = 'auto';

  // Hide suggestions after first message
  const sugg = document.getElementById('chat-suggestions');
  if (sugg) sugg.style.display = 'none';
  chatHistory.push({ role: 'user', content: text });
  renderChatMessages();
  chatBusy = true;

  const sendBtn = document.getElementById('chat-send-btn');
  sendBtn.disabled = true;

  chatHistory.push({ role: 'assistant', content: '…' });
  renderChatMessages();

  const aiModel   = document.getElementById('ai-model-select')?.value || 'haiku';
  const apiMessages = chatHistory.slice(0, -1).filter(m => m.content !== '…').map(m => ({ role: m.role, content: m.content }));

  try {
    const res = await apiFetch(`/api/projects/${currentProjectId}/chat`, 'POST', {
      messages: apiMessages,
      ai_model: aiModel,
    });
    chatHistory[chatHistory.length - 1] = {
      id: res.message_id,
      role: 'assistant',
      content: res.reply,
      cited_clips: res.cited_clips || [],
      cut: res.cut || null,
      pinned: false,
    };
    if (currentWorkspace === 'story') loadChatNotes();
  } catch (e) {
    chatHistory[chatHistory.length - 1] = { role: 'assistant', content: `Error: ${e.message}` };
  }

  renderChatMessages();
  chatBusy = false;
  sendBtn.disabled = false;
  input.focus();
}

async function clearChatHistory() {
  if (!confirm('Clear all chat history for this project?')) return;
  await apiFetch(`/api/projects/${currentProjectId}/chat`, 'DELETE').catch(() => {});
  chatHistory = [];
  renderChatMessages();
  loadChatNotes();
}

async function loadChatNotes() {
  const el = document.getElementById('chat-notes-body');
  if (!el) return;
  try {
    const notes = await apiFetch(`/api/projects/${currentProjectId}/chat/notes`);
    if (!notes.length) {
      el.innerHTML = '<div style="font-size:12px;color:var(--text3);padding:4px 0">No pinned notes yet. Pin chat insights using 📌 in the Chat tab.</div>';
      return;
    }
    el.innerHTML = notes.map(n => `
      <div class="chat-note-row">
        <div class="chat-note-content">${_formatChatContent(n.content)}</div>
        <div class="chat-note-meta">${timeAgo(n.created_at)}</div>
        <button class="chat-action-btn" onclick="unpinFromAnalysis(${n.id})">✕ Unpin</button>
      </div>`).join('');
  } catch (_) {
    if (el) el.innerHTML = '';
  }
}

async function unpinFromAnalysis(messageId) {
  await apiFetch(`/api/projects/${currentProjectId}/chat/${messageId}/pin?pinned=false`, 'POST').catch(() => {});
  const idx = chatHistory.findIndex(m => m.id === messageId);
  if (idx !== -1) chatHistory[idx].pinned = false;
  renderChatMessages();
  loadChatNotes();
}

// ════════════════════ NARRATIVE CUT ════════════════════

async function generateNarrativeCut() {
  if (offlineMode) { showToast('Offline mode — toggle OFFLINE to use AI cut generator.', 4000); return; }
  const theme    = document.getElementById('cut-theme-input').value.trim();
  const duration = parseInt(document.getElementById('cut-duration-input').value) || 90;
  const btn      = document.getElementById('generate-cut-btn');
  const result   = document.getElementById('cut-result');

  if (!theme) { alert('Please enter a theme or story angle.'); return; }
  if (!currentProjectId) return;

  btn.disabled = true;
  btn.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" style="margin-right:5px;animation:spin 0.75s linear infinite"><circle cx="6" cy="6" r="5" stroke="currentColor" stroke-width="1.5" stroke-dasharray="20" stroke-dashoffset="5"/></svg>Scanning…';

  result.style.display = 'block';
  result.innerHTML = `
    <div class="processing-state" style="padding:24px 0">
      <div class="spinner"></div>
      <div class="processing-label">AI selecting best moments for:<br><em style="color:var(--text2)">"${escHtml(theme)}"</em></div>
      <div style="margin-top:10px;font-size:11px;color:var(--text3)">Reading transcripts…</div>
    </div>`;

  const aiModel = document.getElementById('ai-model-select')?.value || 'haiku';
  try {
    const cut = await apiFetch(`/api/projects/${currentProjectId}/narrative-cut`, 'POST', {
      theme, duration, ai_model: aiModel,
    });
    renderCutResult(cut);
  } catch (e) {
    result.innerHTML = `<div class="no-data" style="color:var(--red)">Failed: ${escHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" style="margin-right:5px"><path d="M6 1L7.5 5H11.5L8.3 7.3 9.5 11.5 6 9 2.5 11.5 3.7 7.3.5 5H4.5L6 1z" fill="currentColor"/></svg>Generate Cut';
  }
}

function renderCutResult(cut) {
  const result = document.getElementById('cut-result');
  const mins   = Math.floor(cut.total_duration / 60);
  const secs   = Math.round(cut.total_duration % 60);
  const durStr = mins > 0 ? `${mins}:${String(secs).padStart(2, '0')}` : `${secs}s`;

  const clipsHtml = (cut.clips || []).map((c, i) => {
    const in_m  = Math.floor(c.in_time / 60);
    const in_s  = Math.floor(c.in_time % 60);
    const out_m = Math.floor(c.out_time / 60);
    const out_s = Math.floor(c.out_time % 60);
    const dur   = Math.round(c.out_time - c.in_time);
    const role  = (c.narrative_role || '').split('—')[0].trim();
    return `
      <div class="cut-clip-row">
        <div class="cut-clip-num">${i + 1}</div>
        <div style="flex:1;min-width:0">
          <div class="cut-clip-filename">${escHtml(c.filename)}</div>
          <div class="cut-clip-role">${escHtml(role)}</div>
          <div class="cut-clip-quote">"${escHtml((c.quote || '').slice(0, 80))}"</div>
        </div>
        <div class="cut-clip-timecode">
          ${in_m}:${String(in_s).padStart(2, '0')} → ${out_m}:${String(out_s).padStart(2, '0')}
          <span class="cut-clip-dur">${dur}s</span>
        </div>
      </div>`;
  }).join('');

  result.innerHTML = `
    <div class="cut-result-header">
      <div>
        <div class="cut-result-title">${escHtml(cut.title)}</div>
        <div class="cut-result-meta">${cut.clips.length} clips · ${durStr}</div>
      </div>
      <button class="btn btn-primary" onclick="downloadCutXML()">
        <svg width="11" height="11" viewBox="0 0 11 11" fill="none" style="margin-right:4px"><path d="M5.5 1v7M3 6l2.5 2.5L8 6M1 9h9" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
        FCPXML
      </button>
    </div>
    ${cut.narrative_note ? `<div class="cut-result-note">${escHtml(cut.narrative_note)}</div>` : ''}
    <div class="cut-clips-list">${clipsHtml}</div>
    <div style="margin-top:12px;font-size:11px;color:var(--text3);line-height:1.9">
      Drag the .fcpxml into DaVinci Resolve's Media Pool.
      ${cut.csv_path ? `<br><span style="font-family:var(--mono);opacity:0.6;user-select:all">${escHtml(cut.csv_path)}</span>` : ''}
    </div>`;
}

function downloadCutXML() {
  if (!currentProjectId) return;
  const a = document.createElement('a');
  a.href = `/api/projects/${currentProjectId}/narrative-cut/latest.fcpxml`;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ════════════════════ UTILITIES ════════════════════

async function apiFetch(path, method = 'GET', body = null) {
  const opts = { method, headers: {} };
  if (body !== null) {
    opts.body = JSON.stringify(body);
    opts.headers['Content-Type'] = 'application/json';
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function formatTime(s) {
  if (s == null) return '0:00';
  const m   = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
}

function formatDuration(s) {
  const h   = Math.floor(s / 3600);
  const m   = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}

function timeAgo(ts) {
  const diff = (Date.now() - new Date(ts).getTime()) / 1000;
  if (diff < 60)    return 'just now';
  if (diff < 3600)  return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function escHtml(str) {
  return String(str ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function escRegex(str) {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
