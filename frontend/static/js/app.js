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
  if (name === 'export') loadExportPage();
  if (name === 'library') loadLibrary();
  if (name === 'machines') loadMachines();
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

  // Load pending-review manuals separately — show as a CTA banner on dashboard
  loadPendingReviews();
}

async function loadPendingReviews() {
  try {
    const uploads = await api('/api/manual/uploads');
    if (!uploads) return;
    const pending = uploads.filter(u => u.status === 'PENDING_REVIEW');
    const card = document.getElementById('pendingReviewsCard');
    const list = document.getElementById('pendingReviewsList');
    const badge = document.getElementById('pendingReviewBadge');
    if (!card || !list) return;
    if (pending.length === 0) { card.style.display = 'none'; return; }
    card.style.display = 'block';
    badge.textContent = pending.length;
    list.innerHTML = pending.map(u => `
      <div style="padding:12px 0;border-bottom:1px solid var(--grey-200)">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
          <div style="flex:1">
            <div style="font-size:13px;font-weight:700;color:var(--navy)">${u.detected_manufacturer || 'Unknown'}</div>
            <div style="font-size:11px;color:var(--grey-500);margin-top:1px">${u.filename || '—'} · ${u.task_count} task${u.task_count!==1?'s':''} · ${u.uploaded_by || '?'} · ${new Date(u.created_at).toLocaleDateString()}</div>
          </div>
          <a href="/frontend/review.html?id=${u.manual_id}"
             style="padding:7px 16px;background:#1B3A6B;color:#fff;border-radius:6px;text-decoration:none;font-size:12px;font-weight:700;white-space:nowrap;flex-shrink:0">
            📋 Review →
          </a>
        </div>
        ${u.task_count > 0
          ? `<div style="font-size:10.5px;font-weight:600;color:#64748B;margin-bottom:4px;text-transform:uppercase;letter-spacing:.4px">PM Intervals awaiting approval</div>
             <div style="display:flex;flex-wrap:wrap;gap:6px">
               <a href="/frontend/review.html?id=${u.manual_id}" style="display:inline-flex;align-items:center;padding:4px 12px;background:#EFF6FF;border:1.5px solid #BFDBFE;border-radius:6px;font-size:11.5px;font-weight:700;color:#1B3A6B;text-decoration:none">All ${u.task_count} tasks →</a>
             </div>`
          : `<div style="font-size:11px;color:#F59E0B;background:#FFFBEB;border:1px solid #FDE68A;border-radius:5px;padding:5px 10px">⚠ 0 tasks extracted — AI could not parse this PDF</div>`
        }
      </div>`).join('');
  } catch(e) {}
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
  // Show the hidden generate page (used from dashboard schedule table)
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const genPage = document.getElementById('page-generate');
  genPage.style.display = '';
  genPage.classList.add('active');
  document.getElementById('pageTitle').textContent = 'Generate PM Checklist';

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

    document.getElementById('histSelectAll').checked = false;
    document.getElementById('histDeleteBtn').style.display = 'none';
    tbody.innerHTML = data.records.map(r => `
      <tr>
        <td><input type="checkbox" class="hist-check" value="${r.record_id}" onchange="updateDeleteBtn('historyBody','histDeleteBtn')" /></td>
        <td style="font-size:11px;color:var(--grey-500)">${new Date(r.created_at).toLocaleString()}</td>
        <td><strong>${r.machine_name}</strong></td>
        <td>${r.interval_label}</td>
        <td><code style="font-size:11px">${r.work_order}</code></td>
        <td>${r.technician_name}</td>
        <td>${statusBadge(r.status)}</td>
        <td>
          ${r.download_url
            ? `<a href="${makeDownloadUrl(r.download_url)}" target="_blank" class="btn btn-primary btn-sm">⬇ Download</a>`
            : '—'}
          ${(r.status === 'COMPLETED' && canApprove())
            ? `<button class="btn btn-success btn-sm" style="margin-left:4px" onclick="approveRecord('${r.record_id}')">✓ Approve</button>`
            : ''}
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
let _pipelineStartTime = null;
let _elapsedTimer = null;

const PIPELINE_STEPS = [
  { key: 'UPLOADED',       label: 'Uploaded',               icon: '📤', est: null },
  { key: 'CLASSIFYING',    label: 'Classifying manufacturer', icon: '🔍', est: '~5s' },
  { key: 'CHUNKING',       label: 'Smart chunking',          icon: '✂️', est: '~10s' },
  { key: 'EMBEDDING',      label: 'Embedding chunks (watsonx)', icon: '🧠', est: '1-3 min' },
  { key: 'EXTRACTING',     label: 'Extracting PM tasks',     icon: '⚙️', est: '~30s' },
  { key: 'PENDING_REVIEW', label: 'Ready — awaiting review', icon: '✅', est: null },
  { key: 'APPROVED',       label: 'Approved',                icon: '🎉', est: null },
];

function renderPipelineSteps(currentStatus) {
  const currentIdx = PIPELINE_STEPS.findIndex(s => s.key === currentStatus);
  const isFailed = currentStatus === 'FAILED';
  const pct = isFailed ? 0 : Math.round((Math.max(currentIdx, 0) / (PIPELINE_STEPS.length - 1)) * 100);

  const stepRows = PIPELINE_STEPS.map((step, i) => {
    let state = 'pending';
    if (i < currentIdx) state = 'done';
    else if (i === currentIdx) state = isFailed ? 'failed' : 'active';
    const textColor = state === 'pending' ? 'var(--grey-400)' : state === 'done' ? 'var(--grey-700)' : 'var(--text)';
    const isTerminal = step.key === 'PENDING_REVIEW' || step.key === 'APPROVED';
    const icon = state === 'done' ? '✅'
               : state === 'active' ? (isTerminal ? '✅' : `<span class="spinner" style="width:13px;height:13px;border-width:2px;display:inline-block;vertical-align:middle"></span>`)
               : state === 'failed' ? '❌'
               : '⬜';
    const badge = state === 'active'
      ? (isTerminal
          ? `<span class="badge badge-green" style="margin-left:auto;font-size:10px">READY</span>`
          : `<span class="badge badge-amber" style="margin-left:auto;font-size:10px">${step.est ? 'est. ' + step.est : 'RUNNING'}</span>`)
      : state === 'done' ? `<span style="margin-left:auto;font-size:11px;color:var(--green)">✓</span>` : '';
    return `<div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--grey-200)">
      <span style="min-width:18px;text-align:center">${icon}</span>
      <span style="font-size:13px;color:${textColor};font-weight:${state === 'active' ? '700' : '400'}">${step.icon} ${step.label}</span>
      ${badge}
    </div>`;
  }).join('');

  const bar = `<div style="margin-top:12px">
    <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--grey-500);margin-bottom:4px">
      <span id="pipelineElapsed">Elapsed: 0s</span>
      <span>${pct}% complete</span>
    </div>
    <div style="background:var(--grey-200);border-radius:4px;height:6px;overflow:hidden">
      <div style="background:${isFailed ? 'var(--red)' : 'var(--navy)'};height:100%;width:${pct}%;transition:width 0.4s ease;border-radius:4px"></div>
    </div>
  </div>`;

  return stepRows + bar;
}

function _startElapsedTimer() {
  if (_elapsedTimer) clearInterval(_elapsedTimer);
  _pipelineStartTime = Date.now();
  _elapsedTimer = setInterval(() => {
    const el = document.getElementById('pipelineElapsed');
    if (!el) return;
    const secs = Math.floor((Date.now() - _pipelineStartTime) / 1000);
    el.textContent = secs < 60 ? `Elapsed: ${secs}s` : `Elapsed: ${Math.floor(secs/60)}m ${secs%60}s`;
  }, 1000);
}

function _stopElapsedTimer() {
  if (_elapsedTimer) { clearInterval(_elapsedTimer); _elapsedTimer = null; }
}

async function submitUpload(e) {
  e.preventDefault();
  const btn = document.getElementById('uploadBtn');
  const alertEl = document.getElementById('uploadAlert');
  btn.disabled = true;
  btn.textContent = '⬆ Uploading...';

  // Immediately show upload status in Step 2 — don't wait for HTTP response
  const stepsEl = document.getElementById('pipelineSteps');
  const badge = document.getElementById('pipelineStatusBadge');
  if (stepsEl) stepsEl.innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;padding:16px 0">
      <span class="spinner" style="width:20px;height:20px;flex-shrink:0"></span>
      <div>
        <div style="font-size:13px;font-weight:700;color:var(--text)">Uploading PDF to server...</div>
        <div style="font-size:12px;color:var(--grey-500);margin-top:3px">File transfer in progress</div>
      </div>
    </div>`;
  if (badge) { badge.className = 'badge badge-amber'; badge.textContent = 'UPLOADING'; }
  _startElapsedTimer();
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
    // pipelineProgress is always visible on the upload page
    document.getElementById('uploadForm').reset();
    btn.disabled = false;
    btn.textContent = '⬆ Upload & Start AI Processing';

    // Start polling (elapsed timer already started before the fetch)
    _startPipelinePoller(_currentManualId);
  } catch(err) {
    _stopElapsedTimer();
    const stepsElErr = document.getElementById('pipelineSteps');
    const badgeErr = document.getElementById('pipelineStatusBadge');
    if (stepsElErr) stepsElErr.innerHTML = `<div style="padding:16px 0;color:var(--red);font-size:13px">❌ Upload failed — ${err.message}</div>`;
    if (badgeErr) { badgeErr.className = 'badge badge-red'; badgeErr.textContent = 'FAILED'; }
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
    if (['APPROVED', 'FAILED', 'PENDING_REVIEW'].includes(data.status)) {
      clearInterval(_pipelinePoller);
      _stopElapsedTimer();
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

  // When pipeline completes — show interval buttons for the review flow
  if ((data.status === 'PENDING_REVIEW' || data.status === 'APPROVED') && reviewCard) {
    const tasks      = data.extracted_tasks || [];
    const taskCount  = tasks.length || data.extracted_task_count || 0;
    const mfr        = data.detected_manufacturer || 'Unknown';
    const isApproved = data.status === 'APPROVED';
    reviewCard.style.display = 'block';

    if (taskCount === 0) {
      // Zero tasks — show helpful error state, not a broken ZIP button
      reviewCard.innerHTML = `
        <div class="card-header">
          <h3 class="card-title" style="color:var(--amber)">⚠️ Step 3 — No Tasks Extracted</h3>
          <span class="badge badge-amber">0 TASKS</span>
        </div>
        <div style="background:#FFFBEB;border:1.5px solid #FDE68A;border-radius:8px;padding:14px 16px;margin-bottom:12px">
          <div style="font-size:12.5px;font-weight:700;color:#92400E;margin-bottom:5px">
            AI extraction returned 0 tasks for <strong>${mfr}</strong>
          </div>
          <div style="font-size:11.5px;color:#78350F;line-height:1.6">
            This can happen when the PDF is image-based (not selectable text), when the maintenance schedule is embedded in diagrams, or when the table format is not recognised.<br/>
            <strong>Try:</strong> uploading a text-searchable PDF, or a different chapter/section of the manual.
          </div>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-outline" style="flex:1;font-size:12px"
            onclick="document.getElementById('pdfFile').click()">↑ Upload Different PDF</button>
        </div>`;
      return;
    }

    // Show source badge based on task origins
    const hasLibrary = tasks.some(t => t._source === 'pm_library');
    const hasAI = tasks.some(t => t._source === 'ai_extracted' || !t._source);
    const fromLibrary = hasLibrary && !hasAI;  // pure fallback
    const fromMixed = hasLibrary && hasAI;      // AI + PM Library supplement
    const libraryBadge = fromLibrary
      ? `<span style="background:#EEF2FF;color:#4338CA;border:1px solid #C7D2FE;border-radius:4px;padding:2px 8px;font-size:11px;font-weight:600;margin-left:8px">📚 PM Library</span>`
      : fromMixed
      ? `<span style="background:#F0FDF4;color:#166534;border:1px solid #BBF7D0;border-radius:4px;padding:2px 8px;font-size:11px;font-weight:600;margin-left:8px">🤖+📚 AI + Library</span>`
      : '';

    // Build interval list from extracted_tasks (or fall back if tasks array not in poll data)
    const ivLabel = h => {
      const m = {8:'8hr / Daily',120:'2-Week',240:'Monthly Prep',500:'500hr / Monthly',
        1000:'1000hr',1500:'1500hr / Qtrly',2000:'2000hr',3000:'3000hr / 6-Month',
        4000:'4000hr',6000:'6000hr / Annual',42000:'42000hr / 7yr'};
      return m[h] || `${h}hr`;
    };

    const intervals = [...new Set(tasks.map(t => t.interval_hours).filter(Boolean))].sort((a,b)=>a-b);
    const intervalBtns = intervals.length
      ? intervals.map(h => {
          const cnt = tasks.filter(t => t.interval_hours === h).length;
          return `<a href="/frontend/review.html?id=${data.manual_id}&interval=${h}"
            style="display:flex;flex-direction:column;align-items:center;gap:4px;
                   flex:1;min-width:80px;padding:12px 8px;
                   background:#fff;border:2px solid #CBD5E1;border-radius:8px;
                   text-decoration:none;transition:all .15s;cursor:pointer"
            onmouseover="this.style.borderColor='#1B3A6B';this.style.background='#EFF6FF'"
            onmouseout="this.style.borderColor='#CBD5E1';this.style.background='#fff'">
              <span style="font-size:13px;font-weight:800;color:#1B3A6B">${ivLabel(h)}</span>
              <span style="font-size:10px;font-weight:600;color:#64748B">${cnt} task${cnt!==1?'s':''}</span>
            </a>`;
        }).join('')
      : `<div style="font-size:11.5px;color:#64748B;padding:12px">Interval data not available — open full review page.</div>`;

    reviewCard.innerHTML = `
      <div class="card-header">
        <h3 class="card-title" style="color:${isApproved ? 'var(--green)' : 'var(--navy)'}">
          ${isApproved ? '✅ Step 3 — Approved' : '✅ Step 3 — Review &amp; Approve'}
        </h3>
        <span class="badge ${isApproved ? 'badge-green' : 'badge-blue'}">${taskCount} tasks</span>
        ${libraryBadge}
      </div>

      <div style="font-size:11.5px;color:#475569;margin-bottom:10px">
        <strong>${mfr}</strong> — ${taskCount} tasks across <strong>${intervals.length} PM interval${intervals.length!==1?'s':''}</strong>
        ${fromLibrary ? ' · <span style="color:#4338CA;font-weight:600">Loaded from PM Library</span>' : fromMixed ? ' · <span style="color:#166534;font-weight:600">AI extracted + PM Library supplemented</span>' : ''}
        ${isApproved ? ` · Approved by <strong>${data.approved_by || 'engineer'}</strong>` : ' · Select an interval to review and approve:'}
      </div>

      <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px">
        ${intervalBtns}
      </div>

      <a href="/frontend/review.html?id=${data.manual_id}"
         style="display:flex;align-items:center;justify-content:center;gap:8px;
                width:100%;padding:11px;background:#1B3A6B;color:#fff;border-radius:8px;
                text-decoration:none;font-size:13px;font-weight:700;letter-spacing:.1px;
                box-shadow:0 2px 8px rgba(27,58,107,.2);transition:background .15s;margin-bottom:8px"
         onmouseover="this.style.background='#0F2549'" onmouseout="this.style.background='#1B3A6B'">
        📋 Open Full Review — All ${taskCount} Tasks →
      </a>

      ${isApproved ? `
      <div style="display:flex;gap:8px">
        <button class="btn btn-outline" style="flex:1;font-size:12px"
          onclick="generateZip()">⬇ Download ZIP</button>
        <button class="btn btn-outline" style="flex:1;font-size:12px"
          onclick="generateZip()">📊 Download Excel</button>
      </div>` : ''}
      <div id="zipAlert" style="margin-top:8px"></div>`;
  }

  if (data.status === 'APPROVED' && generateCard) {
    generateCard.style.display = 'block';
    const s = document.getElementById('approvalSuccess');
    if (s) s.innerHTML = `✅ <strong>${data.extracted_task_count || 'New'} tasks added to PM Library for ${data.machine_id || 'the machine'}.</strong> You can now generate PM documents using these tasks.`;
    loadIntervalButtons(data.machine_id);
  }
}

function loadIntervalButtons(machineId) {
  if (!machineId) return;
  // Populate the inline interval dropdown in generateCard
  api('/api/library').then(lib => {
    if (!lib) return;
    const machine = lib.machines.find(m => m.machine_id === machineId);
    if (!machine) return;
    const sel = document.getElementById('gen2-interval');
    if (sel) {
      sel.innerHTML = '<option value="">Select interval...</option>';
      machine.intervals.filter(iv => iv.task_count > 0).forEach(iv => {
        const opt = document.createElement('option');
        opt.value = iv.hours;
        opt.textContent = `${iv.label} (${iv.natural_label}) — ${iv.task_count} tasks`;
        sel.appendChild(opt);
      });
    }
    const hiddenMach = document.getElementById('gen2-machine');
    if (hiddenMach) hiddenMach.value = machineId;
  }).catch(() => null);
}

async function submitGenerate2(e) {
  e.preventDefault();
  const btn = document.getElementById('gen2Btn');
  const alertEl = document.getElementById('genAlert2');
  const result = document.getElementById('genResult2');
  result.style.display = 'none';
  alertEl.innerHTML = '';

  btn.disabled = true;
  btn.textContent = '⏳ Generating...';
  showLoading('Generating Excel checklist — please wait...');

  const payload = {
    machine_id: document.getElementById('gen2-machine').value,
    interval_hours: parseInt(document.getElementById('gen2-interval').value),
    work_order: document.getElementById('gen2-wo').value,
    technician_name: document.getElementById('gen2-tech').value,
    output_format: document.getElementById('gen2-format').value,
  };

  try {
    const data = await api('/api/generate', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (!data) throw new Error('No response from server');

    hideLoading();
    btn.disabled = false;
    btn.textContent = '⬇ Generate & Download Excel';

    result.style.display = 'block';
    document.getElementById('genSuccess2').innerHTML = `
      ✅ <strong>Generated successfully!</strong> &nbsp;${data.task_count} tasks · ${(data.file_size_bytes / 1024).toFixed(1)} KB
    `;
    const link = document.getElementById('genDownloadLink2');
    link.href = makeDownloadUrl(data.download_url);
    link.textContent = `⬇ Download ${data.output_format?.toUpperCase() || 'XLSX'}`;
    link.click(); // auto-trigger download
  } catch(err) {
    hideLoading();
    btn.disabled = false;
    btn.textContent = '⬇ Generate & Download Excel';
    alertEl.innerHTML = `<div class="alert alert-danger">❌ ${err.message}</div>`;
  }
}

async function generateZip() {
  if (!_currentManualId) return;
  // machine select may not exist in the new review card — use any available select
  const machineId = (document.getElementById('review-machine-select') || document.getElementById('gen-machine'))?.value || '';
  const btn = document.getElementById('generateZipBtn');
  const alertEl = document.getElementById('zipAlert');
  if (alertEl) alertEl.innerHTML = '';
  btn.disabled = true;
  btn.textContent = '⏳ Generating ZIP...';
  try {
    const token = await getFreshToken();
    const qs = machineId ? `?machine_id=${encodeURIComponent(machineId)}` : '';
    const res = await fetch(`${API}/api/manual/uploads/${_currentManualId}/generate-zip${qs}`, {
      headers: { 'Authorization': `Bearer ${token}` },
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: 'Generation failed' }));
      throw new Error(err.detail || 'Generation failed');
    }
    const blob = await res.blob();
    const cd = res.headers.get('Content-Disposition') || '';
    const filename = cd.match(/filename="([^"]+)"/)?.[1] || 'PM_output.zip';
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    btn.textContent = `✅ Downloaded — ${filename}`;
    btn.disabled = false;
    alertEl.innerHTML = `<div class="alert alert-success" style="font-size:12px">✅ <strong>${filename}</strong> downloaded — check your Downloads folder.</div>`;
  } catch(err) {
    btn.textContent = '⬇ Generate PM Checklists (ZIP)';
    btn.disabled = false;
    alertEl.innerHTML = `<div class="alert alert-danger" style="font-size:12px">❌ ${err.message}</div>`;
  }
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
    document.getElementById('uploadSelectAll').checked = false;
    document.getElementById('uploadDeleteBtn').style.display = 'none';
    tbody.innerHTML = uploads.map(u => `
      <tr>
        <td><input type="checkbox" class="upload-check" value="${u.manual_id}" onchange="updateDeleteBtn('uploadQueueBody','uploadDeleteBtn')" /></td>
        <td style="font-size:12px">${u.filename}</td>
        <td>${u.machine_id || '—'}</td>
        <td>${pipelineBadge(u.status)}</td>
        <td>${u.detected_manufacturer || '—'}</td>
        <td><strong>${u.task_count}</strong></td>
        <td style="font-size:11px">${u.uploaded_by || '—'}</td>
        <td>
          ${u.status === 'PENDING_REVIEW'
            ? `<a class="btn btn-success btn-sm" href="/frontend/review.html?id=${u.manual_id}" style="text-decoration:none">📋 Review →</a>`
            : u.status === 'APPROVED'
              ? `<a class="btn btn-outline btn-sm" href="/frontend/review.html?id=${u.manual_id}" style="text-decoration:none">✅ View</a>`
              : `<button class="btn btn-outline btn-sm" onclick="refreshUploadStatus('${u.manual_id}')">↻ Status</button>`}
        </td>
      </tr>`).join('');
  } catch(e) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="alert alert-danger">${e.message}</div></td></tr>`;
  }
}

async function resumePipelineReview(manualId) {
  _currentManualId = manualId;
  // pipelineProgress is always visible on the upload page
  _pollPipelineStatus(manualId);
}

async function refreshUploadStatus(manualId) {
  _currentManualId = manualId;
  // pipelineProgress is always visible on the upload page
  _startElapsedTimer();
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

function exportHistory(machineId) {
  const mid = machineId !== undefined ? machineId : (document.getElementById('export-machine')?.value || '');
  const url = '/api/export/history/csv' + (mid ? `?machine_id=${mid}` : '');
  _downloadFromApi(url);
}

function exportLibrary() {
  _downloadFromApi('/api/export/library/csv');
}

function exportAuditLog() {
  _downloadFromApi('/api/export/audit-logs/csv');
}

async function loadExportPage() {
  const container = document.getElementById('exportMachineList');
  if (!container) return;
  container.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';

  try {
    // Load machines that have actual PM records
    const [libData, histData] = await Promise.all([
      api('/api/library'),
      api('/api/history?limit=500'),
    ]);
    if (!libData || !histData) return;

    // Build set of machine IDs that have at least one PM record
    const activeIds = new Set(histData.records.map(r => r.machine_id));
    const activeMachines = (libData.machines || []).filter(m => activeIds.has(m.machine_id));

    if (activeMachines.length === 0) {
      container.innerHTML = `<div class="empty-state" style="padding:32px">
        <div class="empty-icon">📂</div>
        <p>No processed PM documents yet.<br/>Upload a manual and generate a checklist first.</p>
      </div>`;
      return;
    }

    container.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px;padding:16px">
      ${activeMachines.map(m => {
        const count = histData.records.filter(r => r.machine_id === m.machine_id).length;
        return `<div style="background:var(--grey-100);border:1px solid var(--grey-300);border-radius:8px;padding:16px">
          <div style="font-weight:700;color:var(--navy);margin-bottom:4px">${m.name}</div>
          <div style="font-size:12px;color:var(--grey-500);margin-bottom:12px">${m.manufacturer || ''} · ${count} document(s)</div>
          <button class="btn btn-primary btn-sm" style="width:100%" onclick="exportHistory('${m.machine_id}')">
            ⬇ Export ${m.name} CSV
          </button>
        </div>`;
      }).join('')}
    </div>`;
  } catch(e) {
    container.innerHTML = `<div class="alert alert-danger" style="margin:16px">${e.message}</div>`;
  }
}


// ─── Select / Delete helpers ─────────────────────────────────────────────────

function toggleSelectAll(tbodyId, checkAllId, btnId) {
  const checked = document.getElementById(checkAllId).checked;
  document.querySelectorAll(`#${tbodyId} input[type="checkbox"]`).forEach(cb => cb.checked = checked);
  document.getElementById(btnId).style.display = checked ? 'inline-block' : 'none';
}

function updateDeleteBtn(tbodyId, btnId) {
  const any = [...document.querySelectorAll(`#${tbodyId} input[type="checkbox"]`)].some(cb => cb.checked);
  document.getElementById(btnId).style.display = any ? 'inline-block' : 'none';
}

async function deleteSelectedHistory() {
  const ids = [...document.querySelectorAll('#historyBody input[type="checkbox"]:checked')].map(cb => cb.value);
  if (!ids.length) return;
  if (!confirm(`Delete ${ids.length} PM record(s)? This cannot be undone.`)) return;
  try {
    await api('/api/history', { method: 'DELETE', body: JSON.stringify(ids) });
    loadHistory();
  } catch(e) { alert('Delete failed: ' + e.message); }
}

async function deleteSelectedUploads() {
  const ids = [...document.querySelectorAll('#uploadQueueBody input[type="checkbox"]:checked')].map(cb => cb.value);
  if (!ids.length) return;
  if (!confirm(`Delete ${ids.length} upload(s)? This cannot be undone.`)) return;
  let failed = 0;
  for (const id of ids) {
    try {
      const token = await getFreshToken();
      await fetch(API + `/api/manual/uploads/${id}`, { method: 'DELETE', headers: { 'Authorization': `Bearer ${token}` } });
    } catch { failed++; }
  }
  if (failed) alert(`${failed} deletion(s) failed.`);
  loadUploads();
}

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
