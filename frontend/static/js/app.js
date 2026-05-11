/* ── PM Automation System — Dashboard JS ── */

const API = '';
let currentUser = null;
let histOffset = 0;
const histLimit = 20;
let histTotal = 0;

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
  const roleEl = document.getElementById('userRole');
  const avatarEl = document.getElementById('userAvatar');
  if (nameEl) nameEl.textContent = u.name || u.email;
  if (roleEl) roleEl.textContent = u.role;
  if (avatarEl) avatarEl.textContent = (u.name || u.email || '?')[0].toUpperCase();
}

function applyRoleUI() {
  if (!currentUser) return;
  const role = currentUser.role;
  // Hide upload nav for non-engineers/managers
  if (!['Manager', 'Engineer'].includes(role)) {
    const el = document.getElementById('nav-upload');
    if (el) el.style.display = 'none';
    const sec = document.getElementById('nav-section-upload');
    if (sec) sec.style.display = 'none';
  }
  // Hide export nav for non-managers
  if (role !== 'Manager') {
    const el = document.getElementById('nav-export');
    if (el) el.style.display = 'none';
  }
  // Hide manager section if technician/supervisor
  if (!['Manager', 'Engineer'].includes(role)) {
    const sec = document.getElementById('nav-section-manager');
    if (sec) sec.style.display = 'none';
  }
  // Add machine button for non-managers/engineers
  const addBtn = document.getElementById('addMachineBtn');
  if (addBtn && !['Manager', 'Engineer'].includes(role)) {
    addBtn.style.display = 'none';
  }
}

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
  };
  document.getElementById('pageTitle').textContent = titles[name] || name;

  // Lazy-load page data
  if (name === 'history') loadHistory();
  if (name === 'library') loadLibrary();
  if (name === 'machines') loadMachines();
  if (name === 'upload') loadUploads();
}

// ─── API helper ───────────────────────────────────────────────────────────────

async function api(path, opts = {}) {
  const token = localStorage.getItem('pm_token');
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(API + path, { ...opts, headers });
  if (res.status === 401) { logout(); return null; }
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
    link.href = data.download_url;
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
      tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state"><div class="empty-icon">🕐</div><p>No PM records found</p></div></td></tr>`;
      return;
    }

    tbody.innerHTML = data.records.map(r => `
      <tr>
        <td>${new Date(r.created_at).toLocaleString()}</td>
        <td><strong>${r.machine_name}</strong></td>
        <td>${r.interval_label}</td>
        <td><code>${r.work_order}</code></td>
        <td>${r.technician_name}</td>
        <td>${statusBadge(r.status)}</td>
        <td>
          ${r.download_url ? `<a href="${r.download_url}" target="_blank" class="btn btn-primary btn-sm">⬇ Download</a>` : ''}
          ${(r.status === 'COMPLETED' && canApprove()) ? `<button class="btn btn-success btn-sm" style="margin-left:4px" onclick="approveRecord('${r.record_id}')">✓ Approve</button>` : ''}
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

async function submitUpload(e) {
  e.preventDefault();
  const btn = document.getElementById('uploadBtn');
  const alert = document.getElementById('uploadAlert');
  btn.disabled = true;
  btn.textContent = '⬆️ Uploading...';
  alert.innerHTML = '';

  const file = document.getElementById('upload-file').files[0];
  const machineId = document.getElementById('upload-machine').value;

  const formData = new FormData();
  formData.append('file', file);
  if (machineId) formData.append('machine_id', machineId);

  const token = localStorage.getItem('pm_token');
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
    alert.innerHTML = `
      <div class="alert alert-success">
        ✅ Manual uploaded! Manual ID: <code>${data.manual_id}</code><br/>
        RAG pipeline is running in background. Check the queue below for status.
      </div>`;
    document.getElementById('uploadForm').reset();
    setTimeout(loadUploads, 2000);
  } catch(err) {
    alert.innerHTML = `<div class="alert alert-danger">❌ ${err.message}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '⬆️ Upload & Process';
  }
}

async function loadUploads() {
  const tbody = document.getElementById('uploadQueueBody');
  try {
    const uploads = await api('/api/manual/uploads');
    if (!uploads || uploads.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state"><p>No uploads yet</p></div></td></tr>`;
      return;
    }
    tbody.innerHTML = uploads.map(u => `
      <tr>
        <td>${u.filename}</td>
        <td>${u.machine_id || '—'}</td>
        <td>${pipelineBadge(u.status)}</td>
        <td>${u.detected_manufacturer || '—'}</td>
        <td>${u.task_count}</td>
        <td>${u.uploaded_by || '—'}</td>
        <td>
          ${u.status === 'PENDING_REVIEW' ?
            `<button class="btn btn-success btn-sm" onclick="approveManual('${u.manual_id}','${u.machine_id}')">✓ Approve</button>` :
            `<button class="btn btn-outline btn-sm" onclick="viewUpload('${u.manual_id}')">View</button>`}
        </td>
      </tr>`).join('');
  } catch(e) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="alert alert-danger">${e.message}</div></td></tr>`;
  }
}

async function approveManual(manualId, machineId) {
  const targetMachine = prompt('Machine ID to add tasks to:', machineId || '');
  if (!targetMachine) return;
  try {
    const formData = new FormData();
    formData.append('machine_id', targetMachine);
    const token = localStorage.getItem('pm_token');
    const res = await fetch(API + `/api/manual/uploads/${manualId}/approve`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${token}` },
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);
    alert(`✅ Approved! ${data.tasks_added_to_library} tasks added to PM Library for ${targetMachine}`);
    loadUploads();
  } catch(e) {
    alert('Approval failed: ' + e.message);
  }
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
