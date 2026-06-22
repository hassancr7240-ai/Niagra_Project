/* ── PM Automation System — Dashboard JS ── */

const API = '';
let currentUser = null;
let histOffset = 0;
const histLimit = 20;
let histTotal = 0;

// ─── Authenticated download helper ───────────────────────────────────────────
// Appends ?token=XXX to local download URLs so the browser can open them directly
function makeDownloadUrl(url) {
  if (!url) return '#';
  const token = localStorage.getItem('pm_token');
  if (!token) return url;
  // Azure Blob URLs already have SAS tokens — don't add ours
  if (url.startsWith('https://') && url.includes('.blob.core.windows.net')) return url;
  if (url.startsWith('sftp://') || url.startsWith('ftp://')) return url;
  // Local /api/download/... path — append token as query param
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

// ─── Init ─────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  const token = localStorage.getItem('pm_token');
  if (!token) { window.location.href = '/frontend/index.html'; return; }

  // Validate token is still alive before loading anything
  try {
    const check = await fetch(API + '/health');
    const healthData = await check.json();
    if (healthData.env === 'development') {
      document.getElementById('topbarEnv').style.display = 'inline-block';
    }
  } catch(e) {}

  const userJson = localStorage.getItem('pm_user');
  if (userJson) {
    currentUser = JSON.parse(userJson);
    initUserUI();
  }

  // Apply role-based UI
  applyRoleUI();

  // Load initial data sequentially to avoid token race condition
  await loadMachinesForSelects();
  await loadDashboard();
});

function initUserUI() {
  const u = currentUser;
  const nameEl = document.getElementById('userName');
  const avatarEl = document.getElementById('userAvatar');
  if (nameEl) nameEl.textContent = u.name || u.email;
  if (avatarEl) avatarEl.textContent = (u.name || u.email || '?')[0].toUpperCase();
}

// Every signed-in user sees the same simple menu. Permissions are still
// enforced server-side — actions a user isn't allowed to do return a clear
// error from the API rather than being hidden behind role-specific menus.
function applyRoleUI() {}

// ─── Navigation ───────────────────────────────────────────────────────────────

function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));

  const page = document.getElementById(`page-${name}`);
  if (page) page.classList.add('active');

  const navItem = document.querySelector(`.nav-item[onclick*="'${name}'"]`);
  if (navItem) navItem.classList.add('active');

  const titles = {
    dashboard: 'Dashboard',
    generate: 'Generate PM Checklist',
    history: 'PM History',
    library: 'PM Library',
    machines: 'Machines',
    upload: 'Upload Manual',
    export: 'Export Data',
    checklist: 'Fill PM Checklist',
    chat: 'PM Chat Assistant',
  };
  document.getElementById('pageTitle').textContent = titles[name] || name;

  // Lazy-load page data
  if (name === 'history') loadHistory();
  if (name === 'library') loadLibrary();
  if (name === 'machines') loadMachines();
  if (name === 'upload') loadUploads();
  if (name === 'chat') initChat();
  if (name === 'checklist') loadRecentPMsForChecklist();
}

// ─── Token management ─────────────────────────────────────────────────────────

async function getFreshToken() {
  let token = localStorage.getItem('pm_token');
  if (!token) return null;

  // Check expiry from JWT payload — refresh if < 10 minutes left
  try {
    const payload = JSON.parse(atob(token.split('.')[1]));
    const expiresAt = payload.exp * 1000;
    if (Date.now() > expiresAt - 10 * 60 * 1000) {
      const user = JSON.parse(localStorage.getItem('pm_user') || '{}');
      if (user.email) {
        const r = await fetch(API + '/dev/token', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: user.email, name: user.name || user.email, role: user.role || 'Manager' }),
        });
        if (r.ok) {
          const data = await r.json();
          token = data.access_token;
          localStorage.setItem('pm_token', token);
          console.log('Token auto-refreshed silently');
        }
      }
    }
  } catch(e) { /* decode failed — use token as-is */ }

  return token;
}

// ─── API helper ───────────────────────────────────────────────────────────────

async function api(path, opts = {}) {
  const token = await getFreshToken();
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(API + path, { ...opts, headers });
  if (res.status === 401) {
    // Try one silent re-auth before logging out
    const user = JSON.parse(localStorage.getItem('pm_user') || '{}');
    if (user.email) {
      try {
        const r = await fetch(API + '/dev/token', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: user.email, name: user.name, role: user.role }),
        });
        if (r.ok) {
          const data = await r.json();
          localStorage.setItem('pm_token', data.access_token);
          // Retry original request with new token
          const retryHeaders = { ...headers, 'Authorization': `Bearer ${data.access_token}` };
          const retry = await fetch(API + path, { ...opts, headers: retryHeaders });
          if (retry.ok) return retry.status === 204 ? null : retry.json();
        }
      } catch(e) {}
    }
    logout();
    return null;
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Request failed' }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

// ─── Dashboard ────────────────────────────────────────────────────────────────

async function loadDashboard() {
  try {
    const data = await api('/api/history/dashboard');
    if (!data) return;

    // Stats
    document.getElementById('stat-total').textContent = data.stats.total_pms;
    document.getElementById('stat-month').textContent = data.stats.completed_this_month;
    document.getElementById('stat-overdue').textContent = data.stats.overdue_count;
    document.getElementById('stat-duesoon').textContent = data.stats.due_soon_count;
    document.getElementById('stat-machines').textContent = data.stats.machines_covered;
    document.getElementById('stat-tasks').textContent = data.stats.total_tasks;

    // Overdue
    const overdueBody = document.getElementById('overdueBody');
    document.getElementById('overdueBadge').textContent = data.overdue.length;
    if (data.overdue.length === 0) {
      overdueBody.innerHTML = `<tr><td colspan="4"><div class="empty-state"><div class="empty-icon">✅</div><p>All PMs on track</p></div></td></tr>`;
    } else {
      overdueBody.innerHTML = data.overdue.map(o => `
        <tr class="overdue-row">
          <td><strong>${o.machine_name}</strong></td>
          <td>${o.interval_label}</td>
          <td><span class="badge badge-red">OVERDUE</span></td>
          <td><button class="btn btn-primary btn-sm" onclick="quickGenerate('${o.machine_id}',${o.interval_hours})">Generate</button></td>
        </tr>`).join('');
    }

    // Recent PMs
    const recentBody = document.getElementById('recentBody');
    if (data.recent_pms.length === 0) {
      recentBody.innerHTML = `<tr><td colspan="4"><div class="empty-state"><p>No PM records yet</p></div></td></tr>`;
    } else {
      recentBody.innerHTML = data.recent_pms.map(r => `
        <tr>
          <td>${r.machine_name}</td>
          <td>${r.interval_label}</td>
          <td>${r.technician_name}</td>
          <td>${statusBadge(r.status)}</td>
        </tr>`).join('');
    }

    // Schedule
    const schedBody = document.getElementById('scheduleBody');
    if (data.schedule.length === 0) {
      schedBody.innerHTML = `<tr><td colspan="6"><div class="empty-state"><p>No schedule data</p></div></td></tr>`;
    } else {
      schedBody.innerHTML = data.schedule.map(s => {
        const statusBadgeHtml = s.status === 'OVERDUE' ? `<span class="badge badge-red">OVERDUE</span>` :
          s.status === 'DUE_SOON' ? `<span class="badge badge-amber">DUE SOON</span>` :
          s.status === 'NEVER_DONE' ? `<span class="badge badge-grey">NEVER DONE</span>` :
          `<span class="badge badge-green">ON TRACK</span>`;
        return `
        <tr class="${s.status === 'OVERDUE' ? 'overdue-row' : s.status === 'DUE_SOON' ? 'due-soon-row' : ''}">
          <td><strong>${s.machine_name}</strong></td>
          <td>${s.interval_label}</td>
          <td>${s.last_completed_at ? new Date(s.last_completed_at).toLocaleDateString() : '—'}</td>
          <td>${s.predicted_next_due_hours ? s.predicted_next_due_hours + ' hrs' : '—'}</td>
          <td>${statusBadgeHtml}</td>
          <td><button class="btn btn-outline btn-sm" onclick="quickGenerate('${s.machine_id}',${s.interval_hours})">Generate</button></td>
        </tr>`;
      }).join('');
    }
  } catch (e) {
    console.error('Dashboard load error:', e);
  }
}

// ─── Machines for Selects ─────────────────────────────────────────────────────

async function loadMachinesForSelects() {
  try {
    const machines = await api('/api/machines');
    if (!machines) return;

    ['gen-machine', 'upload-machine', 'histFilter'].forEach(id => {
      const sel = document.getElementById(id);
      if (!sel) return;
      const hasDefault = sel.options[0].value === '';
      machines.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.machine_id;
        opt.textContent = m.name;
        sel.appendChild(opt);
      });
    });
  } catch(e) {
    console.error('Failed to load machines:', e);
  }
}

async function loadIntervals() {
  const machineId = document.getElementById('gen-machine').value;
  const intSel = document.getElementById('gen-interval');
  intSel.innerHTML = '<option value="">Select interval...</option>';
  if (!machineId) return;

  try {
    const lib = await api('/api/library');
    if (!lib) return;
    const machine = lib.machines.find(m => m.machine_id === machineId);
    if (!machine) return;
    machine.intervals.forEach(iv => {
      const opt = document.createElement('option');
      opt.value = iv.hours;
      opt.textContent = `${iv.label} (${iv.natural_label}) — ${iv.task_count} tasks`;
      intSel.appendChild(opt);
    });
  } catch(e) {}
}

// ─── Generate PM ──────────────────────────────────────────────────────────────

async function submitGenerate(e) {
  e.preventDefault();
  const btn = document.getElementById('genBtn');
  const alert = document.getElementById('genAlert');
  const result = document.getElementById('genResult');
  result.style.display = 'none';
  alert.innerHTML = '';

  btn.disabled = true;
  btn.textContent = '⚙️ Generating...';
  showLoading('Generating PM document — please wait...');

  const payload = {
    machine_id: document.getElementById('gen-machine').value,
    interval_hours: parseInt(document.getElementById('gen-interval').value),
    work_order: document.getElementById('gen-wo').value,
    technician_name: document.getElementById('gen-tech').value,
    output_format: document.getElementById('gen-format').value,
    notes: document.getElementById('gen-notes').value || null,
  };
  const storage = document.getElementById('gen-storage').value;
  if (storage) payload.storage_target = storage;

  try {
    const data = await api('/api/generate', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (!data) throw new Error('No response');

    hideLoading();
    btn.disabled = false;
    btn.textContent = '⚙️ Generate PM Document';

    result.style.display = 'block';
    document.getElementById('genSuccess').innerHTML = `
      <strong>✅ PM document generated successfully!</strong><br/>
      ${data.task_count} tasks · ${(data.file_size_bytes / 1024).toFixed(1)} KB · ${data.storage_target}
    `;
    const link = document.getElementById('genDownloadLink');
    link.href = makeDownloadUrl(data.download_url);
    link.textContent = `⬇ Download ${data.output_format?.toUpperCase() || 'PDF'}`;
    document.getElementById('genFileName').textContent = data.file_name;
    document.getElementById('genHash').textContent = data.file_hash;
  } catch(err) {
    hideLoading();
    btn.disabled = false;
    btn.textContent = '⚙️ Generate PM Document';
    alert.innerHTML = `<div class="alert alert-danger">❌ ${err.message}</div>`;
  }
}

function quickGenerate(machineId, intervalHours) {
  showPage('generate');
  setTimeout(() => {
    const machSel = document.getElementById('gen-machine');
    machSel.value = machineId;
    loadIntervals().then(() => {
      document.getElementById('gen-interval').value = intervalHours;
    });
  }, 100);
}

async function previewTasks() {
  const machineId = document.getElementById('gen-machine').value;
  const intervalHours = document.getElementById('gen-interval').value;
  if (!machineId || !intervalHours) {
    alert('Please select a machine and interval first.');
    return;
  }

  const modal = document.getElementById('taskPreviewModal');
  const title = document.getElementById('taskPreviewTitle');
  const body = document.getElementById('taskPreviewBody');
  body.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';
  modal.classList.add('open');

  try {
    const data = await api(`/api/library/${machineId}/${intervalHours}`);
    if (!data) return;

    title.textContent = `${data.machine_name} — ${data.interval_label} (${data.task_count} tasks)`;
    const stateColors = { RUNNING: 'green', STOPPED: 'amber', POWERED_OFF: 'purple' };
    body.innerHTML = `
      <div class="table-wrapper">
        <table>
          <thead><tr><th>#</th><th>Area</th><th>Action</th><th>Description</th><th>State</th><th>⚠</th></tr></thead>
          <tbody>
            ${data.tasks.map(t => `
              <tr ${t.safety_flag ? 'style="background:#FFF0F0"' : ''}>
                <td><strong>${t.task_no}</strong></td>
                <td><span class="badge badge-grey">${t.area}</span></td>
                <td>${t.action}</td>
                <td style="font-size:12px">${t.description}${t.part_number ? `<br/><code style="color:var(--navy)">PART: ${t.part_number}</code>` : ''}</td>
                <td><span class="badge state-${t.machine_state.toLowerCase()}">${t.machine_state.replace('_',' ')}</span></td>
                <td>${t.safety_flag ? '⚠️' : ''}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  } catch(e) {
    body.innerHTML = `<div class="alert alert-danger">${e.message}</div>`;
  }
}

// ─── History ──────────────────────────────────────────────────────────────────

async function loadHistory() {
  const machineId = document.getElementById('histFilter')?.value || '';
  const tbody = document.getElementById('historyBody');
  tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state"><div class="spinner"></div></div></td></tr>`;

  try {
    const url = `/api/history?limit=${histLimit}&offset=${histOffset}${machineId ? `&machine_id=${machineId}` : ''}`;
    const data = await api(url);
    if (!data) return;

    histTotal = data.total;
    document.getElementById('histCount').textContent = `Showing ${histOffset + 1}–${Math.min(histOffset + histLimit, histTotal)} of ${histTotal}`;
    document.getElementById('histPrev').disabled = histOffset === 0;
    document.getElementById('histNext').disabled = histOffset + histLimit >= histTotal;

    if (data.records.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8"><div class="empty-state"><div class="empty-icon">🕐</div><p>No PM records found</p></div></td></tr>`;
      return;
    }

    tbody.innerHTML = data.records.map(r => `
      <tr>
        <td style="font-size:11px;color:var(--grey-500)">${new Date(r.created_at).toLocaleString()}</td>
        <td><strong>${r.machine_name}</strong></td>
        <td>${r.interval_label}</td>
        <td><code style="font-size:11px">${r.work_order}</code></td>
        <td>${r.technician_name}</td>
        <td>${statusBadge(r.status)}</td>
        <td>
          ${r.download_url
            ? `<a href="${makeDownloadUrl(r.download_url)}" target="_blank" class="btn btn-primary btn-sm">⬇ PDF</a>`
            : ''}
          <button class="btn btn-outline btn-sm" style="margin-left:4px"
            onclick="fillChecklistById('${r.record_id}')" title="Fill Checklist for this PM">
            ✅ Fill
          </button>
          ${(r.status === 'COMPLETED' && canApprove())
            ? `<button class="btn btn-success btn-sm" style="margin-left:4px" onclick="approveRecord('${r.record_id}')">✓ Approve</button>`
            : ''}
        </td>
        <td>
          <span style="font-size:10px;color:var(--grey-500);font-family:monospace"
            title="PM Record ID — click to copy" onclick="copyText('${r.record_id}',this)"
            style="cursor:pointer">
            ${r.record_id.substring(0, 8)}…
          </span>
        </td>
      </tr>`).join('');
  } catch(e) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="alert alert-danger">${e.message}</div></td></tr>`;
  }
}

function histPage(dir) {
  histOffset = Math.max(0, histOffset + dir * histLimit);
  loadHistory();
}

function fillChecklistById(recordId) {
  showPage('checklist');
  setTimeout(() => {
    const inp = document.getElementById('cl-record-id');
    if (inp) { inp.value = recordId; loadChecklist(); }
  }, 200);
}

function copyText(text, el) {
  navigator.clipboard.writeText(text).then(() => {
    const orig = el.textContent;
    el.textContent = 'Copied!';
    setTimeout(() => { el.textContent = orig; }, 1500);
  });
}

async function approveRecord(recordId) {
  if (!confirm('Approve this PM record?')) return;
  try {
    await api(`/api/history/${recordId}/approve`, {
      method: 'POST',
      body: JSON.stringify({ notes: '' }),
    });
    loadHistory();
  } catch(e) {
    alert('Approval failed: ' + e.message);
  }
}

function canApprove() {
  return currentUser && ['Manager', 'Supervisor'].includes(currentUser.role);
}

// ─── Library ──────────────────────────────────────────────────────────────────

async function loadLibrary() {
  const content = document.getElementById('libraryContent');
  content.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';

  try {
    const data = await api('/api/library');
    if (!data) return;

    let html = `<div style="margin-bottom:12px;font-size:13px;color:var(--grey-500)">
      ${data.total_machines} machines · ${data.total_tasks} tasks · ${data.total_intervals} intervals
    </div>`;

    data.machines.forEach(m => {
      html += `
      <div style="margin-bottom:20px">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
          <strong style="font-size:15px">${m.name}</strong>
          <span class="badge badge-grey">${m.manufacturer}</span>
          <span class="badge badge-blue">${m.machine_type}</span>
          ${m.location ? `<span style="font-size:12px;color:var(--grey-500)">${m.location}</span>` : ''}
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          ${m.intervals.map(iv => `
            <div style="background:var(--grey-100);border:1px solid var(--grey-300);border-radius:6px;padding:10px 14px;cursor:pointer;min-width:100px"
              onclick="viewInterval('${m.machine_id}',${iv.hours},'${m.name} ${iv.label}')">
              <div style="font-size:16px;font-weight:700;color:var(--navy)">${iv.label}</div>
              <div style="font-size:11px;color:var(--grey-500)">${iv.natural_label}</div>
              <div style="font-size:12px;margin-top:4px">${iv.task_count} tasks</div>
            </div>`).join('')}
        </div>
      </div>`;
    });

    content.innerHTML = html;
  } catch(e) {
    content.innerHTML = `<div class="alert alert-danger">${e.message}</div>`;
  }
}

function viewInterval(machineId, hours, title) {
  document.getElementById('taskPreviewTitle').textContent = title;
  const body = document.getElementById('taskPreviewBody');
  body.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';
  document.getElementById('taskPreviewModal').classList.add('open');

  api(`/api/library/${machineId}/${hours}`).then(data => {
    if (!data) return;
    body.innerHTML = `
      <div class="table-wrapper">
        <table>
          <thead><tr><th>#</th><th>Area</th><th>Action</th><th>Description</th><th>State</th><th>Part #</th><th>⚠</th></tr></thead>
          <tbody>
            ${data.tasks.map(t => `
              <tr ${t.safety_flag ? 'style="background:#FFF0F0"' : ''}>
                <td><strong>${t.task_no}</strong></td>
                <td><span class="badge badge-grey">${t.area}</span></td>
                <td>${t.action}</td>
                <td style="font-size:12px">${t.description}</td>
                <td><span class="badge state-${t.machine_state.toLowerCase()}">${t.machine_state.replace('_',' ')}</span></td>
                <td>${t.part_number ? `<code>${t.part_number}</code>` : ''}</td>
                <td>${t.safety_flag ? '⚠️' : ''}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  }).catch(e => { body.innerHTML = `<div class="alert alert-danger">${e.message}</div>`; });
}

// ─── Machines ─────────────────────────────────────────────────────────────────

async function loadMachines() {
  const tbody = document.getElementById('machinesBody');
  try {
    const machines = await api('/api/machines');
    if (!machines) return;
    if (machines.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state"><p>No machines registered</p></div></td></tr>`;
      return;
    }
    tbody.innerHTML = machines.map(m => `
      <tr>
        <td><code>${m.machine_id}</code></td>
        <td><strong>${m.name}</strong></td>
        <td>${m.manufacturer}</td>
        <td>${m.model}</td>
        <td><span class="badge ${m.machine_type === 'KRONES' ? 'badge-blue' : 'badge-grey'}">${m.machine_type}</span></td>
        <td>${m.location || '—'}</td>
        <td><span class="badge ${m.is_active ? 'badge-green' : 'badge-grey'}">${m.is_active ? 'ACTIVE' : 'INACTIVE'}</span></td>
      </tr>`).join('');
  } catch(e) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="alert alert-danger">${e.message}</div></td></tr>`;
  }
}

function openAddMachineModal() {
  document.getElementById('addMachineModal').classList.add('open');
}

async function submitNewMachine(e) {
  e.preventDefault();
  try {
    await api('/api/machines', {
      method: 'POST',
      body: JSON.stringify({
        machine_id: document.getElementById('new-machine-id').value.toUpperCase(),
        name: document.getElementById('new-machine-name').value,
        manufacturer: document.getElementById('new-machine-mfr').value,
        model: document.getElementById('new-machine-model').value,
        machine_type: document.getElementById('new-machine-type').value,
        location: document.getElementById('new-machine-loc').value || null,
      }),
    });
    closeModal('addMachineModal');
    loadMachines();
    loadMachinesForSelects();
  } catch(e) {
    alert('Failed to create machine: ' + e.message);
  }
}

// ─── Upload ───────────────────────────────────────────────────────────────────

// ─── Upload & RAG Pipeline (full integrated workflow) ─────────────────────────

let _currentManualId = null;
let _pipelinePoller = null;

const PIPELINE_STEPS = [
  { key: 'UPLOADED',       label: 'Uploaded',               icon: '📤' },
  { key: 'CLASSIFYING',    label: 'Classifying manufacturer', icon: '🔍' },
  { key: 'CHUNKING',       label: 'Chunking text (500w)',    icon: '✂️' },
  { key: 'EMBEDDING',      label: 'Embedding with IBM Granite', icon: '🧠' },
  { key: 'EXTRACTING',     label: 'Extracting PM tasks',     icon: '⚙️' },
  { key: 'PENDING_REVIEW', label: 'Ready for review',        icon: '✅' },
  { key: 'APPROVED',       label: 'Approved & added to library', icon: '🎉' },
];

function renderPipelineSteps(currentStatus) {
  const currentIdx = PIPELINE_STEPS.findIndex(s => s.key === currentStatus);
  return PIPELINE_STEPS.map((step, i) => {
    let state = 'pending';
    if (i < currentIdx) state = 'done';
    else if (i === currentIdx) state = 'active';
    const color = state === 'done' ? 'var(--green)' : state === 'active' ? 'var(--navy)' : 'var(--grey-300)';
    const textColor = state === 'pending' ? 'var(--grey-500)' : 'var(--text)';
    return `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--grey-200)">
      <span style="font-size:16px">${state === 'done' ? '✅' : state === 'active' ? '<span class="spinner" style="width:14px;height:14px;border-width:2px"></span>' : '⬜'}</span>
      <span style="font-size:13px;color:${textColor};font-weight:${state === 'active' ? '700' : '400'}">${step.icon} ${step.label}</span>
      ${state === 'active' ? '<span class="badge badge-amber" style="margin-left:auto;font-size:10px">RUNNING</span>' : ''}
      ${state === 'done' ? '<span style="margin-left:auto;font-size:11px;color:var(--green)">✓</span>' : ''}
    </div>`;
  }).join('');
}

async function submitUpload(e) {
  e.preventDefault();
  const btn = document.getElementById('uploadBtn');
  const alertEl = document.getElementById('uploadAlert');
  btn.disabled = true;
  btn.textContent = '⬆ Uploading...';
  alertEl.innerHTML = '';

  const file = document.getElementById('upload-file').files[0];
  if (!file) { alertEl.innerHTML = '<div class="alert alert-danger">Please select a PDF file</div>'; btn.disabled = false; btn.textContent = '⬆ Upload & Start AI Processing'; return; }

  const machineId = document.getElementById('upload-machine').value;
  const formData = new FormData();
  formData.append('file', file);
  if (machineId) formData.append('machine_id', machineId);

  const token = await getFreshToken();
  try {
    const res = await fetch(API + '/api/manual/upload', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${token}` },
      body: formData,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: 'Upload failed' }));
      throw new Error(err.detail);
    }
    const data = await res.json();
    _currentManualId = data.manual_id;

    // Show progress panel
    document.getElementById('pipelineProgress').style.display = 'block';
    document.getElementById('uploadForm').reset();
    btn.disabled = false;
    btn.textContent = '⬆ Upload & Start AI Processing';

    // Start polling
    _startPipelinePoller(_currentManualId);
  } catch(err) {
    alertEl.innerHTML = `<div class="alert alert-danger">❌ ${err.message}</div>`;
    btn.disabled = false;
    btn.textContent = '⬆ Upload & Start AI Processing';
  }
}

function _startPipelinePoller(manualId) {
  if (_pipelinePoller) clearInterval(_pipelinePoller);
  _pipelinePoller = setInterval(async () => {
    await _pollPipelineStatus(manualId);
  }, 3000);
  _pollPipelineStatus(manualId);
}

async function _pollPipelineStatus(manualId) {
  try {
    const data = await api(`/api/manual/uploads/${manualId}`);
    if (!data) return;
    _updatePipelineUI(data);
    if (['APPROVED', 'FAILED'].includes(data.status)) {
      clearInterval(_pipelinePoller);
    }
  } catch(e) {
    console.warn('Pipeline poll error:', e);
  }
}

function _updatePipelineUI(data) {
  const stepsEl = document.getElementById('pipelineSteps');
  const badge = document.getElementById('pipelineStatusBadge');
  const detailsEl = document.getElementById('pipelineDetails');
  const reviewCard = document.getElementById('reviewCard');
  const generateCard = document.getElementById('generateCard');

  if (stepsEl) stepsEl.innerHTML = renderPipelineSteps(data.status);

  if (badge) {
    const badgeMap = { UPLOADED:'badge-grey', CLASSIFYING:'badge-amber', CHUNKING:'badge-amber',
      EMBEDDING:'badge-amber', EXTRACTING:'badge-amber', PENDING_REVIEW:'badge-blue',
      APPROVED:'badge-green', FAILED:'badge-red' };
    badge.className = `badge ${badgeMap[data.status] || 'badge-grey'}`;
    badge.textContent = data.status.replace('_',' ');
  }

  if (detailsEl) {
    const info = [];
    if (data.detected_manufacturer) info.push(`Manufacturer: <strong>${data.detected_manufacturer}</strong>`);
    if (data.detected_chapters?.length) info.push(`Maintenance chapters: <strong>${data.detected_chapters.join(', ')}</strong>`);
    if (data.extracted_task_count) info.push(`Tasks extracted: <strong>${data.extracted_task_count}</strong>`);
    if (data.error_message) info.push(`<span style="color:var(--red)">Error: ${data.error_message}</span>`);
    detailsEl.innerHTML = info.join(' &nbsp;|&nbsp; ');
  }

  // Show review card when PENDING_REVIEW
  if (data.status === 'PENDING_REVIEW' && reviewCard) {
    reviewCard.style.display = 'block';
    document.getElementById('reviewTaskCount').textContent = `${data.extracted_task_count || 0} tasks`;

    // Populate machine dropdown for review
    const sel = document.getElementById('review-machine-select');
    if (sel && sel.options.length === 1) {
      const genSel = document.getElementById('gen-machine');
      if (genSel) Array.from(genSel.options).slice(1).forEach(o => sel.appendChild(o.cloneNode(true)));
    }
    if (data.machine_id && sel) sel.value = data.machine_id;

    // Show extracted tasks preview
    const tasksEl = document.getElementById('reviewTasksList');
    if (tasksEl && data.extracted_tasks?.length) {
      tasksEl.innerHTML = `<table style="width:100%;font-size:12px">
        <thead><tr><th style="padding:4px 8px;background:var(--grey-200)">#</th><th style="padding:4px 8px;background:var(--grey-200)">Area</th><th style="padding:4px 8px;background:var(--grey-200)">Action</th><th style="padding:4px 8px;background:var(--grey-200)">Description (first 80 chars)</th><th style="padding:4px 8px;background:var(--grey-200)">Hrs</th></tr></thead>
        <tbody>${data.extracted_tasks.slice(0,20).map(t => `
          <tr style="border-bottom:1px solid var(--grey-200)">
            <td style="padding:3px 8px">${t.task_no || '—'}</td>
            <td style="padding:3px 8px">${t.area || '—'}</td>
            <td style="padding:3px 8px">${t.action || '—'}</td>
            <td style="padding:3px 8px;font-size:11px">${(t.description || '').substring(0,80)}${(t.description||'').length>80?'…':''}</td>
            <td style="padding:3px 8px">${t.interval_hours || '—'}</td>
          </tr>`).join('')}
        </tbody></table>
        ${data.extracted_tasks.length > 20 ? `<p style="font-size:11px;color:var(--grey-500);text-align:center;margin:8px 0">... and ${data.extracted_tasks.length - 20} more tasks</p>` : ''}`;
    } else if (tasksEl) {
      tasksEl.innerHTML = '<div class="empty-state" style="padding:20px"><p>Tasks will appear here after extraction</p></div>';
    }
  }

  if (data.status === 'APPROVED' && generateCard) {
    reviewCard.style.display = 'none';
    generateCard.style.display = 'block';
    const s = document.getElementById('approvalSuccess');
    if (s) s.innerHTML = `✅ <strong>${data.extracted_task_count || 'New'} tasks added to PM Library for ${data.machine_id || 'the machine'}.</strong> You can now generate PM documents using these tasks.`;
    loadIntervalButtons(data.machine_id);
  }
}

function loadIntervalButtons(machineId) {
  if (!machineId) return;
  api(`/api/library/${machineId}/intervals 2>/dev/null`).catch(() => null); // graceful
  api('/api/library').then(lib => {
    if (!lib) return;
    const machine = lib.machines.find(m => m.machine_id === machineId);
    if (!machine) return;
    const container = document.getElementById('generateIntervalButtons');
    if (!container) return;
    container.innerHTML = machine.intervals.filter(iv => iv.task_count > 0).map(iv =>
      `<button class="btn btn-primary btn-sm" onclick="quickGenerate('${machineId}',${iv.hours})">
        Generate ${iv.label} PM (${iv.task_count} tasks)
      </button>`
    ).join('');
  }).catch(() => null);
}

async function approvePipeline() {
  if (!_currentManualId) return;
  const machineId = document.getElementById('review-machine-select')?.value;
  if (!machineId) { alert('Please select a machine first'); return; }

  const btn = event.target;
  btn.disabled = true;
  btn.textContent = '⏳ Approving...';

  try {
    const token = await getFreshToken();
    const fd = new FormData();
    fd.append('machine_id', machineId);
    const res = await fetch(API + `/api/manual/uploads/${_currentManualId}/approve`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${token}` },
      body: fd,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);
    _updatePipelineUI({ ...(await api(`/api/manual/uploads/${_currentManualId}`)), status: 'APPROVED', machine_id: machineId, extracted_task_count: data.tasks_added_to_library });
    loadUploads();
    loadMachinesForSelects();
  } catch(e) {
    alert('Approval failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '✓ Approve & Add to PM Library';
  }
}

function startNewChat() {
  showPage('chat');
  setTimeout(() => {
    const machId = document.getElementById('review-machine-select')?.value;
    if (machId) {
      const filter = document.getElementById('chat-machine-filter');
      if (filter) filter.value = machId;
    }
    newChatSession();
  }, 300);
}

async function loadUploads() {
  const tbody = document.getElementById('uploadQueueBody');
  if (!tbody) return;
  try {
    const uploads = await api('/api/manual/uploads');
    if (!uploads || uploads.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state"><p>No uploads yet</p></div></td></tr>`;
      return;
    }
    tbody.innerHTML = uploads.map(u => `
      <tr>
        <td style="font-size:12px">${u.filename}</td>
        <td>${u.machine_id || '—'}</td>
        <td>${pipelineBadge(u.status)}</td>
        <td>${u.detected_manufacturer || '—'}</td>
        <td><strong>${u.task_count}</strong></td>
        <td style="font-size:11px">${u.uploaded_by || '—'}</td>
        <td>
          ${u.status === 'PENDING_REVIEW'
            ? `<button class="btn btn-success btn-sm" onclick="resumePipelineReview('${u.manual_id}')">✓ Review</button>`
            : u.status === 'APPROVED'
              ? `<button class="btn btn-outline btn-sm" onclick="quickGenerate('${u.machine_id || ''}',8)">Generate</button>`
              : `<button class="btn btn-outline btn-sm" onclick="refreshUploadStatus('${u.manual_id}')">↻ Status</button>`}
        </td>
      </tr>`).join('');
  } catch(e) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="alert alert-danger">${e.message}</div></td></tr>`;
  }
}

async function resumePipelineReview(manualId) {
  _currentManualId = manualId;
  document.getElementById('pipelineProgress').style.display = 'block';
  _pollPipelineStatus(manualId);
}

async function refreshUploadStatus(manualId) {
  _currentManualId = manualId;
  document.getElementById('pipelineProgress').style.display = 'block';
  _startPipelinePoller(manualId);
}

async function approveManual(manualId, machineId) {
  return resumePipelineReview(manualId);
}

// ─── Utilities ────────────────────────────────────────────────────────────────

function statusBadge(status) {
  const map = {
    PENDING: 'badge-grey', IN_PROGRESS: 'badge-amber',
    COMPLETED: 'badge-blue', APPROVED: 'badge-green',
  };
  return `<span class="badge ${map[status] || 'badge-grey'}">${status}</span>`;
}

function pipelineBadge(status) {
  const map = {
    UPLOADED: 'badge-grey', CLASSIFYING: 'badge-amber', CHUNKING: 'badge-amber',
    EMBEDDING: 'badge-amber', EXTRACTING: 'badge-amber',
    PENDING_REVIEW: 'badge-blue', APPROVED: 'badge-green', FAILED: 'badge-red',
  };
  return `<span class="badge ${map[status] || 'badge-grey'}">${status}</span>`;
}

function closeModal(id) {
  document.getElementById(id).classList.remove('open');
}

function showLoading(text) {
  document.getElementById('loadingText').textContent = text || 'Loading...';
  document.getElementById('loadingOverlay').classList.add('open');
}

function hideLoading() {
  document.getElementById('loadingOverlay').classList.remove('open');
}

// ─── Checklist helpers ───────────────────────────────────────────────────────

function showPageAndLoadLatest() {
  // Show the recent PMs picker in sidebar
  loadRecentPMsForChecklist();
}

async function loadRecentPMsForChecklist() {
  const container = document.getElementById('recentPMsForChecklist');
  if (!container) return;
  try {
    const data = await api('/api/history?limit=10');
    if (!data || !data.records?.length) {
      container.innerHTML = `<div style="font-size:12px;color:var(--grey-500);padding:8px">
        No PM records yet.<br/>Generate a PM first from the Generate PM page.
      </div>`;
      return;
    }
    container.innerHTML = data.records.map(r => `
      <div onclick="fillChecklistById('${r.record_id}')"
        style="padding:8px 10px;border-bottom:1px solid var(--grey-200);cursor:pointer;font-size:12px;
               transition:background .15s" onmouseover="this.style.background='var(--grey-100)'"
               onmouseout="this.style.background=''">
        <div style="font-weight:600;color:var(--navy)">${r.machine_name} — ${r.interval_label}</div>
        <div style="color:var(--grey-500);font-size:11px">${r.work_order} | ${r.technician_name}</div>
        <div style="display:flex;justify-content:space-between;margin-top:2px">
          ${statusBadge(r.status)}
          <span style="font-size:10px;color:var(--grey-500)">${new Date(r.created_at).toLocaleDateString()}</span>
        </div>
      </div>`).join('');
  } catch(e) {
    container.innerHTML = `<div style="font-size:12px;color:var(--red);padding:8px">${e.message}</div>`;
  }
}

// Auto-load recent PMs when checklist page is opened
const _origShowPage = showPage;

function logout() {
  localStorage.removeItem('pm_token');
  localStorage.removeItem('pm_user');
  window.location.href = '/frontend/index.html';
}

// ─── Export (Manager) ─────────────────────────────────────────────────────────

async function _downloadFromApi(url) {
  const token = localStorage.getItem('pm_token');
  try {
    const r = await fetch(API + url, { headers: { 'Authorization': `Bearer ${token}` } });
    if (!r.ok) throw new Error('Export failed: ' + r.status);
    const cd = r.headers.get('Content-Disposition') || '';
    const fn = cd.match(/filename="([^"]+)"/)?.[1] || 'export.csv';
    const b = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(b);
    a.download = fn;
    a.click();
  } catch (e) {
    alert('Export failed: ' + e.message);
  }
}

function exportHistory() {
  const machineId = document.getElementById('export-machine')?.value || '';
  const url = '/api/export/history/csv' + (machineId ? `?machine_id=${machineId}` : '');
  _downloadFromApi(url);
}

function exportLibrary() {
  _downloadFromApi('/api/export/library/csv');
}

function exportAuditLog() {
  _downloadFromApi('/api/export/audit-logs/csv');
}

// Also populate the export machine filter
document.addEventListener('DOMContentLoaded', () => {
  setTimeout(() => {
    const exportSel = document.getElementById('export-machine');
    const genSel = document.getElementById('gen-machine');
    if (exportSel && genSel) {
      Array.from(genSel.options).slice(1).forEach(opt => {
        exportSel.appendChild(opt.cloneNode(true));
      });
    }
  }, 2000);
});


// ─── Fill Checklist (Technician) ─────────────────────────────────────────────

let _checklistData = null;

async function loadChecklist() {
  const recordId = document.getElementById('cl-record-id').value.trim();
  if (!recordId) { alert('Please enter a Record ID'); return; }

  const content = document.getElementById('checklistContent');
  content.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';

  try {
    const data = await api(`/api/checklist/${recordId}`);
    if (!data) return;
    _checklistData = data;
    renderChecklist(data);
  } catch(e) {
    content.innerHTML = `<div class="alert alert-danger">
      Record not found. Generate a PM first, then enter the Record ID from the History page.
      <br/>${e.message}
    </div>`;
  }
}

function renderChecklist(data) {
  const content = document.getElementById('checklistContent');
  const pct = data.completion_percentage;
  const stateColors = { RUNNING: 'state-running', STOPPED: 'state-stopped', POWERED_OFF: 'state-powered_off' };

  content.innerHTML = `
    <div style="background:var(--grey-100);border-radius:8px;padding:16px;margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <div>
          <strong>${data.machine_id}</strong> &nbsp;|&nbsp; ${data.interval_hours}hr PM &nbsp;|&nbsp; WO: ${data.work_order}
        </div>
        <div>${statusBadge(data.status)} &nbsp; <strong>${pct}%</strong> complete</div>
      </div>
      <div class="progress"><div class="progress-bar" style="width:${pct}%;background:${pct===100?'var(--green)':'var(--amber)'}"></div></div>
    </div>
    <div class="table-wrapper">
      <table id="checklistTable">
        <thead><tr><th>#</th><th>Area</th><th>Action</th><th>Description</th><th>State</th><th>Initial</th><th>Done</th></tr></thead>
        <tbody>
          ${data.tasks.map(t => `
            <tr id="task-row-${t.task_id}" ${t.safety_flag ? 'style="background:#FFF0F0"' : ''}>
              <td><strong>${t.task_no}</strong></td>
              <td><span class="badge badge-grey">${t.area}</span></td>
              <td>${t.action}${t.safety_flag ? ' ⚠️' : ''}</td>
              <td style="font-size:12px">${t.description}${t.part_number ? `<br/><code>PART: ${t.part_number}</code>` : ''}</td>
              <td><span class="badge ${stateColors[t.machine_state]||'badge-grey'}">${t.machine_state.replace('_',' ')}</span></td>
              <td><input type="text" id="init-${t.task_id}" value="${t.initialed_by||''}" placeholder="Initials" maxlength="5"
                style="width:60px;padding:4px 6px;border:1.5px solid var(--grey-300);border-radius:4px;font-size:12px;text-align:center" /></td>
              <td><input type="checkbox" id="done-${t.task_id}" ${t.is_done?'checked':''} onchange="updateTaskRow('${t.task_id}')"
                style="width:18px;height:18px;cursor:pointer" /></td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>
    <div style="display:flex;gap:10px;margin-top:16px">
      <button class="btn btn-success" onclick="submitChecklist('${data.record_id}')">
        Submit Checklist
      </button>
      <button class="btn btn-outline" onclick="markAllDone()">Mark All Done</button>
      <span style="font-size:12px;color:var(--grey-500);align-self:center">
        ${data.completed_tasks}/${data.total_tasks} tasks completed
      </span>
    </div>`;
}

function updateTaskRow(taskId) {
  const done = document.getElementById(`done-${taskId}`)?.checked;
  const row = document.getElementById(`task-row-${taskId}`);
  if (row) row.style.opacity = done ? '0.6' : '1';
}

function markAllDone() {
  if (!_checklistData) return;
  _checklistData.tasks.forEach(t => {
    const cb = document.getElementById(`done-${t.task_id}`);
    if (cb) cb.checked = true;
    const init = document.getElementById(`init-${t.task_id}`);
    if (init && !init.value && currentUser) init.value = (currentUser.name || 'TECH').slice(0,5).toUpperCase();
    updateTaskRow(t.task_id);
  });
}

async function submitChecklist(recordId) {
  if (!_checklistData) return;
  const completedTasks = _checklistData.tasks.map(t => ({
    task_id: t.task_id,
    task_no: t.task_no,
    initialed_by: document.getElementById(`init-${t.task_id}`)?.value || '',
    is_done: document.getElementById(`done-${t.task_id}`)?.checked || false,
    notes: null,
  }));

  const done = completedTasks.filter(t => t.is_done).length;
  if (done === 0) { alert('Please complete at least one task before submitting.'); return; }

  showLoading('Submitting checklist...');
  try {
    const result = await api(`/api/checklist/${recordId}`, {
      method: 'POST',
      body: JSON.stringify({ completed_tasks: completedTasks }),
    });
    hideLoading();
    if (result) {
      alert(`Checklist submitted! ${result.completed_count}/${result.total_tasks} tasks done (${result.completion_percentage}%). Status: ${result.status}`);
      loadChecklist();
    }
  } catch(e) {
    hideLoading();
    alert('Submit failed: ' + e.message);
  }
}


// Close modals on overlay click
document.querySelectorAll('.modal-overlay').forEach(overlay => {
  overlay.addEventListener('click', e => {
    if (e.target === overlay) overlay.classList.remove('open');
  });
});


// ─── Chat ─────────────────────────────────────────────────────────────────────

let _chatSessionId = null;
let _chatSending = false;

async function initChat() {
  // Populate machine filter from existing select
  const machineFilter = document.getElementById('chat-machine-filter');
  if (machineFilter && machineFilter.options.length === 1) {
    const genSel = document.getElementById('gen-machine');
    if (genSel) {
      Array.from(genSel.options).slice(1).forEach(opt => {
        machineFilter.appendChild(opt.cloneNode(true));
      });
    }
  }
  // Check AI status and show correct badge
  try {
    const status = await api('/api/chat/status');
    if (status) {
      if (!status.ai_ready) {
        const warning = document.getElementById('chatApiWarning');
        if (warning) warning.style.display = 'block';
      } else {
        // Show green "AI connected" badge with model name
        const badge = document.getElementById('chatAiReadyBadge');
        const modelEl = document.getElementById('chatModelName');
        if (badge) badge.style.display = 'block';
        if (modelEl && status.models) {
          const model = status.models.chat_model || status.models.classification_model || '';
          modelEl.textContent = `Model: ${model}`;
        }
        if (status.rag_ready) {
          const ragBadge = document.getElementById('chatRagBadge');
          if (ragBadge) ragBadge.style.display = 'inline-block';
        }
      }
    }
  } catch(e) {}
  await loadChatSessions();
}

async function loadChatSessions() {
  const list = document.getElementById('chatSessionList');
  try {
    const sessions = await api('/api/chat/sessions');
    if (!sessions || sessions.length === 0) {
      list.innerHTML = '<div style="padding:20px 12px;font-size:12px;color:var(--grey-500);text-align:center">No conversations yet.<br/>Click + New to start.</div>';
      return;
    }
    list.innerHTML = sessions.map(s => `
      <div class="chat-session-item ${_chatSessionId === s.session_id ? 'active' : ''}"
           onclick="loadSession('${s.session_id}', ${JSON.stringify(s.title).replace(/"/g, '&quot;')}, '${s.machine_id || ''}')">
        <div class="chat-session-title">${escapeHtml(s.title)}</div>
        <div class="chat-session-meta">
          ${s.machine_id ? `<span class="badge badge-grey" style="font-size:9px">${s.machine_id}</span>` : ''}
          <span style="font-size:10px;color:var(--grey-500)">${s.message_count} msgs</span>
        </div>
        <button class="chat-session-delete" onclick="deleteSession(event,'${s.session_id}')">×</button>
      </div>`).join('');
  } catch(e) {
    list.innerHTML = `<div class="alert alert-danger" style="margin:8px;font-size:12px">${e.message}</div>`;
  }
}

async function newChatSession() {
  const machineId = document.getElementById('chat-machine-filter')?.value || null;
  try {
    const session = await api('/api/chat/sessions', {
      method: 'POST',
      body: JSON.stringify({ machine_id: machineId || null, title: 'New Chat' }),
    });
    if (!session) return;
    _chatSessionId = session.session_id;
    document.getElementById('chatTitle').textContent = 'New Chat';
    document.getElementById('chatSubtitle').textContent = machineId ? `Machine: ${machineId}` : 'Ask anything about preventive maintenance';
    clearChatMessages();
    await loadChatSessions();
  } catch(e) {
    alert('Failed to create session: ' + e.message);
  }
}

async function loadSession(sessionId, title, machineId) {
  _chatSessionId = sessionId;
  document.getElementById('chatTitle').textContent = title;
  document.getElementById('chatSubtitle').textContent = machineId ? `Machine: ${machineId}` : 'Multi-turn conversation';
  clearChatMessages();

  try {
    const messages = await api(`/api/chat/sessions/${sessionId}/messages`);
    if (!messages || messages.length === 0) return;

    const welcome = document.getElementById('chatWelcome');
    if (welcome) welcome.style.display = 'none';

    messages.forEach(m => appendMessage(m.role, m.content, m.has_checklist));
    scrollChatToBottom();
  } catch(e) {
    appendMessage('assistant', `Error loading session: ${e.message}`, false);
  }
  await loadChatSessions();
}

async function deleteSession(e, sessionId) {
  e.stopPropagation();
  if (!confirm('Delete this conversation?')) return;
  try {
    await api(`/api/chat/sessions/${sessionId}`, { method: 'DELETE' });
    if (_chatSessionId === sessionId) {
      _chatSessionId = null;
      clearChatMessages();
      document.getElementById('chatTitle').textContent = 'PM Assistant';
    }
    await loadChatSessions();
  } catch(ex) {
    alert('Delete failed: ' + ex.message);
  }
}

async function sendChatMessage() {
  if (_chatSending) return;
  const input = document.getElementById('chatInput');
  const message = input.value.trim();
  if (!message && !_chatUploadFile) return;

  // If user hits Send while a file is selected — auto-upload it first, then send message
  if (_chatUploadFile) {
    const file = _chatUploadFile;
    _chatUploadFile = null;
    document.getElementById('chatFileInput').value = '';
    document.getElementById('chatUploadStrip').style.display = 'none';
    const manualId = await _doUpload(file);
    if (manualId) {
      _activeManualId = manualId;
      _pollManualStatus(manualId, file.name);  // start background status polling
    }
    if (!message) return;   // file-only send: just upload, no message yet
  }

  if (!message) return;

  // Create session on first message if none exists
  if (!_chatSessionId) {
    const machineId = document.getElementById('chat-machine-filter')?.value || null;
    try {
      const session = await api('/api/chat/sessions', {
        method: 'POST',
        body: JSON.stringify({ machine_id: machineId || null, title: message.slice(0, 60) }),
      });
      if (!session) return;
      _chatSessionId = session.session_id;
    } catch(e) {
      alert('Could not start session: ' + e.message);
      return;
    }
  }

  const welcome = document.getElementById('chatWelcome');
  if (welcome) welcome.style.display = 'none';

  appendMessage('user', message, false);
  input.value = '';
  input.style.height = 'auto';
  scrollChatToBottom();

  _chatSending = true;
  document.getElementById('chatSendBtn').disabled = true;
  document.getElementById('chatTyping').style.display = 'flex';

  const machineId = document.getElementById('chat-machine-filter')?.value || null;

  try {
    const resp = await api(`/api/chat/sessions/${_chatSessionId}/message`, {
      method: 'POST',
      body: JSON.stringify({
        message,
        machine_id: machineId || null,
        manual_id: _activeManualId || null,
      }),
    });
    if (resp) {
      appendMessage('assistant', resp.content, resp.has_checklist);
      if (resp.rag_used) {
        document.getElementById('chatRagBadge').style.display = 'inline-block';
      }
      await loadChatSessions();
    }
  } catch(e) {
    appendMessage('assistant', `Error: ${e.message}`, false);
  } finally {
    _chatSending = false;
    document.getElementById('chatSendBtn').disabled = false;
    document.getElementById('chatTyping').style.display = 'none';
    scrollChatToBottom();
  }
}

function sendSuggestion(text) {
  const input = document.getElementById('chatInput');
  input.value = text;
  sendChatMessage();
}

// ── In-chat manual upload ─────────────────────────────────────────────────────
let _chatUploadFile = null;
let _activeManualId = null;   // manual_id of the last successfully processed upload — sent with every chat message

function onChatFileSelected(e) {
  const file = e.target.files[0];
  if (!file) return;
  _chatUploadFile = file;
  document.getElementById('chatUploadFileName').textContent = `📄 ${file.name} (${(file.size/1024).toFixed(0)} KB)`;
  document.getElementById('chatUploadStrip').style.display = 'flex';
}

function cancelChatUpload() {
  _chatUploadFile = null;
  document.getElementById('chatFileInput').value = '';
  document.getElementById('chatUploadStrip').style.display = 'none';
  document.getElementById('chatUploadProgress').style.display = 'none';
}

/**
 * Upload the pending file and return the manual_id on success, or null on failure.
 * Shows progress in the upload progress bar (does NOT show the chat strip — caller hides it).
 */
async function _doUpload(file) {
  const progress = document.getElementById('chatUploadProgress');
  progress.style.cssText = 'display:block;background:#D5F5E3;border:1px solid #A9DFBF;border-radius:6px;padding:8px 12px;margin-bottom:8px;font-size:12px;color:#1E8449';
  progress.textContent = `⏳ Uploading ${file.name}… (${(file.size/1024/1024).toFixed(1)} MB)`;

  const token = await getFreshToken();
  const form = new FormData();
  form.append('file', file);

  try {
    const res = await fetch(API + '/api/manual/upload', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${token}` },
      body: form,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      progress.style.background = '#FDEDEC';
      progress.style.borderColor = '#F1948A';
      progress.style.color = '#922B21';
      progress.textContent = `Upload failed: ${err.detail || res.statusText}`;
      return null;
    }
    const data = await res.json();
    progress.textContent = `⏳ "${file.name}" uploaded — starting RAG pipeline (classifying, chunking, embedding)…`;
    // Do NOT hide here — _pollManualStatus will manage the progress bar
    return data.manual_id;
  } catch(e) {
    progress.style.background = '#FDEDEC';
    progress.style.color = '#922B21';
    progress.textContent = `Upload error: ${e.message}`;
    return null;
  }
}

/** "Upload & Process" button — upload without sending a message */
async function submitChatUpload() {
  if (!_chatUploadFile) return;
  const file = _chatUploadFile;
  _chatUploadFile = null;
  document.getElementById('chatFileInput').value = '';
  document.getElementById('chatUploadStrip').style.display = 'none';

  const manualId = await _doUpload(file);
  if (manualId) {
    _activeManualId = manualId;
    appendMessage('assistant',
      `**Manual uploaded:** ${file.name}\n\nRAG pipeline is now running — classifying, chunking, and embedding the document (1–3 minutes).\n\nI will notify you when it's ready. While waiting you can already ask questions — I'll answer from the document as soon as indexing completes.`,
      false
    );
    _pollManualStatus(manualId, file.name);
  }
}

/** Poll the status endpoint every 10s until the pipeline is done, then notify the user */
async function _pollManualStatus(manualId, filename) {
  const maxAttempts = 30;  // 30 × 10s = 5 minutes max
  let attempt = 0;
  const progress = document.getElementById('chatUploadProgress');

  const tick = async () => {
    attempt++;
    try {
      const status = await api(`/api/manual/uploads/${manualId}/status`);
      if (!status) return;

      if (status.ready) {
        _activeManualId = manualId;
        progress.style.cssText = 'display:block;background:#D5F5E3;border:1px solid #82E0AA;border-radius:6px;padding:8px 12px;margin-bottom:8px;font-size:12px;color:#1E8449';
        progress.textContent = `✅ "${filename}" is ready! ${status.task_count} tasks extracted. Ask me anything from this manual.`;
        appendMessage('assistant',
          `**"${filename}" is fully processed and ready!**\n\n${status.task_count > 0 ? `${status.task_count} maintenance tasks were extracted.` : ''}\n\nYou can now ask:\n- *"What are the 8hr daily checks?"*\n- *"Generate a complete checklist for this machine"*\n- *"What does the manual say about lubrication?"*\n- *"List all safety procedures"*\n- *"Generate the checklist as an Excel sheet"*`,
          false
        );
        setTimeout(() => { progress.style.display = 'none'; }, 15000);
        return;  // done polling
      }

      if (status.status === 'FAILED') {
        progress.style.cssText = 'display:block;background:#FDEDEC;border:1px solid #F1948A;border-radius:6px;padding:8px 12px;margin-bottom:8px;font-size:12px;color:#922B21';
        progress.textContent = `❌ Processing failed: ${status.error || 'Unknown error'}. Try re-uploading.`;
        return;
      }

      // Still running — update progress bar
      progress.style.cssText = 'display:block;background:#EBF5FB;border:1px solid #AED6F1;border-radius:6px;padding:8px 12px;margin-bottom:8px;font-size:12px;color:#1A5276';
      progress.textContent = `⏳ ${status.label} (${status.progress}%)`;

      if (attempt < maxAttempts) {
        setTimeout(tick, 10000);
      } else {
        progress.textContent = '⚠️ Processing is taking longer than expected. You can still try asking questions.';
      }
    } catch(e) {
      if (attempt < maxAttempts) setTimeout(tick, 15000);
    }
  };

  setTimeout(tick, 8000);  // first check after 8s
}

function appendMessage(role, content, hasChecklist) {
  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = `chat-message ${role === 'user' ? 'chat-message-user' : 'chat-message-assistant'}`;

  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble';
  bubble.innerHTML = renderMarkdown(content);

  div.appendChild(bubble);

  if (role === 'assistant' && hasChecklist) {
    const actions = document.createElement('div');
    actions.className = 'chat-message-actions';
    actions.innerHTML = `
      <button class="btn btn-outline btn-sm" onclick="copyChecklistText(this)">📋 Copy Checklist</button>
      <button class="btn btn-success btn-sm" onclick="downloadChecklist(this)">⬇ Download as PDF</button>
    `;
    actions.dataset.content = content;
    div.appendChild(actions);
  }

  container.appendChild(div);
}

function clearChatMessages() {
  const container = document.getElementById('chatMessages');
  container.innerHTML = `
    <div class="chat-welcome" id="chatWelcome">
      <div style="font-size:32px;margin-bottom:12px">💬</div>
      <h3 style="margin:0 0 8px;color:var(--navy)">PM Maintenance Assistant</h3>
      <p style="color:var(--grey-500);font-size:13px;max-width:400px;margin:0 auto 20px">
        Ask about maintenance procedures, generate checklists, check safety requirements, or get PM schedule advice.
      </p>
      <div class="chat-suggestions">
        <button class="chat-suggestion" onclick="sendSuggestion('Generate the 240hr PM checklist for the bottle coder as an Excel sheet')">Generate 240hr Bottle Coder PM as Excel</button>
                <button class="chat-suggestion" onclick="sendSuggestion('Give me a PDF of the 8hr PM for the dehumidifier')">PDF of 8hr Dehumidifier PM</button>
        <button class="chat-suggestion" onclick="sendSuggestion('What LOTO steps are required before servicing the CONTIFORM-C3-L3?')">LOTO steps for CONTIFORM-C3-L3</button>
        <button class="chat-suggestion" onclick="sendSuggestion('What maintenance tasks are overdue on Line 3?')">What PMs are overdue?</button>
        <button class="chat-suggestion" onclick="sendSuggestion('List all lubrication tasks for Krones machines at 1500hr interval')">Lubrication tasks at 1500hr</button>
      </div>
    </div>`;
  document.getElementById('chatRagBadge').style.display = 'none';
}

function clearChat() {
  _chatSessionId = null;
  clearChatMessages();
  document.getElementById('chatTitle').textContent = 'PM Assistant';
  document.getElementById('chatSubtitle').textContent = 'Ask anything about preventive maintenance';
}

function scrollChatToBottom() {
  const c = document.getElementById('chatMessages');
  if (c) c.scrollTop = c.scrollHeight;
}

function chatKeyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChatMessage();
  }
}

function autoResizeTextarea(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function copyChecklistText(btn) {
  const content = btn.closest('.chat-message-actions').dataset.content;
  navigator.clipboard.writeText(content).then(() => {
    btn.textContent = '✅ Copied!';
    setTimeout(() => { btn.textContent = '📋 Copy Checklist'; }, 2000);
  });
}

function downloadChecklist(btn) {
  const content = btn.closest('.chat-message-actions').dataset.content;
  const blob = new Blob([content], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `pm-checklist-${new Date().toISOString().slice(0,10)}.txt`;
  a.click();
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function renderMarkdown(text) {
  if (!text) return '';
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    // Code blocks
    .replace(/```[\s\S]*?```/g, m => `<pre><code>${m.slice(3, -3).replace(/^[^\n]*\n?/, '')}</code></pre>`)
    // Inline code
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    // Links — generated-document download links render as a green button (with auth token attached)
    .replace(/\[([^\]]+)\]\((\/api\/download\/[^)]+)\)/g,
      (_, label, url) => `<a href="${makeDownloadUrl(url)}" target="_blank" rel="noopener" class="btn btn-success btn-sm" style="display:inline-block;margin-top:6px;text-decoration:none">${label}</a>`)
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)]+|\/[^)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>')
    // Bold
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    // Headings
    .replace(/^#{1,3}\s+(.+)$/gm, (_, h) => `<strong style="display:block;margin:10px 0 4px;color:var(--navy)">${h}</strong>`)
    // Numbered lists
    .replace(/^(\d+)\.\s+(.+)$/gm, (_, n, item) => {
      const safety = item.includes('⚠') ? ' style="background:#FFF0F0;border-left:3px solid var(--red);padding-left:8px"' : '';
      return `<div class="chat-list-item"${safety}><span class="chat-list-num">${n}.</span>${item}</div>`;
    })
    // Bullet lists
    .replace(/^[-*]\s+(.+)$/gm, (_, item) => {
      const safety = item.includes('⚠') ? ' style="background:#FFF0F0;border-left:3px solid var(--red);padding-left:8px"' : '';
      return `<div class="chat-list-item"${safety}><span class="chat-list-bullet">•</span>${item}</div>`;
    })
    // Horizontal rules
    .replace(/^---$/gm, '<hr style="border:none;border-top:1px solid var(--grey-300);margin:8px 0">')
    // Paragraphs
    .replace(/\n\n/g, '<br/><br/>')
    .replace(/\n/g, '<br/>');
}
