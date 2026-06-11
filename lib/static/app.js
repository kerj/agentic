let currentFilter = 'all';
let searchQuery = '';
let allJobs = [];
let lastFetch = null;
let selectedRepo = (window.AGENTIC_CFG&&window.AGENTIC_CFG.defaultRepo);
const IS_LOCAL = (window.AGENTIC_CFG&&window.AGENTIC_CFG.isLocal);

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function relTime(iso) {
  if (!iso) return '';
  const d = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (d < 60)    return d + 's ago';
  if (d < 3600)  return Math.floor(d/60) + 'm ago';
  if (d < 86400) return Math.floor(d/3600) + 'h ago';
  return Math.floor(d/86400) + 'd ago';
}

function setFilter(state, btn) {
  currentFilter = state;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderJobs();
}

function buildJobCard(j) {
  const s = j._state || 'pending';
  const sumCls = (s === 'failed') ? 'job-summary failed' : 'job-summary';
  const actions = [];
  const sp = "event.stopPropagation();";
  if (s === 'pending')
    actions.push(`<button class="btn btn-ghost" onclick="${sp}cancelJob('${escHtml(j.id)}')">Cancel</button>`);
  if (s === 'running')
    actions.push(`<button class="btn btn-red" onclick="${sp}abandonJob('${escHtml(j.id)}')">Abandon</button>`);
  const isReviewJob = j.job_type === 'review';
  if (s === 'done') {
    const _rd = _loadDraft(j.id);
    const _rcount = _rd ? (_rd.comments || []).length : 0;
    const _rlabel = _rcount ? `Review (${_rcount})` : 'Review';
    const _rcls   = (_rcount && !(_rd && _rd.submitted)) ? 'btn btn-amber' : 'btn btn-ghost';
    actions.push(`<button class="${_rcls}" onclick="${sp}viewDiff('${escHtml(j.id)}')">${_rlabel}</button>`);
  }
  if (s === 'failed' || s === 'merged')
    actions.push(`<button class="btn btn-ghost" onclick="${sp}viewDiff('${escHtml(j.id)}')">View Diff</button>`);
  if (s === 'done') {
    // Accept Chain only makes sense when there are manual chain children (non-review)
    const hasChainChild = allJobs.some(x => x.parent_request_id === j.id && x._state === 'done' && x.job_type !== 'review');
    if (hasChainChild)
      actions.push(`<button class="btn btn-blue" onclick="${sp}acceptChain('${escHtml(j.id)}')">Accept Chain ↓</button>`);
    actions.push(`<button class="btn btn-ghost" onclick="${sp}reviewJob('${escHtml(j.id)}')">Review in IDE</button>`);
    actions.push(`<button class="btn btn-blue" style="opacity:.7" onclick="${sp}acceptJob('${escHtml(j.id)}')">Accept</button>`);
  }
  if (s === 'done' || s === 'failed')
    actions.push(`<button class="btn btn-red" onclick="${sp}rejectJob('${escHtml(j.id)}')">Reject</button>`);

  // Status + chain menu — always present
  const allStatuses = ['pending','running','done','merged','failed','abandoned','cancelled'];
  const statusItems = allStatuses.filter(x => x !== s).map(st =>
    `<button onclick="event.stopPropagation();setStatus('${escHtml(j.id)}','${st}')">→ ${st}</button>`
  ).join('');
  const chainLabel = j.parent_request_id ? '🔗 Edit chain…' : '🔗 Set chain…';
  const deleteItem = s !== 'running'
    ? `<div class="divider"></div><button class="menu-danger" onclick="event.stopPropagation();deleteJob('${escHtml(j.id)}')">🗑 Delete…</button>`
    : '';
  const menuItems = `<button onclick="event.stopPropagation();openChain('${escHtml(j.id)}')">${chainLabel}</button><div class="divider"></div>${statusItems}${deleteItem}`;
  actions.push(`<div class="status-menu">
    <button class="status-menu-btn" onclick="event.stopPropagation();toggleMenu(this)">⋯</button>
    <div class="status-dropdown">${menuItems}</div>
  </div>`);

  return `
<div class="job-card">
  <div class="state-dot dot-${escHtml(s)}"></div>
  <div class="job-body" onclick="openDrawer('${escHtml(j.id)}')">
    <div class="job-top">
      <span class="job-name">${escHtml(j.name || j.id)}</span>${isReviewJob ? '<span class="badge-review-type">review</span>' : ''}${j.profile_display || j.profile ? `<span class="badge-profile">${escHtml(j.profile_display || j.profile)}</span>` : ''}
      <span class="state-badge badge-${escHtml(s)}">${escHtml(s)}</span>
    </div>
    <div class="job-id-sub"><a class="job-id" href="/job/${escHtml(j.id)}" onclick="event.stopPropagation()">${escHtml(j.id)}</a></div>
    <div class="job-request">${escHtml(j.request || '')}</div>
    <div class="job-meta">
      <span>⏱ ${relTime(j.submitted_at)}</span>
      <span>📁 ${escHtml(j.target_repo || '')}</span>
      <span title="Model this job ran on (recorded when the worker started)">🤖 ${escHtml(j.resolved_model || (j.model_hint && j.model_hint !== 'auto' ? j.model_hint : 'not run yet'))}</span>
      ${j.priority   ? '<span>⬆ p' + escHtml(String(j.priority)) + '</span>' : ''}
    </div>
    ${j.summary ? `<div class="${sumCls}">↳ ${escHtml(j.summary)}</div>` : ''}
  </div>
  <div class="job-actions">${actions.join('')}</div>
</div>`;
}

function renderJobs() {
  if (document.querySelector('.status-dropdown.open, #chain-modal.open')) return;
  const list = document.getElementById('job-list');
  let jobs = currentFilter === 'all' ? allJobs : allJobs.filter(j => j._state === currentFilter);
  if (selectedRepo) jobs = jobs.filter(j => j.target_repo === selectedRepo);
  if (searchQuery) {
    jobs = jobs.filter(j =>
      (j.name || '').toLowerCase().includes(searchQuery) ||
      (j.request || '').toLowerCase().includes(searchQuery) ||
      (j.target_repo || '').toLowerCase().includes(searchQuery) ||
      (j.id || '').toLowerCase().includes(searchQuery)
    );
  }
  if (jobs.length === 0) {
    list.innerHTML = '<div class="empty"><div style="font-size:32px">📭</div><p>No jobs' +
      (currentFilter !== 'all' ? ' in <strong>' + escHtml(currentFilter) + '</strong>' : '') + '</p></div>';
    return;
  }

  // Build lookup structures for chain rendering
  const jobById = {};
  jobs.forEach(j => { jobById[j.id] = j; });
  const childMap = {};
  jobs.forEach(j => {
    if (j.parent_request_id) {
      childMap[j.parent_request_id] = childMap[j.parent_request_id] || [];
      childMap[j.parent_request_id].push(j.id);
    }
  });

  function renderChain(job, depth) {
    const children = (childMap[job.id] || []).map(cid => jobById[cid]).filter(Boolean);
    const cardHtml = buildJobCard(job);
    // Separate review children (same-branch) from manual chain children (own branch)
    const reviewChildren = children.filter(c => c.job_type === 'review');
    const chainChildren  = children.filter(c => c.job_type !== 'review');
    const reviewHtml = reviewChildren.map(c =>
      `<div class="chain-child chain-review">${renderChain(c, depth + 1)}</div>`
    ).join('');
    const chainHtml = chainChildren.map(c =>
      `<div class="chain-child">${renderChain(c, depth + 1)}</div>`
    ).join('');
    return cardHtml + reviewHtml + chainHtml;
  }

  // Identify root jobs: no parent, or parent not in current filtered list
  const rootJobs = jobs.filter(j => !j.parent_request_id || !jobById[j.parent_request_id]);
  list.innerHTML = rootJobs.map(j => renderChain(j, 0)).join('');
}

function updateCounts(jobs) {
  const c = {all:0,pending:0,running:0,done:0,merged:0,failed:0,abandoned:0,cancelled:0};
  jobs.forEach(j => { c.all++; if (c[j._state] !== undefined) c[j._state]++; });
  Object.keys(c).forEach(k => { const el = document.getElementById('cnt-'+k); if(el) el.textContent = c[k]; });
}

async function fetchRepos() {
  try {
    const r = await fetch('/api/repos');
    if (!r.ok) return;
    const repos = await r.json();
    const sel = document.getElementById('repo-select');
    const current = sel.value;
    sel.innerHTML = '';
    repos.forEach(repo => {
      const opt = document.createElement('option');
      opt.value = repo;
      opt.textContent = repo;
      if (repo === current) opt.selected = true;
      sel.appendChild(opt);
    });
    if (!sel.value && repos.length) sel.value = repos[0];
  } catch(e) {}
}

function onRepoChange(repo) {
  selectedRepo = repo;
  renderJobs();
  populateAfterDropdown();
}

async function fetchJobs() {
  try {
    const r = await fetch('/api/jobs');
    if (!r.ok) return;
    allJobs = await r.json();
    lastFetch = Date.now();
    updateCounts(allJobs);
    renderJobs();
    populateAfterDropdown();
  } catch(e) {}
}

function populateAfterDropdown() {
  const sel = document.getElementById('after');
  const current = sel.value;
  sel.innerHTML = '<option value="">— no chain (independent job) —</option>';
  allJobs.filter(j => !selectedRepo || j.target_repo === selectedRepo).forEach(j => {
    const opt = document.createElement('option');
    opt.value = j.id;
    const label = (j.name || j.id) + '  [' + j._state + ']  ' + (j.request || '').slice(0, 50);
    opt.textContent = label;
    if (j.id === current) opt.selected = true;
    sel.appendChild(opt);
  });
}

async function submitJob() {
  const request    = document.getElementById('req').value.trim();
  const repo       = selectedRepo || (window.AGENTIC_CFG&&window.AGENTIC_CFG.defaultRepo);
  const priority   = parseInt(document.getElementById('priority').value, 10);
  const after      = document.getElementById('after').value.trim();
  if (!request) { toast('Request cannot be empty', 'error'); return; }
  try {
    const r = await fetch('/api/submit', {
      method:'POST', headers:{'Content-Type':'application/json'},
      // Model is no longer per-job — the worker resolves it from Settings
      // (local_model / cloud_model) at run time. 'auto' lets that happen.
      body: JSON.stringify({request, repo, priority, model_hint: 'auto', after}),
    });
    const d = await r.json();
    if (d.ok) { document.getElementById('req').value = ''; document.getElementById('after').value = ''; toast('Submitted ' + (d.name || d.id), 'success'); fetchJobs(); }
    else toast('Error: ' + (d.error || 'unknown'), 'error');
  } catch(e) { toast('Network error', 'error'); }
}

async function cancelJob(id) {
  const r = await fetch('/api/cancel', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast('Cancelled ' + id, 'success'); fetchJobs(); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

function toggleMenu(btn) {
  const dd = btn.nextElementSibling;
  document.querySelectorAll('.status-dropdown.open').forEach(d => { if (d !== dd) d.classList.remove('open'); });
  dd.classList.toggle('open');
}
document.addEventListener('click', () => {
  document.querySelectorAll('.status-dropdown.open').forEach(d => d.classList.remove('open'));
});

/* ── Review ── */
let reviewJobId = null;
let anchorLine = null;     // {file, line, side}
let reviewComments = [];
let reviewSubmitted = false;

function _loadDraft(jobId) {
  try {
    const raw = localStorage.getItem('agentic_review_' + jobId);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}
function _saveDraft(jobId, comments, submitted) {
  try {
    if (comments.length || submitted) {
      localStorage.setItem('agentic_review_' + jobId, JSON.stringify({ comments, submitted: !!submitted }));
    } else {
      localStorage.removeItem('agentic_review_' + jobId);
    }
  } catch {}
}

/* ── Chain editor ── */
let _chainJobId = null;

function openChain(id) {
  _chainJobId = id;
  const job = allJobs.find(j => j.id === id);
  const current = job && job.parent_request_id;
  const currentJob = current && allJobs.find(j => j.id === current);
  const currentLabel = currentJob ? (currentJob.name || current) : 'none (independent)';
  document.getElementById('chain-current-label').innerHTML =
    'Current parent: <span>' + escHtml(currentLabel) + '</span>';

  // Populate dropdown — all jobs except this one and its own descendants
  const sel = document.getElementById('chain-select');
  sel.innerHTML = '<option value="">— run independently (no parent) —</option>';
  allJobs.filter(j => j.id !== id).forEach(j => {
    const opt = document.createElement('option');
    opt.value = j.id;
    opt.textContent = (j.name || j.id) + '  [' + j._state + ']  ' + (j.request || '').slice(0, 50);
    opt.selected = j.id === current;
    sel.appendChild(opt);
  });

  document.getElementById('chain-modal').classList.add('open');
}

function closeChain() {
  document.getElementById('chain-modal').classList.remove('open');
  _chainJobId = null;
}

async function saveChain() {
  const parentId = document.getElementById('chain-select').value || null;
  const r = await fetch('/api/set-chain', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:_chainJobId, parent_id:parentId})});
  const d = await r.json();
  if (d.ok) { const pJob = parentId && allJobs.find(j => j.id === parentId); toast(parentId ? 'Chained after ' + (pJob && pJob.name ? pJob.name : parentId) : 'Removed from chain', 'success'); closeChain(); fetchJobs(); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

async function clearChain() {
  const r = await fetch('/api/set-chain', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:_chainJobId, parent_id:null})});
  const d = await r.json();
  if (d.ok) { toast('Removed from chain', 'success'); closeChain(); fetchJobs(); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

async function setStatus(id, status) {
  const r = await fetch('/api/set-status', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id, status})});
  const d = await r.json();
  if (d.ok) { toast('Moved ' + id + ' → ' + status, 'success'); fetchJobs(); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

async function abandonJob(id) {
  if (!confirm('Mark this running job as failed? (Use this to unstick a job whose worker crashed)')) return;
  const r = await fetch('/api/abandon', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast('Abandoned ' + id, 'success'); fetchJobs(); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

async function deleteJob(id) {
  const job = allJobs.find(j => j.id === id);
  const label = (job && job.name) ? job.name : id;
  if (!confirm(`Permanently delete "${label}"?\n\nThis removes the job, its branch, worktree, diff, and log. There is no undo.`)) return;
  const r = await fetch('/api/delete', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast('Deleted ' + label, 'success'); fetchJobs(); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

async function acceptChain(id) {
  const job = allJobs.find(j => j.id === id);
  const base = job ? (job.base_branch || 'base branch') : 'base branch';
  // Walk the full chain (all descendants in 'done' state)
  const doneChain = [];
  let cur = id;
  while (cur) {
    const next = allJobs.find(j => j.parent_request_id === cur && j._state === 'done');
    if (!next) break;
    doneChain.push(next);
    cur = next.id;
  }
  // Review jobs don't produce a separate merge (commits already on parent branch)
  const mergeJobs   = doneChain.filter(j => j.job_type !== 'review').length + 1;
  const reviewJobs  = doneChain.filter(j => j.job_type === 'review').length;
  const reviewNote  = reviewJobs ? ` (includes ${reviewJobs} review job(s) already baked in)` : '';
  if (!confirm(`Merge ${mergeJobs} job(s) into a new staging branch based on '${base}'${reviewNote}.\n\nThe staging branch will NOT be merged into your working branch automatically — run:\n  git merge <staging-branch>\n\nProceed?`)) return;
  const r = await fetch('/api/accept-chain', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) {
    toast(`${d.accepted.length} job(s) accepted → staging: ${d.staging_branch}  (run: git merge ${d.staging_branch})`, 'success');
    fetchJobs();
  } else toast('Error: ' + (d.error || 'unknown'), 'error');
}

async function acceptJob(id, acknowledge) {
  if (!acknowledge && !confirm('Merge agentic/' + id + ' into its base branch?')) return;
  const r = await fetch('/api/accept', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id, acknowledge: !!acknowledge})});
  const d = await r.json();
  if (d.ok) { toast('Accepted ' + id + ' → ' + (d.message || ''), 'success'); fetchJobs(); return; }
  if ((d.error || '').includes('notable action')) {
    if (confirm('⚠️ ' + d.error + '\\n\\nOpen the job to review the flagged actions. Merge anyway?')) {
      return acceptJob(id, true);
    }
    return;
  }
  toast('Accept failed: ' + (d.error || 'unknown'), 'error');
}

async function rejectJob(id) {
  if (!confirm('Discard agentic/' + id + ' and delete the worktree?')) return;
  const r = await fetch('/api/reject', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast('Rejected ' + id, 'success'); fetchJobs(); }
  else toast('Reject failed: ' + (d.error || 'unknown'), 'error');
}

async function reviewJob(id) {
  const r = await fetch('/api/apply-to-tree', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) toast((d.message || 'Changes applied — review in your IDE, then commit.'), 'success');
  else toast('Review failed: ' + (d.error || 'unknown'), 'error');
}

/* ── Worker streaming ── */
let workerEs = null;

async function stopWorker() {
  const r = await fetch('/api/stop-worker', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const d = await r.json();
  if (d.ok) {
    appendLog('■ Stop requested — waiting for worker to exit…', 'error');
  } else {
    toast('Stop failed: ' + (d.error || 'unknown'), 'error');
  }
}
let _runAll   = false;

function runWorker(loop = false) {
  if (workerEs) return;
  _runAll = loop;
  const btn    = document.getElementById('run-btn');
  const allBtn = document.getElementById('run-all-btn');
  const body   = document.getElementById('log-body');
  btn.disabled = true; btn.textContent = '⏳ Running…';
  allBtn.disabled = true;
  document.getElementById('stop-btn').style.display = 'inline-block';
  body.innerHTML = '';
  document.getElementById('log-panel').classList.add('open');
  document.body.classList.add('log-open');   // views reserve space for the console
  _syncDrawerWithLog(true);
  document.getElementById('log-dot').style.background = '#f0883e';
  document.getElementById('log-title').textContent = 'Worker running…';
  { const tk = document.getElementById('log-tokens'); if (tk) { tk.textContent = ''; tk.style.display = 'none'; } }

  workerEs = new EventSource('/api/worker-stream');
  workerEs.onmessage = e => {
    const msg = JSON.parse(e.data);
    // Live token counter — updates the header in place, never the log body.
    if (msg.progress) {
      const p = msg.progress;
      const k = n => n >= 1000 ? (n/1000).toFixed(1).replace(/\.0$/,'') + 'k' : String(n);
      const el = document.getElementById('log-tokens');
      let txt = '🔢 ' + (p.input||0).toLocaleString() + ' in / ' + (p.output||0).toLocaleString() + ' out';
      if (p.ctx_budget) txt += ' · ctx ' + k(p.ctx_used||0) + '/' + k(p.ctx_budget);
      el.textContent = txt;
      el.style.display = '';
      return;
    }
    if (msg.replayed) {
      appendLog('── reconnected — replaying missed output ──', '');
    }
    // Track which job was claimed so we can auto-open its drawer on completion
    if (msg.line && msg.line.startsWith('🔧 Claimed:')) {
      const match = msg.line.match(/j_[0-9a-f_]+/);
      if (match) window._lastClaimedJobId = match[0];
    }
    if (msg.done !== undefined) {
      workerEs.close(); workerEs = null;
      const rc = msg.rc;
      document.getElementById('log-dot').style.background = rc === 0 ? '#3fb950' : '#f85149';
      document.getElementById('log-title').textContent = rc === 0 ? 'Worker done ✓' : 'Worker failed (exit ' + rc + ')';
      fetchJobs().then(() => {
        const pending = allJobs.filter(j => j._state === 'pending').length;
        if (_runAll && rc === 0 && pending > 0) {
          appendLog('', '');
          appendLog('── ' + pending + ' job(s) still pending — starting next worker…', 'success');
          runWorker(true);
        } else {
          _runAll = false;
          btn.disabled = false; btn.textContent = '▶ Run Worker';
          document.getElementById('run-all-btn').disabled = false;
          document.getElementById('stop-btn').style.display = 'none';
          if (_runAll && pending === 0) appendLog('Queue empty — all done.', 'success');
        }
      });
      if (window._lastClaimedJobId && !_runAll) {
        const _openId = window._lastClaimedJobId;
        window._lastClaimedJobId = null;
        setTimeout(() => openDrawer(_openId), 500);
      }
      return;
    }
    if (msg.error) { appendLog(msg.error, 'error'); return; }
    const line = msg.line || '';
    const cls  = line.startsWith('✅') || line.startsWith('✓') ? 'success'
               : line.startsWith('❌') || line.startsWith('Error') ? 'error' : '';
    appendLog(line, cls);
  };
  workerEs.onerror = () => {
    if (workerEs) { workerEs.close(); workerEs = null; }
    btn.disabled = false; btn.textContent = '▶ Run Worker';
    document.getElementById('run-all-btn').disabled = false;
    document.getElementById('stop-btn').style.display = 'none';
    appendLog('Stream disconnected — click ▶ Run Worker to reconnect if job is still running', 'error');
  };
}

function appendLog(line, cls) {
  const body = document.getElementById('log-body');
  const el = document.createElement('div');
  el.className = 'log-line' + (cls ? ' ' + cls : '');
  el.textContent = line;
  body.appendChild(el);
  body.scrollTop = body.scrollHeight;
}

function closeLog() {
  document.getElementById('log-panel').classList.remove('open');
  document.body.classList.remove('log-open');
  _syncDrawerWithLog(false);
}

/* ── Log panel resize & collapse ── */
(function() {
  const panel = document.getElementById('log-panel');
  const handle = document.getElementById('log-resize');
  const collapseBtn = document.getElementById('log-collapse');
  const COLLAPSED_H = 36;
  const STORAGE_KEY = 'agentic_log_h';
  let _isCollapsed = false;

  function getStoredH() {
    return parseInt(localStorage.getItem(STORAGE_KEY) || '300', 10);
  }
  function setH(h) {
    // Write to :root so BOTH the panel and the views' space-reservation rules
    // (body.log-open) read the same height.
    document.documentElement.style.setProperty('--log-h', h + 'px');
  }

  // Restore saved height
  setH(getStoredH());

  handle.addEventListener('mousedown', function(e) {
    e.preventDefault();
    const startY = e.clientY;
    const startH = panel.getBoundingClientRect().height;
    function onMove(ev) {
      const newH = Math.max(80, startH - (ev.clientY - startY));
      setH(newH);
    }
    function onUp(ev) {
      const finalH = panel.getBoundingClientRect().height;
      if (finalH > COLLAPSED_H + 10) {
        localStorage.setItem(STORAGE_KEY, String(Math.round(finalH)));
        _isCollapsed = false;
      }
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  collapseBtn.addEventListener('click', function() {
    if (_isCollapsed) {
      setH(getStoredH());
      _isCollapsed = false;
      collapseBtn.textContent = '⌄';
    } else {
      setH(COLLAPSED_H);
      _isCollapsed = true;
      collapseBtn.textContent = '⌃';
    }
  });
})();

/* ── Diff helpers (duplicated from detail page — both are standalone documents) ── */
function parseSplitDiff(raw) {
  const files = [];
  let file = null, hunk = null;
  for (const line of raw.split('\n')) {
    if (line.startsWith('diff --git ')) {
      if (file) files.push(file);
      const m = line.match(/diff --git a\/(.*) b\/(.*)/);
      file = { name: m ? m[2] : line, hunks: [] }; hunk = null;
    } else if (file && line.startsWith('@@ ')) {
      hunk = { header: line, lines: [] }; file.hunks.push(hunk);
    } else if (hunk) {
      if      (line.startsWith('+') && !line.startsWith('+++')) hunk.lines.push({ t:'a', c:line.slice(1) });
      else if (line.startsWith('-') && !line.startsWith('---')) hunk.lines.push({ t:'d', c:line.slice(1) });
      else if (!line.startsWith('\\') && !line.startsWith('index ') && !line.startsWith('diff ')
            && !line.startsWith('---') && !line.startsWith('+++'))
        hunk.lines.push({ t:'c', c:line.slice(1) });
    }
  }
  if (file) files.push(file);
  return files;
}

function buildSplitRows(hunk) {
  const rows = [], pending = [];
  const m = hunk.header.match(/@@ -(\d+)(?:,\d+)? \+(\d+)/);
  let lo = m ? +m[1] : 1, ln = m ? +m[2] : 1;
  const flush = () => { while (pending.length) rows.push({ l:{n:lo++,c:pending.shift().c,t:'d'}, r:null }); };
  for (const line of hunk.lines) {
    if      (line.t === 'c') { flush(); rows.push({ l:{n:lo++,c:line.c,t:'c'}, r:{n:ln++,c:line.c,t:'c'} }); }
    else if (line.t === 'd') { pending.push(line); }
    else { pending.length ? rows.push({ l:{n:lo++,c:pending.shift().c,t:'d'}, r:{n:ln++,c:line.c,t:'a'} })
                          : rows.push({ l:null, r:{n:ln++,c:line.c,t:'a'} }); }
  }
  flush();
  return rows;
}

/* ── Review comment UI ── */
function initReview(id) {
  reviewJobId = id;
  anchorLine = null;
  const draft = _loadDraft(id);
  reviewComments = draft ? [...draft.comments] : [];
  reviewSubmitted = !!(draft && draft.submitted);
  document.getElementById('review-composer').style.display = 'none';
  renderReviewComments();
  const job = allJobs.find(j => j.id === id);
  document.getElementById('review-panel').style.display =
    (job && job._state === 'merged') ? 'none' : '';
}

function _clearAnchor() {
  anchorLine = null;
  document.querySelectorAll('.sd-ln.ln-anchor').forEach(el => el.classList.remove('ln-anchor'));
}

function _highlightRange(file, start, end, side) {
  document.querySelectorAll('.sd-ln[data-side]').forEach(el => {
    if (el.dataset.file === file && el.dataset.side === side) {
      const ln = +el.dataset.line;
      if (ln >= start && ln <= end) el.classList.add('ln-anchor');
    }
  });
}

function openComposer(file, startLine, endLine, side) {
  const loc = startLine === endLine ? `${file}:${startLine}` : `${file}:${startLine}–${endLine}`;
  document.getElementById('review-composer-label').textContent = loc;
  document.getElementById('review-textarea').value = '';
  const c = document.getElementById('review-composer');
  c.dataset.file = file; c.dataset.start = startLine; c.dataset.end = endLine; c.dataset.side = side;
  c.style.display = 'block';
  document.getElementById('review-textarea').focus();
}

function saveReviewComment() {
  const text = document.getElementById('review-textarea').value.trim();
  if (!text) return;
  const c = document.getElementById('review-composer');
  reviewComments.push({ file: c.dataset.file, startLine: +c.dataset.start, endLine: +c.dataset.end, side: c.dataset.side, comment: text });
  c.style.display = 'none';
  _clearAnchor();
  reviewSubmitted = false;
  if (reviewJobId) _saveDraft(reviewJobId, reviewComments, false);
  renderReviewComments();
  renderCommentMarkers();
  renderJobs();
}

function cancelReviewComment() {
  document.getElementById('review-composer').style.display = 'none';
  _clearAnchor();
}

function deleteReviewComment(idx) {
  reviewComments.splice(idx, 1);
  if (reviewJobId) _saveDraft(reviewJobId, reviewComments, false);
  renderReviewComments();
  renderCommentMarkers();
  renderJobs();
}

function renderCommentMarkers() {
  document.querySelectorAll('.sd-ln.ln-commented').forEach(el => el.classList.remove('ln-commented'));
  for (const c of reviewComments) {
    document.querySelectorAll('.sd-ln[data-side]').forEach(el => {
      if (el.dataset.file === c.file && el.dataset.side === c.side) {
        const ln = +el.dataset.line;
        if (ln >= c.startLine && ln <= c.endLine) el.classList.add('ln-commented');
      }
    });
  }
}

function renderReviewComments() {
  const list = document.getElementById('review-comments-list');
  const row  = document.getElementById('review-submit-row');
  if (!reviewComments.length) { list.innerHTML = ''; row.style.display = 'none'; return; }
  list.innerHTML = reviewComments.map((c, i) => {
    const loc = c.startLine === c.endLine ? `${c.file}:${c.startLine}` : `${c.file}:${c.startLine}–${c.endLine}`;
    const del = reviewSubmitted ? '' : `<button class="review-comment-del" onclick="deleteReviewComment(${i})" title="Remove">×</button>`;
    return `<div class="review-comment">
      <span class="review-comment-loc">${escHtml(loc)}</span>
      <span class="review-comment-text">${escHtml(c.comment)}</span>
      ${del}
    </div>`;
  }).join('');
  if (reviewSubmitted) {
    row.innerHTML = '<span style="color:#3fb950;font-size:12px">✓ Review submitted</span>';
  } else {
    const n = reviewComments.length;
    row.innerHTML = `<span id="review-count">${n} comment${n !== 1 ? 's' : ''}</span><button class="btn btn-primary" onclick="submitReview()">Submit Review</button>`;
  }
  row.style.display = 'flex';
}

async function submitReview() {
  if (!reviewComments.length || !reviewJobId) return;
  const btn = document.querySelector('#review-submit-row button');
  btn.disabled = true; btn.textContent = 'Submitting…';
  try {
    const r = await fetch('/api/review', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ job_id: reviewJobId, comments: reviewComments }),
    });
    const d = await r.json();
    if (d.ok) { _saveDraft(reviewJobId, reviewComments, true); reviewSubmitted = true; renderReviewComments(); renderJobs(); toast('Review submitted — ' + (d.name || d.id), 'success'); closeDiff(); }
    else { toast('Failed: ' + (d.error || 'unknown'), 'error'); btn.disabled = false; btn.textContent = 'Submit Review'; }
  } catch(e) { toast('Network error', 'error'); btn.disabled = false; btn.textContent = 'Submit Review'; }
}

document.getElementById('diff-content').addEventListener('click', e => {
  const td = e.target.closest('.sd-ln[data-line]');
  if (!td) { _clearAnchor(); return; }
  const file = td.dataset.file, line = +td.dataset.line, side = td.dataset.side;
  if (!anchorLine || anchorLine.file !== file) {
    _clearAnchor();
    anchorLine = { file, line, side };
    td.classList.add('ln-anchor');
  } else {
    const start = Math.min(anchorLine.line, line), end = Math.max(anchorLine.line, line);
    _clearAnchor();
    _highlightRange(file, start, end, side);
    openComposer(file, start, end, side);
  }
});

/* ── Diff modal ── */
async function viewDiff(id) {
  const job = allJobs.find(j => j.id === id);
  const label = (job && job.name) ? job.name : id;
  document.getElementById('diff-title').textContent = label;
  document.getElementById('diff-content').innerHTML = '<div style="padding:16px;color:#8b949e">Loading…</div>';
  document.getElementById('diff-modal').classList.add('open');
  initReview(id);
  try {
    const r = await fetch('/api/diff/' + encodeURIComponent(id));
    const d = await r.json();
    if (!d.ok) {
      document.getElementById('diff-content').innerHTML = '<div style="padding:16px;color:#f85149">' + escHtml(d.error) + '</div>';
      return;
    }
    const raw = d.diff || '';
    if (!raw.trim()) {
      document.getElementById('diff-content').innerHTML = '<div style="padding:16px;color:#6e7681">No diff available.</div>';
      return;
    }
    const files = parseSplitDiff(raw);
    if (!files.length) {
      document.getElementById('diff-content').innerHTML = '<div style="padding:16px;color:#6e7681">No changes found.</div>';
      return;
    }
    const html = files.map(fileObj => {
      let diffHtml = '<div style="padding:8px;font-size:11px;color:#6e7681">No hunks.</div>';
      if (fileObj.hunks.length) {
        const tbl = fileObj.hunks.map(h => {
          const dataRows = buildSplitRows(h).map(row => {
            const L=row.l, R=row.r, lt=L?L.t:'e', rt=R?R.t:'e';
            const fn = escHtml(fileObj.name);
            const lnL = L ? ` data-file="${fn}" data-line="${L.n}" data-side="L"` : '';
            const lnR = R ? ` data-file="${fn}" data-line="${R.n}" data-side="R"` : '';
            return `<tr>`
              + `<td class="sd-ln sd-${lt}"${lnL}>${L?L.n:''}</td>`
              + `<td class="sd-cell sd-${lt}">${L?escHtml(L.c):''}</td>`
              + `<td class="sd-div"></td>`
              + `<td class="sd-ln sd-${rt}"${lnR}>${R?R.n:''}</td>`
              + `<td class="sd-cell sd-${rt}">${R?escHtml(R.c):''}</td>`
              + `</tr>`;
          }).join('');
          return `<tr class="sd-hunk-row"><td colspan="5">${escHtml(h.header)}</td></tr>${dataRows}`;
        }).join('');
        diffHtml = `<div style="overflow-x:auto"><table class="sd-table">${tbl}</table></div>`;
      }
      return `<details open style="border:1px solid #21262d;border-radius:6px;margin-bottom:6px;overflow:hidden">
        <summary style="display:flex;align-items:center;gap:8px;cursor:pointer;list-style:none;padding:6px 10px;background:#161b22">
          <span style="color:#3fb950;font-size:13px">±</span>
          <span style="font-family:monospace;font-size:12px;color:#e6edf3">${escHtml(fileObj.name)}</span>
        </summary>
        ${diffHtml}
      </details>`;
    }).join('');
    document.getElementById('diff-content').innerHTML = `<div style="padding:12px">${html}</div>`;
    renderCommentMarkers();
  } catch(e) {
    document.getElementById('diff-content').innerHTML = '<div style="padding:16px;color:#f85149">Failed to load diff</div>';
  }
}

function closeDiff(e) {
  if (!e || e.target === document.getElementById('diff-modal') || e.currentTarget === document.getElementById('diff-close')) {
    if (reviewJobId) _saveDraft(reviewJobId, reviewComments, reviewSubmitted);
    document.getElementById('diff-modal').classList.remove('open');
    _clearAnchor();
    reviewComments = [];
    document.getElementById('review-composer').style.display = 'none';
    renderReviewComments();
    renderJobs();
  }
}

/* ── Toast ── */
let toastTimer;
function toast(msg, type) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show ' + (type || '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, 3000);
}

document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') submitJob();
  if (e.key === 'Escape') { closeDiff(); closeChain(); }
});
document.getElementById('chain-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('chain-modal')) closeChain();
});

/* ── Detail drawer ── */
function openDrawer(id) {
  const drawer = document.getElementById('detail-drawer');
  const backdrop = document.getElementById('drawer-backdrop');
  const iframe = document.getElementById('detail-iframe');
  const jobIdEl = document.getElementById('drawer-job-id');
  const tabLink = document.getElementById('drawer-open-tab');

  jobIdEl.textContent = id;
  tabLink.href = '/job/' + encodeURIComponent(id);
  iframe.src = '/job/' + encodeURIComponent(id);

  drawer.classList.add('open');
  backdrop.classList.add('open');
  // The drawer's bottom follows the console automatically via the CSS rule
  // `body.log-open #detail-drawer { bottom: var(--log-h) }` — no JS needed; it
  // tracks resize/collapse of the terminal too.
}

function closeDrawer() {
  document.getElementById('detail-drawer').classList.remove('open');
  document.getElementById('drawer-backdrop').classList.remove('open');
  document.getElementById('detail-iframe').src = '';
}

// Drawer/console coordination is now pure CSS (body.log-open + --log-h). Kept as
// a no-op so existing openLog/closeLog callers don't break.
function _syncDrawerWithLog(_open) {}

// Model is no longer chosen on the Submit form — it's a global Settings knob
// (local_model / cloud_model). The header badge shows the active one. Here we
// just clarify which execution mode runs jobs and point to Settings.
(function() {
  const hint = document.getElementById('exec-mode-hint');
  if (hint) {
    hint.innerHTML = IS_LOCAL
      ? 'Jobs run in <b style="color:#3fb950">Local</b> mode (Ollama) on the model in the header badge. Change mode/model in ⚙ Settings.'
      : 'Jobs run in <b style="color:#58a6ff">Cloud</b> mode (Claude) on the model in the header badge. Change mode/model in ⚙ Settings.';
  }
}());

// ── Settings panel (local mode only) ──
// Settings is always reachable — the panel itself holds the local/cloud switch.
document.getElementById('settings-btn').style.display = '';
let _settingsData = null;

const SETTINGS_GROUPS = [
  ['mode',    'Mode & project'],
  ['cloud',   'Cloud model'],
  ['context', 'Local model · context & loop'],
  ['model',   'Local model · model & Ollama'],
  ['caps',    'Local model · tool output caps'],
  ['timeout', 'Local model · job timeout'],
];

function openSettings() {
  document.getElementById('settings-overlay').style.display = 'flex';
  document.getElementById('settings-body').innerHTML = '<div style="color:#8b949e;font-size:13px">Loading…</div>';
  document.getElementById('settings-status').textContent = '';
  fetch('/api/settings').then(r => r.json()).then(d => {
    if (!d.ok) { document.getElementById('settings-body').textContent = 'Error: ' + (d.error||'unknown'); return; }
    _settingsData = d;
    renderSettings(d);
  }).catch(e => { document.getElementById('settings-body').textContent = 'Failed to load settings'; });
}
function closeSettings() { document.getElementById('settings-overlay').style.display = 'none'; }

function renderSettings(d) {
  const byKey = {};
  d.schema.forEach(s => byKey[s.key] = s);
  let html = '';
  SETTINGS_GROUPS.forEach(([gid, gname], i) => {
    const rows = d.schema.filter(s => s.group === gid);
    if (!rows.length) return;
    // A one-line note before the first local-only section clarifies scope.
    if (gid === 'context') {
      html += `<div style="margin:6px 0 14px;padding:8px 10px;background:#010409;border:1px solid #21262d;border-radius:6px;font-size:11px;color:#6e7681">The settings below apply to <b style="color:#8b949e">local mode</b> (Ollama) jobs. Cloud mode uses the Claude API and the key above.</div>`;
    }
    html += `<div style="margin-bottom:18px"><div style="font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px">${escHtml(gname)}</div>`;
    rows.forEach(s => { html += settingControl(s, d); });
    html += `</div>`;
  });
  // API key (secret) — shown as status, write-only
  html += `<div style="margin-bottom:6px"><div style="font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px">Anthropic API key (cloud mode)</div>
    <div style="display:flex;align-items:center;gap:8px">
      <input id="set-secret-key" type="password" placeholder="${d.secrets.anthropic_api_key ? '●●●●●●●● set — type to replace' : 'not set'}" style="flex:1;background:#010409;border:1px solid #21262d;color:#e6edf3;padding:6px 10px;border-radius:6px;font-size:12px">
      <button onclick="saveSecret()" style="background:#21262d;border:1px solid #30363d;color:#e6edf3;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px">Set</button>
    </div>
    <div style="font-size:11px;color:#6e7681;margin-top:4px">Stored locally (0600), never shown again.</div></div>`;
  document.getElementById('settings-body').innerHTML = html;
}

function settingControl(s, d) {
  const help = `<div style="font-size:11px;color:#6e7681;margin-top:2px">${escHtml(s.help||'')}</div>`;
  const label = `<label style="font-size:13px;color:#e6edf3;font-weight:500">${escHtml(s.label)}</label>`;
  if (s.control === 'slider') {
    // Cap the context-budget slider at the model's real window.
    let max = s.max;
    let note = '';
    if (s.key === 'context_budget' && d.num_ctx) { max = d.num_ctx; note = ` <span style="color:#6e7681;font-weight:400">(model window: ${(d.num_ctx/1000).toFixed(0)}k)</span>`; }
    return `<div style="margin-bottom:14px">
      <div style="display:flex;align-items:baseline;gap:8px">${label}<span id="val-${s.key}" style="margin-left:auto;font-size:12px;color:#88b4ff;font-variant-numeric:tabular-nums">${s.value}</span>${note}</div>
      <input type="range" id="set-${s.key}" min="${s.min}" max="${max}" step="${s.step}" value="${Math.min(s.value,max)}"
             oninput="document.getElementById('val-${s.key}').textContent=this.value" style="width:100%;margin-top:6px">
      ${help}</div>`;
  }
  if (s.control === 'dirpicker') {
    // Text input (keeps id set-${s.key} so saveSettings reads it generically)
    // plus a Browse button that opens the confined folder browser.
    return `<div style="margin-bottom:14px">${label}
      <div style="display:flex;gap:8px;margin-top:6px">
        <input type="text" id="set-${s.key}" value="${escHtml(String(s.value))}" placeholder="(server working dir)"
               style="flex:1;background:#010409;border:1px solid #21262d;color:#e6edf3;padding:6px 10px;border-radius:6px;font-size:13px">
        <button type="button" onclick="openDirPicker('set-${s.key}')"
                style="background:#21262d;border:1px solid #30363d;color:#e6edf3;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap">Browse…</button>
      </div>${help}</div>`;
  }
  if (s.control === 'select') {
    // Fixed-option knobs (e.g. mode) use s.options; model dropdowns fall back to
    // the relevant list — cloud_model → cloud models, local_model → ollama models.
    let choices = (s.options && s.options.length) ? s.options : null;
    if (!choices) choices = (s.key === 'cloud_model') ? (d.cloud_models || []) : (d.ollama_models || []);
    const opts = choices.map(m => `<option value="${escHtml(m)}"${m===s.value?' selected':''}>${escHtml(m)}</option>`).join('')
      || `<option value="${escHtml(s.value)}" selected>${escHtml(s.value)}</option>`;
    return `<div style="margin-bottom:14px">${label}<select id="set-${s.key}" style="width:100%;margin-top:6px;background:#010409;border:1px solid #21262d;color:#e6edf3;padding:6px 10px;border-radius:6px;font-size:13px">${opts}</select>${help}</div>`;
  }
  // number / text
  const t = s.type === 'int' ? 'number' : 'text';
  const bounds = s.type === 'int' && s.min!==undefined ? `min="${s.min}" max="${s.max}" step="${s.step||1}"` : '';
  return `<div style="margin-bottom:14px;display:flex;align-items:center;gap:10px">
    <div style="flex:1">${label}${help}</div>
    <input type="${t}" id="set-${s.key}" value="${escHtml(String(s.value))}" ${bounds} style="width:110px;background:#010409;border:1px solid #21262d;color:#e6edf3;padding:6px 10px;border-radius:6px;font-size:13px;text-align:right"></div>`;
}

function saveSettings() {
  if (!_settingsData) return;
  const updates = {};
  _settingsData.schema.forEach(s => {
    const el = document.getElementById('set-' + s.key);
    if (!el) return;
    updates[s.key] = s.type === 'int' ? parseInt(el.value, 10) : el.value;
  });
  document.getElementById('settings-status').textContent = 'Saving…';
  fetch('/api/settings', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({settings: updates})})
    .then(r => r.json()).then(d => {
      if (!d.ok) { document.getElementById('settings-status').textContent = 'Error: ' + (d.error||'unknown'); return; }
      _settingsData.schema.forEach(s => { if (d.settings[s.key]!==undefined) s.value = d.settings[s.key]; });
      // Mode is baked into the page (IS_LOCAL gates the badge, model list, submit
      // field, which model API to call). If it changed, reload so the whole UI
      // reflects the new mode instead of looking unchanged until a manual reload.
      const nowLocal = d.settings.mode === 'local';
      if (nowLocal !== IS_LOCAL) {
        document.getElementById('settings-status').textContent = '✓ Switched to ' + d.settings.mode + ' mode — reloading…';
        setTimeout(() => location.reload(), 700);
      } else {
        document.getElementById('settings-status').textContent = '✓ Saved — applies to the next job';
      }
    }).catch(() => document.getElementById('settings-status').textContent = 'Save failed');
}

function saveSecret() {
  const v = document.getElementById('set-secret-key').value;
  if (!v) return;
  fetch('/api/secrets', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'ANTHROPIC_API_KEY', value:v})})
    .then(r => r.json()).then(d => {
      document.getElementById('settings-status').textContent = d.ok ? '✓ API key saved' : 'Error: ' + (d.error||'unknown');
      if (d.ok) document.getElementById('set-secret-key').value = '';
    }).catch(() => document.getElementById('settings-status').textContent = 'Failed to save key');
}

// ── Directory picker (confined folder browser, sets a target input) ──────────
let _dirpickerTarget = null;   // id of the input to write the chosen path into
let _dirpickerCurrent = '';    // path currently shown

function openDirPicker(targetInputId) {
  _dirpickerTarget = targetInputId;
  document.getElementById('dirpicker-overlay').style.display = 'flex';
  // Start at the current value if set, else at the browse root (empty path).
  const cur = (document.getElementById(targetInputId) || {}).value || '';
  browseTo(cur);
}
function closeDirPicker() {
  document.getElementById('dirpicker-overlay').style.display = 'none';
  _dirpickerTarget = null;
}
function browseTo(path) {
  const list = document.getElementById('dirpicker-list');
  list.innerHTML = '<div style="color:#8b949e;font-size:13px;padding:8px">Loading…</div>';
  fetch('/api/browse?path=' + encodeURIComponent(path || ''))
    .then(r => r.json()).then(d => {
      if (!d.ok) { list.innerHTML = '<div style="color:#f85149;font-size:13px;padding:8px">' + escHtml(d.error||'error') + '</div>'; return; }
      _dirpickerCurrent = d.path;
      document.getElementById('dirpicker-path').textContent = d.path;
      // "Use this folder" is enabled only when the current dir is itself a repo.
      const useBtn = document.getElementById('dirpicker-use');
      useBtn.disabled = !d.is_repo;
      useBtn.style.opacity = d.is_repo ? '1' : '.45';
      useBtn.style.cursor  = d.is_repo ? 'pointer' : 'not-allowed';
      document.getElementById('dirpicker-hint').textContent = d.is_repo
        ? 'This folder is a git repo — you can use it.'
        : 'Open a folder marked ● to pick a git repo.';
      let html = '';
      if (d.parent) {
        html += `<div onclick="browseTo('${escAttr(d.parent)}')" style="padding:7px 10px;cursor:pointer;border-radius:6px;color:#8b949e;font-size:13px" onmouseover="this.style.background='#161b22'" onmouseout="this.style.background='none'">⬆ ..</div>`;
      }
      if (!d.entries.length) {
        html += '<div style="color:#6e7681;font-size:12px;padding:8px">No subfolders.</div>';
      }
      d.entries.forEach(e => {
        const dot = e.is_repo ? '<span style="color:#3fb950">●</span> ' : '<span style="color:#30363d">▸</span> ';
        const tag = e.is_repo ? ' <span style="color:#6e7681;font-size:11px">git repo</span>' : '';
        html += `<div style="display:flex;align-items:center;gap:6px;padding:7px 10px;border-radius:6px" onmouseover="this.style.background='#161b22'" onmouseout="this.style.background='none'">
          <span onclick="browseTo('${escAttr(e.path)}')" style="flex:1;cursor:pointer;color:#e6edf3;font-size:13px">${dot}${escHtml(e.name)}${tag}</span>
          ${e.is_repo ? `<button onclick="pickDir('${escAttr(e.path)}')" style="background:#238636;color:#fff;border:none;padding:3px 10px;border-radius:5px;cursor:pointer;font-size:11px">Use</button>` : ''}
        </div>`;
      });
      list.innerHTML = html;
    }).catch(() => { list.innerHTML = '<div style="color:#f85149;font-size:13px;padding:8px">Failed to browse</div>'; });
}
function pickDir(path) {
  if (_dirpickerTarget) {
    const el = document.getElementById(_dirpickerTarget);
    if (el) el.value = path;
  }
  closeDirPicker();
  // Channels: picking a repo here means "create a channel for it".
  if (window._chPendingCreate) {
    window._chPendingCreate = false;
    _createChannelFromRepo(path);
  }
}
function useCurrentDir() {
  if (_dirpickerCurrent) pickDir(_dirpickerCurrent);
}
// Attribute-safe escaping for inline onclick handlers (single-quote context).
function escAttr(s) { return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'&quot;'); }

// ════════════════════════════════════════════════════════════════════════
// Planning Channels — Queue|Channels toggle + three-zone view + proposal drawer
// ════════════════════════════════════════════════════════════════════════
let currentView = 'queue';
let chState = { cid: null, tid: null, channels: [], models: {cloud:[],local:[]}, prop: null, askEs: null };

function setView(v) {
  currentView = v;
  document.getElementById('vt-queue').classList.toggle('active', v === 'queue');
  document.getElementById('vt-channels').classList.toggle('active', v === 'channels');
  document.getElementById('queue-view').style.display = (v === 'queue') ? '' : 'none';
  document.getElementById('channels-view').classList.toggle('active', v === 'channels');
  if (v === 'channels') {
    if (!chState.models.cloud.length && !chState.models.local.length) loadChannelModels();
    loadChannels();
  }
}

async function loadChannelModels() {
  try {
    const d = await (await fetch('/api/channels/models')).json();
    if (d.ok) chState.models = { cloud: d.cloud || [], local: d.local || [] };
  } catch (e) {}
}

async function loadChannels() {
  const tree = document.getElementById('ch-tree');
  try {
    const d = await (await fetch('/api/channels')).json();
    if (!d.ok) { tree.innerHTML = '<div style="padding:14px;color:#f85149;font-size:12px">'+escHtml(d.error||'error')+'</div>'; return; }
    chState.channels = d.channels || [];
    if (!chState.channels.length) {
      tree.innerHTML = '<div style="padding:14px;color:#6e7681;font-size:12px">No channels yet. Click <b>+ Channel</b> to start one for a repo.</div>';
      return;
    }
    let html = '';
    chState.channels.forEach(ch => {
      const shortRepo = (ch.repo||'').split('/').slice(-1)[0] || ch.repo;
      html += `<div class="ch-item ch-repo" title="${escAttr(ch.repo)}">
        <span>📁 ${escHtml(shortRepo)}</span>
        <span class="ch-repo-path">${escHtml((ch.profile)||'')}</span></div>`;
      (ch.threads||[]).forEach(t => {
        const act = (t.id === chState.tid) ? ' active' : '';
        html += `<div class="ch-thread${act}" onclick="selectThread('${escAttr(ch.id)}','${escAttr(t.id)}')">
          <span class="ch-thread-name">💬 ${escHtml(t.name || t.title || t.id)}</span>
          <span class="ch-thread-mode">${escHtml(t.planning_mode||'local')}</span>
          <button class="ch-thread-del" title="Delete thread" onclick="event.stopPropagation();deleteThread('${escAttr(ch.id)}','${escAttr(t.id)}','${escAttr(t.name||t.title||t.id)}')">✕</button></div>`;
      });
      html += `<div class="ch-thread ch-thread-new" onclick="newThread('${escAttr(ch.id)}')">+ New thread</div>`;
    });
    tree.innerHTML = html;
  } catch (e) {
    tree.innerHTML = '<div style="padding:14px;color:#f85149;font-size:12px">Failed to load channels</div>';
  }
}

function newChannel() {
  // Reuse the dirpicker: stash a hidden input the picker writes into, then create.
  let hidden = document.getElementById('ch-repo-hidden');
  if (!hidden) {
    hidden = document.createElement('input');
    hidden.type = 'hidden'; hidden.id = 'ch-repo-hidden';
    document.body.appendChild(hidden);
  }
  // Override pickDir's normal flow: after the picker sets the value, create.
  window._chPendingCreate = true;
  openDirPicker('ch-repo-hidden');
}

async function _createChannelFromRepo(repo) {
  try {
    const d = await (await fetch('/api/channel/create', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({repo})})).json();
    if (d.ok) { toast('Channel ready for ' + (repo.split('/').slice(-1)[0]||repo), 'success'); await loadChannels(); }
    else toast(d.error||'Failed to create channel', 'error');
  } catch (e) { toast('Failed to create channel', 'error'); }
}

async function newThread(cid) {
  try {
    const d = await (await fetch('/api/channel/'+cid+'/thread/create', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})})).json();
    if (d.ok) { await loadChannels(); selectThread(cid, d.id); }
    else toast(d.error||'Failed', 'error');
  } catch (e) { toast('Failed to create thread', 'error'); }
}

async function deleteThread(cid, tid, name) {
  if (!confirm('Delete thread "' + (name||tid) + '"? Its conversation and citations are removed. This cannot be undone.')) return;
  try {
    const d = await (await fetch('/api/channel/'+cid+'/'+tid+'/delete', {method:'POST'})).json();
    if (d.ok === false) { toast(d.error||'Delete failed', 'error'); return; }
    toast('Thread deleted', 'success');
    // If the open thread was deleted, clear the center pane.
    if (chState.tid === tid) {
      chState.tid = null;
      const tr = document.getElementById('ch-transcript');
      if (tr) tr.innerHTML = '<div id="ch-empty">Thread deleted. Pick or create another.</div>';
      const ir = document.getElementById('ch-input-row'); if (ir) ir.style.display = 'none';
    }
    await loadChannels();
  } catch (e) { toast('Delete failed', 'error'); }
}

async function selectThread(cid, tid) {
  chState.cid = cid; chState.tid = tid;
  if (chState.askEs) { chState.askEs.close(); chState.askEs = null; }
  document.querySelectorAll('.ch-thread').forEach(e => e.classList.remove('active'));
  loadChannels();  // refresh active highlight
  document.getElementById('ch-input-row').style.display = 'flex';
  document.getElementById('ch-plan-label').style.display = '';
  document.getElementById('ch-mode').style.display = '';
  document.getElementById('ch-model').style.display = '';
  document.getElementById('ch-reindex-btn').style.display = '';
  document.getElementById('ch-derive-btn').style.display = '';
  try {
    const d = await (await fetch('/api/channel/'+cid+'/'+tid)).json();
    if (!d.ok) { toast(d.error||'Failed to load thread', 'error'); return; }
    const h = d.header || {};
    document.getElementById('ch-active-title').textContent = h.name || h.title || tid;
    // Backend + model selectors
    document.getElementById('ch-mode').value = h.planning_mode || 'local';
    renderModelOptions(h.planning_mode || 'local', h.planning_model || '');
    // Index stat from the channel header
    const ch = chState.channels.find(c => c.id === cid) || {};
    if (ch.index) document.getElementById('ch-index-stat').textContent = 'Index: ' + ch.index.symbols + ' symbols · ' + ch.index.files + ' files';
    else document.getElementById('ch-index-stat').textContent = '';
    renderTranscript(d.transcript || []);
    renderCitations(d.citations || []);
  } catch (e) { toast('Failed to load thread', 'error'); }
}

function renderModelOptions(mode, current) {
  const sel = document.getElementById('ch-model');
  const list = (mode === 'cloud') ? chState.models.cloud : chState.models.local;
  let html = '<option value="">(backend default)</option>';
  (list||[]).forEach(m => { html += `<option value="${escAttr(m)}"${m===current?' selected':''}>${escHtml(m)}</option>`; });
  sel.innerHTML = html;
}

async function saveThreadModel() {
  if (!chState.cid || !chState.tid) return;
  const mode = document.getElementById('ch-mode').value;
  renderModelOptions(mode, document.getElementById('ch-model').value);
  const model = document.getElementById('ch-model').value;
  try {
    await fetch('/api/channel/'+chState.cid+'/'+chState.tid+'/set-model', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({planning_mode:mode, planning_model:model})});
    loadChannels();
  } catch (e) {}
}

function renderTranscript(tx) {
  const c = document.getElementById('ch-transcript');
  if (!tx.length) { c.innerHTML = '<div id="ch-empty">Ask a question to get a grounded, cited answer.</div>'; return; }
  let html = '';
  tx.forEach(e => { html += transcriptBubble(e); });
  c.innerHTML = html;
  c.scrollTop = c.scrollHeight;
}

// Minimal, XSS-safe Markdown → HTML for assistant chat answers. Escapes first,
// then applies a curated subset (the markdown the planning model actually emits):
// fenced code, headers, bold/italic, inline code, ordered + unordered lists.
function mdToHtml(src) {
  // 1) Escape ALL html up front so model output can never inject markup.
  let s = escHtml(src || '');
  // 2) Fenced code blocks ```...``` → <pre><code> (protect from inline rules).
  const blocks = [];
  s = s.replace(/```[a-zA-Z0-9_-]*\n?([\s\S]*?)```/g, (m, code) => {
    blocks.push('<pre class="md-pre"><code>' + code.replace(/\n+$/,'') + '</code></pre>');
    return '\n@@CB' + (blocks.length - 1) + '@@\n';
  });
  // 3) Inline code `x` (after fences are stashed).
  s = s.replace(/`([^`\n]+)`/g, '<code class="md-code">$1</code>');
  // 4) Bold / italic.
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  // 5) Line-based block structure: headers, list items, paragraphs.
  const lines = s.split('\n');
  let out = '', listType = null;
  const closeList = () => { if (listType) { out += '</' + listType + '>'; listType = null; } };
  for (let raw of lines) {
    const ph = raw.match(/^@@CB(\d+)@@$/);
    if (ph) { closeList(); out += blocks[+ph[1]]; continue; }
    const line = raw.trim();
    if (!line) { closeList(); continue; }
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      closeList(); const lv = m[1].length + 2; out += '<h' + lv + ' class="md-h">' + m[2] + '</h' + lv + '>';
    } else if ((m = line.match(/^[-*]\s+(.*)$/))) {
      if (listType !== 'ul') { closeList(); out += '<ul class="md-ul">'; listType = 'ul'; }
      out += '<li>' + m[1] + '</li>';
    } else if ((m = line.match(/^\d+[.)]\s+(.*)$/))) {
      if (listType !== 'ol') { closeList(); out += '<ol class="md-ol">'; listType = 'ol'; }
      out += '<li>' + m[1] + '</li>';
    } else {
      closeList(); out += '<p class="md-p">' + line + '</p>';
    }
  }
  closeList();
  return out;
}

function transcriptBubble(e) {
  if (e.role === 'user') return `<div class="ch-bubble user">${escHtml(e.text||'')}</div>`;
  if (e.role === 'assistant') {
    const isAgent = e.grounding === 'agent';
    const badge = e.grounding === 'index'
      ? '<span class="ch-cost-badge">index • free</span>'
      : `<span class="ch-cost-badge agent">${escHtml(e.badge || ('read · '+(e.turns||0)+' turns'))}</span>`;
    let footer = '';
    if (e.citations && e.citations.length) {
      footer = '<div class="ch-grounded-footer">grounded in: ' +
        e.citations.filter(ct => ct.file).map(ct => `<span title="View this code" onclick="scrollToCite('${escAttr(ct.file)}',${ct.start||0},${ct.end||0},'${escAttr(ct.symbol||'')}')">${escHtml(ct.file)}${ct.start?':'+ct.start:''}</span>`).join(', ') + '</div>';
    }
    return `<div class="ch-bubble assistant"><div class="ch-md">${mdToHtml(e.text||'')}</div>${badge}${footer}</div>`;
  }
  if (e.role === 'tool') return `<div class="ch-bubble tool">⚙ ${escHtml(e.name||'tool')} ${escHtml(JSON.stringify(e.input||{}).slice(0,120))}</div>`;
  if (e.role === 'draft') return `<div class="ch-bubble assistant" style="color:#58a6ff">📋 Drafted a job proposal.</div>`;
  if (e.role === 'submitted') return `<div class="ch-bubble assistant" style="color:#3fb950">✓ Queued ${(e.jobs||[]).length} job(s) as a chain.</div>`;
  return '';
}

function renderCitations(cites) {
  const c = document.getElementById('ch-citations-list');
  if (!cites.length) { c.innerHTML = '<div style="padding:8px 14px;color:#6e7681;font-size:11px">No citations yet.</div>'; return; }
  c.innerHTML = cites.filter(ct => ct.file).map(ct =>
    `<div class="ch-cite" title="View this code" onclick="scrollToCite('${escAttr(ct.file)}',${ct.start||0},${ct.end||0},'${escAttr(ct.symbol||'')}')">
      <div class="ch-cite-loc">${escHtml(ct.file)}${ct.start?':'+ct.start+(ct.end&&ct.end!==ct.start?'-'+ct.end:''):''}</div>
      ${ct.why?`<div class="ch-cite-why">${escHtml(ct.why)}</div>`:''}</div>`).join('');
}

function scrollToCite(file, line, end, symbol) {
  // Open a read-only code peek. With a line, show that range; without one, show
  // the whole file and highlight the relevant symbol (if known).
  if (!chState.cid) { toast(file, 'success'); return; }
  const pop = document.getElementById('peek-pop');
  const body = document.getElementById('peek-body');
  const title = document.getElementById('peek-title');
  const hasLine = line && line > 0;
  title.textContent = file + (hasLine ? ':' + line + (end && end !== line ? '-' + end : '') : '');
  body.innerHTML = '<div style="padding:14px;color:#8b949e;font-size:12px">Loading…</div>';
  document.getElementById('peek-edit').onclick = null;
  pop.style.display = 'flex';
  let u = '/api/peek?cid=' + encodeURIComponent(chState.cid) + '&file=' + encodeURIComponent(file);
  if (hasLine) u += '&start=' + line + '&end=' + (end || line);
  if (symbol) u += '&symbol=' + encodeURIComponent(symbol);
  fetch(u).then(r => r.json()).then(d => {
    if (!d.ok) { body.innerHTML = '<div style="padding:14px;color:#f85149;font-size:12px">' + escHtml(d.error || 'could not read') + '</div>'; return; }
    // Header note for whole-file views (and truncation).
    let note = '';
    if (d.whole) {
      note = '<div class="peek-note">full file' + (d.symbol ? ' · highlighting <b>' + escHtml(d.symbol) + '</b>' : '')
           + (d.truncated ? ' · showing first ' + d.lines.length + ' of ' + d.total_lines + ' lines' : '') + '</div>';
    }
    body.innerHTML = note + d.lines.map(l =>
      '<div class="peek-line' + (l.cited ? ' peek-cited' : '') + '" id="pk-' + l.n + '">'
      + '<span class="peek-num">' + l.n + '</span>'
      + '<span class="peek-code">' + escHtml(l.text || ' ') + '</span></div>').join('');
    // Scroll the first relevant/cited line into view for whole-file peeks.
    const anchor = d.whole ? d.first_relevant : d.start;
    if (anchor) { const el = document.getElementById('pk-' + anchor); if (el) el.scrollIntoView({block:'center'}); }
    // "Open in editor" — VS Code deep link to the absolute path (+ line if known).
    const edit = document.getElementById('peek-edit');
    const jumpLine = hasLine ? d.start : (d.first_relevant || 1);
    edit.onclick = () => { window.location.href = 'vscode://file' + d.abspath + ':' + jumpLine; };
    edit.style.display = '';
  }).catch(() => { body.innerHTML = '<div style="padding:14px;color:#f85149;font-size:12px">Failed to load</div>'; });
}
function closePeek() { document.getElementById('peek-pop').style.display = 'none'; }

async function askQuestion() {
  const inp = document.getElementById('ch-input');
  const q = inp.value.trim();
  if (!q || !chState.cid || !chState.tid) return;

  // Contention nudge: a single local Ollama serializes requests. The ONLY combo
  // that contends is local-worker + local-planning (a cloud worker uses the
  // claude CLI and never touches Ollama). Fire only when: planning thread is
  // local AND the run mode is local AND a worker is actually running.
  const planMode = (document.getElementById('ch-mode')||{}).value || 'local';
  const workerIsLocal = !!(window.AGENTIC_CFG && window.AGENTIC_CFG.isLocal);
  if (planMode === 'local' && workerIsLocal && !sessionStorage.getItem('dismissContentionNudge')) {
    let workerRunning = false;
    try { workerRunning = (await (await fetch('/api/worker-status')).json()).running; } catch (e) {}
    if (workerRunning) {
      const go = confirm(
        "A worker job is running and this thread plans LOCALLY.\n\n" +
        "A single Ollama serializes requests, so this question may wait several " +
        "minutes behind the worker.\n\n" +
        "Tips: switch this thread's backend to Cloud (header dropdown) to plan " +
        "without waiting, or start Ollama with OLLAMA_NUM_PARALLEL=2.\n\n" +
        "OK = ask anyway (may be slow) · Cancel = stop and switch backend first.");
      if (!go) return;
      sessionStorage.setItem('dismissContentionNudge', '1');  // don't nag again this session
    }
  }

  inp.value = '';
  const c = document.getElementById('ch-transcript');
  const empty = document.getElementById('ch-empty'); if (empty) empty.remove();
  c.insertAdjacentHTML('beforeend', `<div class="ch-bubble user">${escHtml(q)}</div>`);
  // Live streaming bubble
  const live = document.createElement('div');
  live.className = 'ch-bubble assistant';
  live.innerHTML = '<span style="color:#6e7681">thinking…</span>';
  c.appendChild(live); c.scrollTop = c.scrollHeight;

  const dig = document.getElementById('ch-dig-cb').checked ? '1' : '0';
  const url = '/api/ask-stream?cid='+encodeURIComponent(chState.cid)+'&tid='+encodeURIComponent(chState.tid)+'&q='+encodeURIComponent(q)+'&dig='+dig;
  if (chState.askEs) chState.askEs.close();
  const es = new EventSource(url);
  chState.askEs = es;
  let toolLines = [];
  es.onmessage = (m) => {
    let ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
    const t = ev.type;
    if (t === 'tool_use') {
      toolLines.push('⚙ ' + (ev.name||'tool') + ' ' + JSON.stringify(ev.input||{}).slice(0,80));
      live.innerHTML = toolLines.map(escHtml).join('<br>');
      c.scrollTop = c.scrollHeight;
    } else if (t === 'answer_final') {
      live.outerHTML = transcriptBubble({role:'assistant', text:ev.answer, grounding:ev.grounding, turns:ev.turns, badge:ev.badge, citations:ev.citations});
      if (ev.citations && ev.citations.length) {
        // refresh the citations rail
        selectThreadRefreshCites();
      }
      c.scrollTop = c.scrollHeight;
    } else if (t === 'error') {
      live.innerHTML = '<span style="color:#f85149">'+escHtml(ev.error||'error')+'</span>';
    } else if (t === 'done') {
      es.close(); chState.askEs = null;
    }
  };
  es.onerror = () => { es.close(); chState.askEs = null; };
}

async function selectThreadRefreshCites() {
  if (!chState.cid || !chState.tid) return;
  try {
    const d = await (await fetch('/api/channel/'+chState.cid+'/'+chState.tid)).json();
    if (d.ok) renderCitations(d.citations || []);
  } catch (e) {}
}

async function reindexChannel() {
  if (!chState.cid) return;
  try {
    const d = await (await fetch('/api/channel/'+chState.cid+'/reindex', {method:'POST'})).json();
    if (d.ok && d.index) { document.getElementById('ch-index-stat').textContent = 'Index: ' + d.index.symbols + ' symbols · ' + d.index.files + ' files'; toast('Index refreshed', 'success'); }
  } catch (e) { toast('Index refresh failed', 'error'); }
}

// ── Derivation + proposal drawer ──
function deriveJobs() {
  if (!chState.cid || !chState.tid) return;
  openProp();
  document.getElementById('prop-body').innerHTML = '<div style="color:#6e7681;font-size:12px;padding:8px">Deriving jobs from the conversation…</div>';
  document.getElementById('prop-status').textContent = 'running';
  // SSE via fetch (POST) — read the stream incrementally.
  fetch('/api/channel/'+chState.cid+'/'+chState.tid+'/derive', {method:'POST'}).then(resp => {
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    function pump() {
      return reader.read().then(({done, value}) => {
        if (done) { document.getElementById('prop-status').textContent = ''; return; }
        buf += dec.decode(value, {stream:true});
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, idx); buf = buf.slice(idx+2);
          const line = chunk.replace(/^data: /, '');
          let ev; try { ev = JSON.parse(line); } catch (e) { continue; }
          if (ev.type === 'tool_use') {
            document.getElementById('prop-body').innerHTML = '<div style="color:#6e7681;font-size:12px;padding:8px">⚙ '+escHtml(ev.name||'')+' '+escHtml(JSON.stringify(ev.input||{}).slice(0,80))+'</div>';
          } else if (ev.type === 'proposal') {
            renderProposal(ev.proposal);
          } else if (ev.type === 'error') {
            document.getElementById('prop-body').innerHTML = '<div style="color:#f85149;font-size:12px;padding:8px">'+escHtml(ev.error||'error')+'</div>';
          }
        }
        return pump();
      });
    }
    return pump();
  }).catch(() => { document.getElementById('prop-status').textContent = ''; toast('Derivation failed', 'error'); });
}

function renderProposal(prop) {
  chState.prop = prop;
  document.getElementById('prop-summary').textContent = prop.summary || '';
  const jobs = prop.jobs || [];
  if (!jobs.length) { document.getElementById('prop-body').innerHTML = '<div style="color:#6e7681;font-size:12px;padding:8px">No jobs derived.</div>'; return; }
  const collapsedSet = _collapsedSet();
  let html = '';
  jobs.forEach((j, i) => {
    const depOpts = ['<option value="">— none (chain root) —</option>'].concat(
      jobs.filter(o => o.seq < j.seq).map(o => `<option value="${o.seq}"${j.depends_on===o.seq?' selected':''}>after #${o.seq+1}: ${escHtml((o.title||'').slice(0,30))}</option>`)
    ).join('');
    // Chips colored by relevance verdict: relevant=green, irrelevant=dim,
    // unknown/unverified=neutral. Title shows the verdict on hover.
    const chips = (j.anchors||[]).map(a => {
      const rv = a.relevance || 'unknown';
      const cls = rv === 'relevant' ? ' rel' : (rv === 'irrelevant' ? ' irrel' : '');
      return `<span class="prop-anchor-chip${cls}" title="relevance: ${rv}">${escHtml(a.file)}:${a.start||''}${a.end&&a.end!==a.start?'-'+a.end:''}</span>`;
    }).join('');
    // Held-back only when there's truly no resolvable anchor; show WHY.
    const heldNote = j.held_back
      ? `<div class="prop-held-flag">⚠ needs a human anchor — ${escHtml(j.held_back_reason||'no file:line resolved')}</div>`
      : (j.confidence === 'unverified'
          ? '<div class="prop-unverified">anchors found but relevance unconfirmed — review before submitting</div>'
          : '');
    const isCollapsed = collapsedSet.has(j.seq);
    html += `<div class="prop-card${j.held_back?' held-back':''}${isCollapsed?' collapsed':''}" data-seq="${j.seq}">
      <div class="prop-card-top">
        <button class="prop-collapse" onclick="toggleCard(${j.seq})" title="Collapse / expand this job">▾</button>
        <span class="prop-seq">#${j.seq+1}</span>
        <input class="prop-title" value="${escAttr(j.title||'')}">
      </div>
      <div class="prop-card-body">
        ${heldNote}
        <textarea class="prop-request">${escHtml(j.request||'')}</textarea>
        <div class="prop-anchors">${chips||'<span style="font-size:10px;color:#6e7681">no anchors</span>'}</div>
        <div class="prop-dep">depends on: <select class="prop-dep-sel">${depOpts}</select></div>
      </div>
    </div>`;
  });
  document.getElementById('prop-body').innerHTML = html;
}

function _collectProposal() {
  // Read edits back from the cards into chState.prop.
  if (!chState.prop) return null;
  const cards = document.querySelectorAll('#prop-body .prop-card');
  const jobs = [];
  cards.forEach(card => {
    const seq = parseInt(card.getAttribute('data-seq'), 10);
    const depRaw = card.querySelector('.prop-dep-sel').value;
    jobs.push({
      seq,
      title: card.querySelector('.prop-title').value,
      request: card.querySelector('.prop-request').value,
      depends_on: depRaw === '' ? null : parseInt(depRaw, 10),
      anchors: (chState.prop.jobs.find(j => j.seq === seq)||{}).anchors || [],
    });
  });
  jobs.sort((a,b) => a.seq - b.seq);
  return {...chState.prop, jobs};
}

async function submitProposal() {
  const prop = _collectProposal();
  if (!prop) return;
  // Persist edits first, then submit.
  try {
    await fetch('/api/channel/'+chState.cid+'/'+chState.tid+'/proposal/'+prop.proposal_id, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({proposal: prop})});
    const d = await (await fetch('/api/channel/'+chState.cid+'/'+chState.tid+'/submit', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({proposal_id: prop.proposal_id})})).json();
    if (d.ok) {
      const n = (d.jobs||[]).length;
      toast('Queued ' + n + ' job(s)' + (n>1?' as a chain':''), 'success');
      closeProp();
      // refresh both views
      fetchJobs();
      selectThread(chState.cid, chState.tid);
    } else toast(d.error||'Submit failed', 'error');
  } catch (e) { toast('Submit failed', 'error'); }
}

function openProp() {
  const d = document.getElementById('prop-drawer');
  d.classList.add('open'); d.classList.remove('minimized');
  document.getElementById('prop-tab').classList.remove('show');
}
function closeProp() {
  document.getElementById('prop-drawer').classList.remove('open', 'minimized');
  document.getElementById('prop-tab').classList.remove('show');
}
// Minimize: keep the proposal but tuck the drawer to an edge tab (reopenable),
// so it doesn't cover the chat while you keep talking.
function minimizeProp() {
  document.getElementById('prop-drawer').classList.remove('open');
  const n = ((chState.prop && chState.prop.jobs) || []).length;
  document.getElementById('prop-tab-count').textContent = n ? '(' + n + ')' : '';
  document.getElementById('prop-tab').classList.add('show');
}
function restoreProp() { openProp(); }

// Per-card collapse — remembers collapsed job seqs per proposal in localStorage.
function _collapseKey() { return 'propCollapse:' + ((chState.prop && chState.prop.proposal_id) || 'cur'); }
function _collapsedSet() {
  try { return new Set(JSON.parse(localStorage.getItem(_collapseKey()) || '[]')); }
  catch (e) { return new Set(); }
}
function toggleCard(seq) {
  const card = document.querySelector('#prop-body .prop-card[data-seq="' + seq + '"]');
  if (!card) return;
  const set = _collapsedSet();
  const collapsed = card.classList.toggle('collapsed');
  if (collapsed) set.add(seq); else set.delete(seq);
  localStorage.setItem(_collapseKey(), JSON.stringify([...set]));
}

fetchRepos();
fetchJobs();
// Auto-attach log stream if a worker is already running when the page loads
// (handles page refresh mid-job without requiring a manual button click)
fetch('/api/worker-status').then(r => r.json()).then(d => {
  if (d.running && !workerEs) runWorker();
}).catch(() => {});
setInterval(fetchJobs, 3000);
setInterval(fetchRepos, 30000);
setInterval(() => {
  const el = document.getElementById('age');
  if (!lastFetch) return;
  el.textContent = 'updated ' + Math.floor((Date.now() - lastFetch)/1000) + 's ago';
}, 1000);
