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
  if (s === 'pending') {
    // Per-job backend toggle: flip this job between Local and Cloud while it's
    // still pending (calls /api/set-backend → changes model_hint, moves it
    // between slot pools). The button shows where it'll switch TO.
    const cur = jobBackend(j);
    const to  = cur === 'local' ? 'cloud' : 'local';
    const toLabel = to === 'cloud' ? '☁ Cloud' : '🏠 Local';
    actions.push(`<button class="btn btn-blue" title="This job runs on ${cur === 'local' ? 'Local (Ollama)' : 'Cloud (Claude)'}. Click to switch it to ${to === 'cloud' ? 'Cloud' : 'Local'} (allowed while pending)." onclick="${sp}setJobBackend('${escHtml(j.id)}','${to}')">→ ${toLabel}</button>`);
    actions.push(`<button class="btn btn-ghost" onclick="${sp}cancelJob('${escHtml(j.id)}')">Cancel</button>`);
  }
  if (s === 'running') {
    actions.push(`<button class="btn btn-amber" title="Gracefully stop this worker (SIGTERM)" onclick="${sp}stopWorker('${escHtml(j.id)}')">■ Stop</button>`);
    actions.push(`<button class="btn btn-red" onclick="${sp}abandonJob('${escHtml(j.id)}')">Abandon</button>`);
  }
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
    // When accepting this job would conflict with the (now-moved) base — e.g. a
    // sibling job touched the same lines and was accepted first — the IDE-apply
    // path becomes "Resolve merge": git apply --3way writes conflict markers you
    // resolve in your editor. A plain `git merge` accept would just abort.
    if (j.merge_conflict) {
      const cf = (j.conflict_files || []).join(', ');
      actions.push(`<button class="btn btn-amber" title="Accepting now conflicts with the current base${cf ? ' in: ' + escHtml(cf) : ''}. This applies the changes and surfaces conflict markers to resolve in your IDE." onclick="${sp}reviewJob('${escHtml(j.id)}')">⚠ Resolve merge</button>`);
    } else {
      actions.push(`<button class="btn btn-ghost" onclick="${sp}reviewJob('${escHtml(j.id)}')">Review in IDE</button>`);
    }
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
      ${(s === 'pending' || s === 'running') ? `<span class="badge-backend badge-be-${jobBackend(j)}" title="Runs on ${jobBackend(j) === 'local' ? 'Local (Ollama)' : 'Cloud (Claude)'}">${BACKEND_BADGE[jobBackend(j)]} ${jobBackend(j)}</span>` : ''}
      ${j.chain_gated ? '<span class="badge-gated" title="Chain review gate is on: this job waits until you Accept (merge) its parent.">⏸ awaiting parent review</span>' : ''}
      ${(s === 'pending' && _isQueued(j)) ? `<span class="badge-queued" title="The ${escHtml(jobBackend(j))} worker pool is full — this job waits for a free slot.">⏳ queued</span>` : ''}
      ${j.merge_conflict ? `<span class="badge-conflict" title="Accepting this now would conflict with the current base${(j.conflict_files||[]).length ? ' in: ' + escHtml((j.conflict_files||[]).join(', ')) : ''} — another job changed the same code. Use Resolve merge.">⚠ merge conflict${(j.conflict_files||[]).length ? ' · ' + escHtml(String(j.conflict_files.length)) + ' file' + (j.conflict_files.length===1?'':'s') : ''}</span>` : ''}
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
  // Backend is per-job: 'local'→local pool, 'cloud'→cloud pool. Stored as
  // model_hint (local/remote); the concrete model resolves from Settings
  // (local_model / cloud_model) at run time.
  const backend    = (document.getElementById('backend')||{}).value || 'local';
  const model_hint = backend === 'cloud' ? 'remote' : 'local';
  if (!request) { toast('Request cannot be empty', 'error'); return; }
  try {
    const r = await fetch('/api/submit', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({request, repo, priority, model_hint, after}),
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
  if (!confirm(`Merge ${mergeJobs} job(s), in chain order, into your CURRENT branch${reviewNote}.\n\nNo staging branch — the work lands directly on the branch you have checked out. A conflict stops at that job for you to resolve.\n\nProceed?`)) return;
  const r = await fetch('/api/accept-chain', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) {
    toast(`${d.accepted.length} job(s) merged into ${d.staging_branch}`, 'success');
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

/* ════════════════════════════════════════════════════════════════════════
   Worker dispatch (concurrent) — N stacked panes driven by ONE dispatch stream
   ════════════════════════════════════════════════════════════════════════
   The old single-worker model (one /api/worker-stream EventSource, "Claimed:"
   line-sniff, on-done re-fetch + runWorker(true) recursion) is gone. The server
   now runs a slot-pooled dispatcher; the browser is a pure index/renderer:

     • ONE EventSource(/api/dispatch-stream?since=<seq>) tells us when a job
       starts/ends and when a backend drains. Each `start` creates a collapsible
       pane; `end` flips its spinner to ✓/✗.
     • Each pane lazily opens its OWN EventSource(/api/worker-stream/<job_id>)
       on expand (and closes it on collapse) to tail that job's line/progress
       output. Collapsed panes cost nothing.
     • On load / refocus we (re)open the dispatch stream and rebuild a pane for
       every start that has no matching end yet (reconnect-to-all).

   Run buttons no longer drive streams directly — they POST control intents
   (/api/run-worker, /api/run-all) and let the dispatch stream reflect reality. */

const BACKEND_BADGE = { local: '🏠', cloud: '☁' };
function defaultBackend() { return IS_LOCAL ? 'local' : 'cloud'; }

let _dispEs = null;        // the single dispatch-stream EventSource
let _dispSeq = 0;          // highest dispatch seq seen (reconnect cursor)
const _panes = {};         // job_id -> { es, backend, name, done, rc, cursor }

// ── Single-job stop / stop-all ──────────────────────────────────────────────
async function stopWorker(jobId) {
  const body = jobId ? { job_id: jobId } : { all: true };
  try {
    const r = await fetch('/api/stop-worker', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d = await r.json();
    if (d.ok) toast(jobId ? 'Stopping job…' : 'Stopping all workers…', 'success');
    else toast('Stop failed: ' + (d.error || 'unknown'), 'error');
  } catch (e) { toast('Stop failed', 'error'); }
}
function stopAllWorkers() { stopWorker(); }

// ── Run intents (control only — the dispatch stream renders the result) ──────
// Single  → POST /api/run-worker; "Run All" → POST /api/run-all.
// With NO backend arg (the header buttons) we send none, which the server reads
// as "BOTH pools" — jobs carry their own backend now, so Run/Run-All means "run
// whatever's queued". An explicit backend (a card's "run on cloud") targets one.
async function runWorker(loop = false, backend) {
  openLogPanel();
  ensureDispatchStream();   // make sure we're listening before the job starts
  const url = loop ? '/api/run-all' : '/api/run-worker';
  const payload = backend ? {backend} : {};
  try {
    const r = await fetch(url, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d = await r.json();
    if (!d.ok) { toast('Run failed: ' + (d.error || 'unknown'), 'error'); return; }
    refreshPoolChip();
  } catch (e) { toast('Run failed', 'error'); }
}
// Card action: flip a PENDING job's backend (local↔cloud). Server rejects it if
// the job already started. Updates model_hint + which pool the dispatcher claims
// it from; for a chain, links can differ but order is still enforced (a child
// never runs before its parent, even on a different backend).
async function setJobBackend(id, backend) {
  try {
    const r = await fetch('/api/set-backend', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id, backend}),
    });
    const d = await r.json();
    if (!d.ok) { toast('Could not switch backend: ' + (d.error || 'unknown'), 'error'); return; }
    toast('Backend → ' + (backend === 'cloud' ? '☁ Cloud' : '🏠 Local'), 'success');
    fetchJobs();
  } catch (e) { toast('Network error', 'error'); }
}

// ── Log panel chrome (reused: resize/collapse/close from serve.py template) ──
function openLogPanel() {
  document.getElementById('log-panel').classList.add('open');
  document.body.classList.add('log-open');
  _syncDrawerWithLog(true);
  _ensurePanesContainer();
  document.getElementById('stop-btn').style.display = 'inline-block';
}

// Lazily turn #log-body into the host for #worker-panes (once). The panel's
// title/dot are repurposed as a live "N workers" summary.
function _ensurePanesContainer() {
  let host = document.getElementById('worker-panes');
  if (host) return host;
  const body = document.getElementById('log-body');
  body.innerHTML = '';
  host = document.createElement('div');
  host.id = 'worker-panes';
  body.appendChild(host);
  return host;
}

function _updatePanelSummary() {
  const ids = Object.keys(_panes);
  const running = ids.filter(id => !_panes[id].done).length;
  const title = document.getElementById('log-title');
  const dot   = document.getElementById('log-dot');
  if (running > 0) {
    title.textContent = running + ' worker' + (running === 1 ? '' : 's') + ' running…';
    dot.style.background = '#f0883e'; dot.classList.add('pulse');
  } else if (ids.length) {
    title.textContent = 'Workers idle';
    dot.style.background = '#3fb950'; dot.classList.remove('pulse');
  } else {
    title.textContent = 'Worker output';
    dot.style.background = '#3fb950'; dot.classList.remove('pulse');
  }
}

// ── The dispatch stream: one connection, an index of start/end/drained ───────
function ensureDispatchStream() {
  if (_dispEs) return;
  const es = new EventSource('/api/dispatch-stream?since=' + _dispSeq);
  _dispEs = es;
  es.onmessage = e => {
    let ev; try { ev = JSON.parse(e.data); } catch (x) { return; }
    if (typeof ev.seq === 'number') _dispSeq = Math.max(_dispSeq, ev.seq + 1);
    switch (ev.type) {
      case 'start':     onDispatchStart(ev);   break;
      case 'end':       onDispatchEnd(ev);     break;
      case 'drained':   onDispatchDrained(ev); break;
      case 'truncated':
        // The ring dropped events we hadn't seen — resync from its base, then
        // rebuild panes from authoritative server state.
        _dispSeq = ev.base_seq || 0;
        reconcilePanesFromStatus();
        break;
    }
  };
  es.onerror = () => {
    // Native EventSource auto-reconnect reuses the ORIGINAL ?since= (stale), so
    // it would replay from where we first connected. Reconnect explicitly from
    // the live cursor instead: close, drop the handle, and re-open after a short
    // backoff so the server replays only events we actually missed.
    if (_dispEs === es) {
      es.close();
      _dispEs = null;
      setTimeout(ensureDispatchStream, 1500);
    }
    refreshPoolChip();
  };
}

function onDispatchStart(ev) {
  createPane(ev.job_id, ev.backend, ev.name || ev.job_id);
  _updatePanelSummary();
  refreshPoolChip();
}

function onDispatchEnd(ev) {
  const p = _panes[ev.job_id];
  if (p) {
    p.done = true; p.rc = ev.rc;
    _markPaneDone(ev.job_id, ev.rc);
  }
  _updatePanelSummary();
  refreshPoolChip();
  fetchJobs();   // states moved (running→done/failed) — refresh the queue
}

function onDispatchDrained(ev) {
  // A backend finished its queue: reset the Run buttons to idle.
  resetRunButtons();
  refreshPoolChip();
  _updatePanelSummary();
}

// ── Panes: one collapsible <details> per running job ─────────────────────────
function createPane(jobId, backend, name) {
  let p = _panes[jobId];
  if (p && document.getElementById('wpane-' + jobId)) return p;   // already shown
  const host = _ensurePanesContainer();
  const badge = BACKEND_BADGE[backend] || '•';
  const det = document.createElement('details');
  det.className = 'wpane';
  det.id = 'wpane-' + jobId;
  det.innerHTML =
    '<summary class="wpane-head">' +
      '<span class="wpane-status spin" id="wstat-' + jobId + '">⟳</span>' +
      '<span class="wpane-badge wpane-badge-' + backend + '" title="' + escHtml(backend) + '">' + badge + '</span>' +
      '<span class="wpane-name">' + escHtml(name) + '</span>' +
      '<span class="wpane-id">' + escHtml(jobId) + '</span>' +
      '<span class="wpane-tokens" id="wtok-' + jobId + '"></span>' +
      '<button class="wpane-stop" title="Stop this worker" onclick="event.stopPropagation();stopWorker(\'' + escHtml(jobId) + '\')">■</button>' +
    '</summary>' +
    '<div class="wpane-body" id="wbody-' + jobId + '"></div>';
  // Newest pane on top.
  host.insertBefore(det, host.firstChild);
  // Lazy per-job tail: open on expand, close on collapse.
  det.addEventListener('toggle', () => {
    if (det.open) openJobStream(jobId);
    else closeJobStream(jobId);
  });
  p = _panes[jobId] = p || { es: null, backend, name, done: false, rc: null, cursor: 0 };
  p.backend = backend; p.name = name; p.es = null;   // DOM was (re)built; no live tail yet
  // If we're rebuilding the DOM for an already-finished job (e.g. reconnect
  // replayed start+end), reflect the terminal state instead of a spinner.
  if (p.done) _markPaneDone(jobId, p.rc);
  return p;
}

function _markPaneDone(jobId, rc) {
  const stat = document.getElementById('wstat-' + jobId);
  if (stat) {
    stat.classList.remove('spin');
    stat.textContent = rc === 0 ? '✓' : '✗';
    stat.classList.toggle('ok', rc === 0);
    stat.classList.toggle('bad', rc !== 0);
  }
  const stop = document.querySelector('#wpane-' + jobId + ' .wpane-stop');
  if (stop) stop.style.display = 'none';
}

// ── Per-job stream: tail line/progress for ONE job (only while expanded) ─────
function openJobStream(jobId) {
  const p = _panes[jobId];
  if (!p || p.es) return;
  const body = document.getElementById('wbody-' + jobId);
  if (body && !body.childElementCount) body.innerHTML = '';
  const es = new EventSource('/api/worker-stream/' + encodeURIComponent(jobId) + '?cursor=' + (p.cursor || 0));
  p.es = es;
  es.onmessage = e => {
    let msg; try { msg = JSON.parse(e.data); } catch (x) { return; }
    // The terminal {done} frame is synthesized server-side and is NOT part of the
    // job's _w_log buffer, so it must NOT advance the cursor. Every other frame
    // (progress AND line both live in _w_log) advances it by one, so a reconnect
    // resumes exactly where we left off without duplicating output.
    if (msg.done !== undefined) {
      p.done = true; p.rc = (msg.rc !== undefined ? msg.rc : p.rc);
      _markPaneDone(jobId, p.rc);
      _updatePanelSummary();
      closeJobStream(jobId);
      return;
    }
    p.cursor = (p.cursor || 0) + 1;
    // Live token counter for this job (the pane header).
    if (msg.progress) {
      const pr = msg.progress;
      const k = n => n >= 1000 ? (n/1000).toFixed(1).replace(/\.0$/,'') + 'k' : String(n);
      const el = document.getElementById('wtok-' + jobId);
      if (el) {
        let txt = '🔢 ' + (pr.input||0).toLocaleString() + ' / ' + (pr.output||0).toLocaleString();
        if (pr.ctx_budget) txt += ' · ctx ' + k(pr.ctx_used||0) + '/' + k(pr.ctx_budget);
        el.textContent = txt;
      }
      return;
    }
    if (msg.error) { appendPaneLine(jobId, msg.error, 'error'); return; }
    const line = msg.line || '';
    const cls  = line.startsWith('✅') || line.startsWith('✓') ? 'success'
               : line.startsWith('❌') || line.startsWith('Error') ? 'error' : '';
    appendPaneLine(jobId, line, cls);
  };
  es.onerror = () => {
    // Native auto-reconnect reuses the ORIGINAL ?cursor= (stale) — replaying old
    // lines. Reconnect explicitly from the live cursor instead, but only while
    // the pane is still open and the job isn't done.
    if (p.es === es) {
      es.close(); p.es = null;
      setTimeout(() => {
        const det = document.getElementById('wpane-' + jobId);
        if (det && det.open && !p.done) openJobStream(jobId);
      }, 1500);
    }
  };
}

function closeJobStream(jobId) {
  const p = _panes[jobId];
  if (p && p.es) { p.es.close(); p.es = null; }
}

function appendPaneLine(jobId, line, cls) {
  const body = document.getElementById('wbody-' + jobId);
  if (!body) return;
  const el = document.createElement('div');
  el.className = 'log-line' + (cls ? ' ' + cls : '');
  el.textContent = line;
  body.appendChild(el);
  body.scrollTop = body.scrollHeight;
}

// ── Reconnect-to-all: rebuild panes from authoritative server state ──────────
// Used on load/refocus and after a {truncated} resync. Any active worker with no
// pane gets one; the dispatch stream then carries it forward.
async function reconcilePanesFromStatus() {
  try {
    const d = await (await fetch('/api/worker-status')).json();
    if (!d.ok) return;
    (d.active || []).forEach(a => {
      if (!_panes[a.job_id] || !document.getElementById('wpane-' + a.job_id)) {
        openLogPanel();
        createPane(a.job_id, a.backend, a.name || a.job_id);
      }
    });
    _updatePanelSummary();
    renderPoolChip(d);
  } catch (e) {}
}

function resetRunButtons() {
  const btn = document.getElementById('run-btn');
  const allBtn = document.getElementById('run-all-btn');
  if (btn)    { btn.disabled = false; btn.textContent = '▶ Run Worker'; }
  if (allBtn) allBtn.disabled = false;
  // Keep Stop visible while any worker is still active.
  const anyRunning = Object.keys(_panes).some(id => !_panes[id].done);
  document.getElementById('stop-btn').style.display = anyRunning ? 'inline-block' : 'none';
}

// ── Header pool chip ("local 2/2 · cloud 1/4 · 3 queued") ────────────────────
function _ensurePoolChip() {
  let chip = document.getElementById('pool-chip');
  if (chip) return chip;
  chip = document.createElement('span');
  chip.id = 'pool-chip';
  chip.title = 'Worker slots in use per backend, and queued jobs the pool can\'t start yet';
  // Lives in the info row (row 1), in its dedicated slot before the age stamp.
  const slot = document.getElementById('pool-chip-slot');
  if (slot) slot.appendChild(chip);
  else {
    const age = document.getElementById('age');
    if (age && age.parentNode) age.parentNode.insertBefore(chip, age);
    else document.querySelector('header').appendChild(chip);
  }
  return chip;
}

async function refreshPoolChip() {
  try {
    const d = await (await fetch('/api/worker-status')).json();
    if (d.ok) renderPoolChip(d);
  } catch (e) {}
}

function renderPoolChip(d) {
  const chip = _ensurePoolChip();
  const pools = d.pools || {};
  const L = pools.local || {used:0,max:0};
  const C = pools.cloud || {used:0,max:0};
  const queued = (d.pending && (d.pending.local||0) + (d.pending.cloud||0)) || 0;
  const seg = (b, label, badge) => {
    const cls = b.used > 0 ? ' pc-active' : '';
    return '<span class="pc-seg' + cls + '">' + badge + ' ' + label + ' ' + b.used + '/' + b.max + '</span>';
  };
  let html = seg(L, 'local', BACKEND_BADGE.local) + '<span class="pc-dot">·</span>' + seg(C, 'cloud', BACKEND_BADGE.cloud);
  if (queued > 0) html += '<span class="pc-dot">·</span><span class="pc-queued">' + queued + ' queued</span>';
  chip.innerHTML = html;
  // The Stop button is meaningful whenever any worker is active.
  const anyActive = L.used + C.used > 0;
  const stopBtn = document.getElementById('stop-btn');
  if (stopBtn) stopBtn.style.display = anyActive ? 'inline-block' : 'none';
  // Cache pool fullness so job cards can show a "queued" pill, and re-render the
  // list if fullness changed (so pills appear/clear without waiting for poll).
  const full = { local: L.used >= L.max && (L.used + queued) > 0, cloud: C.used >= C.max };
  const changed = !_poolState || _poolState.local !== full.local || _poolState.cloud !== full.cloud;
  _poolState = full;
  if (changed) renderJobs();
}

// Which pool a job will run on, derived from its model_hint (matches the server:
// remote→cloud, local→local, auto/absent→server default mode).
let _poolState = null;   // { local:boolean, cloud:boolean } — true ⇒ pool full
function jobBackend(j) {
  const h = (j.model_hint || 'auto').toLowerCase();
  if (h === 'local')  return 'local';
  if (h === 'remote') return 'cloud';
  return defaultBackend();
}
// A pending job is "queued" (can't start now) when its backend pool is full.
function _isQueued(j) {
  return !!(_poolState && _poolState[jobBackend(j)]);
}

function closeLog() {
  document.getElementById('log-panel').classList.remove('open');
  document.body.classList.remove('log-open');
  _syncDrawerWithLog(false);
  // Closing the panel just stops watching: tear down per-job tails (they reopen
  // on expand). The dispatch stream stays open so the panel repopulates if a
  // worker is still running and the user reopens it. Workers run server-side
  // regardless of the console being visible.
  Object.keys(_panes).forEach(closeJobStream);
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
    // For a chained child this is the whole chain (parent foundation + this
    // job's delta) as one diff — every line commentable. Review comments chain
    // a review job onto THIS job, even when the commented line came from a
    // parent, so reviewing the tip of a chain reviews everything.
    const html = files.map(fileObj => renderDiffFile(fileObj, true)).join('');

    document.getElementById('diff-content').innerHTML = `<div style="padding:12px">${html}</div>`;
    renderCommentMarkers();
  } catch(e) {
    document.getElementById('diff-content').innerHTML = '<div style="padding:16px;color:#f85149">Failed to load diff</div>';
  }
}

// Render one file's split-diff. commentable=true wires line numbers with
// data-file/line/side so they accept review comments; false renders read-only
// (used for the inherited parent-foundation block).
function renderDiffFile(fileObj, commentable) {
  let diffHtml = '<div style="padding:8px;font-size:11px;color:var(--fg-faint)">No hunks.</div>';
  if (fileObj.hunks.length) {
    const tbl = fileObj.hunks.map(h => {
      const dataRows = buildSplitRows(h).map(row => {
        const L=row.l, R=row.r, lt=L?L.t:'e', rt=R?R.t:'e';
        const fn = escHtml(fileObj.name);
        const lnL = (commentable && L) ? ` data-file="${fn}" data-line="${L.n}" data-side="L"` : '';
        const lnR = (commentable && R) ? ` data-file="${fn}" data-line="${R.n}" data-side="R"` : '';
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
  return `<details ${commentable?'open':''} style="border:1px solid #21262d;border-radius:6px;margin-bottom:6px;overflow:hidden">
    <summary style="display:flex;align-items:center;gap:8px;cursor:pointer;list-style:none;padding:6px 10px;background:#161b22">
      <span style="color:#3fb950;font-size:13px">±</span>
      <span style="font-family:monospace;font-size:12px;color:#e6edf3">${escHtml(fileObj.name)}</span>
    </summary>
    ${diffHtml}
  </details>`;
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

// ── Settings panel ──
// Settings is always reachable — the panel itself holds the local/cloud switch.
document.getElementById('settings-btn').style.display = '';
let _settingsData = null;

const SETTINGS_GROUPS = [
  ['mode',    'Project & queue'],
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
  if (s.control === 'toggle') {
    const on = (s.value === true || s.value === 'true' || s.value === 1 || s.value === '1');
    return `<div style="margin-bottom:14px;display:flex;align-items:center;gap:10px">
      <div style="flex:1">${label}${help}</div>
      <label class="toggle-switch" style="position:relative;display:inline-block;width:40px;height:22px;flex:none">
        <input type="checkbox" id="set-${s.key}" ${on?'checked':''} style="opacity:0;width:0;height:0"
               onchange="const t=this.nextElementSibling,k=t.nextElementSibling;t.style.background=this.checked?'var(--accent,#3fb950)':'#30363d';k.style.transform='translateX('+(this.checked?18:0)+'px)'">
        <span class="toggle-track" style="position:absolute;inset:0;cursor:pointer;background:${on?'var(--accent,#3fb950)':'#30363d'};border-radius:22px;transition:background .15s"></span>
        <span class="toggle-knob" style="position:absolute;top:2px;left:2px;width:18px;height:18px;background:#fff;border-radius:50%;transition:transform .15s;transform:translateX(${on?18:0}px);pointer-events:none"></span>
      </label></div>`;
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
    if (s.type === 'bool') updates[s.key] = el.checked;
    else if (s.type === 'int') updates[s.key] = parseInt(el.value, 10);
    else updates[s.key] = el.value;
  });
  document.getElementById('settings-status').textContent = 'Saving…';
  fetch('/api/settings', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({settings: updates})})
    .then(r => r.json()).then(d => {
      if (!d.ok) { document.getElementById('settings-status').textContent = 'Error: ' + (d.error||'unknown'); return; }
      _settingsData.schema.forEach(s => { if (d.settings[s.key]!==undefined) s.value = d.settings[s.key]; });
      // Backend is per-job now (no global mode to switch), so saving never needs
      // a full reload — the header badge shows both pools' models live.
      document.getElementById('settings-status').textContent = '✓ Saved — applies to the next job';
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
    // If a planning run is still in flight for this thread (you navigated away
    // and came back), re-attach to it — the run never stopped, it streamed into
    // a server-side buffer. The reconnect replays tool calls + the final answer.
    reconnectAsk(cid, tid);
  } catch (e) { toast('Failed to load thread', 'error'); }
}

// Re-attach to an in-flight planning run for this thread, if any. No-op (and no
// new bubble) when nothing is running — the answer is already in the transcript.
async function reconnectAsk(cid, tid) {
  try {
    // The endpoint replies with JSON when idle and an event-stream when a run is
    // live. Probe with fetch (reading only the HEADERS) so the common idle path
    // costs nothing; abort immediately on the streaming path and let EventSource
    // own the real reconnect below.
    const ctrl = new AbortController();
    const probe = await fetch('/api/ask-reconnect?tid=' + encodeURIComponent(tid), { signal: ctrl.signal });
    const ct = probe.headers.get('Content-Type') || '';
    ctrl.abort();  // we only needed the content-type; close the socket either way
    if (ct.includes('application/json')) return;  // not running — nothing to do
  } catch (e) {
    if (e && e.name === 'AbortError') { /* expected on the streaming path */ }
    else return;
  }
  // A run is live — open a streaming reconnect and attach a fresh live bubble.
  if (chState.tid !== tid) return;  // user already moved on
  const c = document.getElementById('ch-transcript');
  const empty = document.getElementById('ch-empty'); if (empty) empty.remove();
  const live = document.createElement('div');
  live.className = 'ch-bubble assistant';
  live.innerHTML = '<span style="color:#6e7681">↻ reconnecting to in-progress answer…</span>';
  c.appendChild(live); c.scrollTop = c.scrollHeight;
  if (chState.askEs) chState.askEs.close();
  attachAskStream(new EventSource('/api/ask-reconnect?tid=' + encodeURIComponent(tid)), live, tid);
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
  // 5) Line-based block structure: headers, list items, tables, paragraphs.
  const lines = s.split('\n');
  let out = '', listType = null;
  const closeList = () => { if (listType) { out += '</' + listType + '>'; listType = null; } };
  // Split a GFM table row "| a | b |" into trimmed cells (drop leading/trailing
  // empties from the outer pipes).
  const cells = (row) => {
    let r = row.trim();
    if (r.startsWith('|')) r = r.slice(1);
    if (r.endsWith('|'))   r = r.slice(0, -1);
    return r.split('|').map(c => c.trim());
  };
  const isSep = (row) => /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/.test(row);
  for (let i = 0; i < lines.length; i++) {
    let raw = lines[i];
    const ph = raw.match(/^@@CB(\d+)@@$/);
    if (ph) { closeList(); out += blocks[+ph[1]]; continue; }
    const line = raw.trim();
    if (!line) { closeList(); continue; }
    // GFM table: a "| … |" header line immediately followed by a "|---|---|"
    // separator. Without this, table rows fall through to <p> and render as the
    // raw "| col | col |" plain text the user reported.
    if (line.indexOf('|') !== -1 && i + 1 < lines.length && isSep(lines[i + 1])) {
      closeList();
      const head = cells(line);
      let tbl = '<table class="md-table"><thead><tr>'
        + head.map(h => '<th>' + h + '</th>').join('') + '</tr></thead><tbody>';
      i += 2;  // skip header + separator
      for (; i < lines.length; i++) {
        const r = lines[i].trim();
        if (!r || r.indexOf('|') === -1) { i--; break; }  // table ends
        const cs = cells(r);
        tbl += '<tr>' + head.map((_, k) => '<td>' + (cs[k] || '') + '</td>').join('') + '</tr>';
      }
      out += tbl + '</tbody></table>';
      continue;
    }
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
  attachAskStream(new EventSource(url), live, chState.tid);
  _setPlanningRunning(true);
  refreshPoolChip();  // the planning agent took a slot — reflect it immediately
}

// Wire an ask EventSource to a live bubble. Shared by askQuestion (new run) and
// selectThread's reconnect (re-attach to an in-flight run after navigating
// away). The backend replays the whole buffer on connect, so a reconnect sees
// the tool calls + final answer just like the original stream. `tid` guards
// against a late event landing after the user switched threads again.
function attachAskStream(es, live, tid) {
  chState.askEs = es;
  const c = document.getElementById('ch-transcript');
  let toolLines = [];
  es.onmessage = (m) => {
    if (chState.tid !== tid) { es.close(); if (chState.askEs === es) chState.askEs = null; return; }
    let ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
    const t = ev.type;
    if (t === 'tool_use') {
      toolLines.push('⚙ ' + (ev.name||'tool') + ' ' + JSON.stringify(ev.input||{}).slice(0,80));
      live.innerHTML = toolLines.map(escHtml).join('<br>');
      c.scrollTop = c.scrollHeight;
    } else if (t === 'answer_final') {
      live.outerHTML = transcriptBubble({role:'assistant', text:ev.answer, grounding:ev.grounding, turns:ev.turns, badge:ev.badge, citations:ev.citations});
      if (ev.citations && ev.citations.length) selectThreadRefreshCites();
      c.scrollTop = c.scrollHeight;
      _setPlanningRunning(false);
    } else if (t === 'error') {
      live.innerHTML = '<span style="color:#f85149">'+escHtml(ev.error||'error')+'</span>';
      _setPlanningRunning(false);
    } else if (t === 'done') {
      es.close(); if (chState.askEs === es) chState.askEs = null;
      _setPlanningRunning(false);
      refreshPoolChip();  // slot released — reflect it
    }
  };
  es.onerror = () => { es.close(); if (chState.askEs === es) chState.askEs = null; _setPlanningRunning(false); refreshPoolChip(); };
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

// Stop an in-flight planning run (chat or derive) for the active thread. The
// server kills the agent subprocess; its slot frees and the stream ends.
async function cancelPlanning() {
  if (!chState.tid) return;
  try {
    await fetch('/api/ask-cancel', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tid: chState.tid})});
  } catch (e) {}
  _setPlanningRunning(false);
  refreshPoolChip();
}
// Toggle the planning Stop buttons (chat + derive) + Send disabled state.
function _setPlanningRunning(on) {
  const show = on ? 'inline-block' : 'none';
  const chStop = document.getElementById('ch-stop-btn'); if (chStop) chStop.style.display = show;
  const pStop  = document.getElementById('prop-stop-btn'); if (pStop) pStop.style.display = show;
  const send   = document.getElementById('ch-send-btn'); if (send) send.disabled = on;
}

// ── Derivation + proposal drawer ──
function deriveJobs() {
  if (!chState.cid || !chState.tid) return;
  openProp();
  document.getElementById('prop-body').innerHTML = '<div style="color:#6e7681;font-size:12px;padding:8px">Deriving jobs from the conversation…</div>';
  document.getElementById('prop-status').textContent = 'running';
  _setPlanningRunning(true);
  refreshPoolChip();  // a planning agent just took a slot — reflect it immediately
  // SSE via fetch (POST) — read the stream incrementally.
  fetch('/api/channel/'+chState.cid+'/'+chState.tid+'/derive', {method:'POST'}).then(resp => {
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    function pump() {
      return reader.read().then(({done, value}) => {
        if (done) { document.getElementById('prop-status').textContent = ''; _setPlanningRunning(false); refreshPoolChip(); return; }
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
  }).catch(() => { document.getElementById('prop-status').textContent = ''; _setPlanningRunning(false); toast('Derivation failed', 'error'); });
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

// ── Worker dispatch boot / reconnect-to-all ──────────────────────────────────
// Always open the dispatch stream (it's cheap — index events only) and rebuild a
// pane for every active worker. This handles page refresh / mid-job loads and
// concurrent workers started from another tab without any manual button click.
function bootDispatch() {
  ensureDispatchStream();
  refreshPoolChip();
  reconcilePanesFromStatus().then(() => {
    // If anything is/was running, surface the panel so the user sees it.
    if (Object.keys(_panes).length) openLogPanel();
  });
}
bootDispatch();
// On refocus, re-arm: re-attach any worker that started while we were hidden and
// nudge the stream (EventSource may have been throttled in the background).
window.addEventListener('focus', () => { ensureDispatchStream(); reconcilePanesFromStatus(); });

setInterval(fetchJobs, 3000);
setInterval(fetchRepos, 30000);
setInterval(refreshPoolChip, 5000);   // keep the pool chip + queued pills fresh
setInterval(() => {
  const el = document.getElementById('age');
  if (!lastFetch) return;
  el.textContent = 'updated ' + Math.floor((Date.now() - lastFetch)/1000) + 's ago';
}, 1000);
