const JOB_ID = (window.AGENTIC_CFG && window.AGENTIC_CFG.jobId) || '';
let toastTimer;
function toast(msg, type) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show ' + (type || '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, 3000);
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

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

function estimateCost(model, inputTokens, outputTokens) {
  const r = {haiku:[0.80,4], sonnet:[3,15], opus:[15,75]};
  const k = Object.keys(r).find(k => (model||'').toLowerCase().includes(k));
  if (!k) return null;
  const cost = (inputTokens * r[k][0] + outputTokens * r[k][1]) / 1e6;
  return cost < 0.01 ? '<$0.01' : '$' + cost.toFixed(2);
}

function relTime(iso) {
  if (!iso) return '';
  const d = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (d < 60)    return d + 's ago';
  if (d < 3600)  return Math.floor(d/60) + 'm ago';
  if (d < 86400) return Math.floor(d/3600) + 'h ago';
  return Math.floor(d/86400) + 'd ago';
}

async function acceptJob(id, acknowledge) {
  if (!acknowledge && !confirm('Merge agentic/' + id + ' into the target repo\'s current branch?')) return;
  const r = await fetch('/api/accept', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id, acknowledge: !!acknowledge})});
  const d = await r.json();
  if (d.ok) { toast('Accepted ' + id, 'success'); setTimeout(() => { _notifyParent(); }, 1200); return; }
  // Flagged job: accept_job refused pending acknowledgement. Surface the reason
  // and let the reviewer explicitly acknowledge after inspecting the banner above.
  if ((d.error || '').includes('notable action')) {
    if (confirm('⚠️ ' + d.error + '\\n\\nYou have reviewed the flagged actions in the Agent Activity panel and want to merge anyway?')) {
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
  if (d.ok) { toast('Rejected ' + id, 'success'); setTimeout(() => { _notifyParent(); }, 1200); }
  else toast('Reject failed: ' + (d.error || 'unknown'), 'error');
}

async function abandonJob(id) {
  if (!confirm('Mark this running job as failed? (Use this to unstick a job whose worker crashed)')) return;
  const r = await fetch('/api/abandon', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast('Abandoned ' + id, 'success'); setTimeout(() => { _notifyParent(); }, 1200); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

async function reviewJob(id) {
  const r = await fetch('/api/apply-to-tree', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) toast((d.message || 'Changes applied — review in your IDE, then commit.'), 'success');
  else toast('Review failed: ' + (d.error || 'unknown'), 'error');
}

/* ── Diff cache — loaded once per page load for done/failed jobs ── */
let _diffCache = null;

async function loadDiff(state) {
  if (_diffCache !== null) return _diffCache;
  if (state !== 'done' && state !== 'failed') return '';
  try {
    const r = await fetch('/api/diff/' + encodeURIComponent(JOB_ID));
    const d = await r.json();
    _diffCache = (d.ok && d.diff) ? d.diff : '';
  } catch(e) {
    _diffCache = '';
  }
  return _diffCache;
}

function renderPage(job, activity, chain, diff) {
  const s = job._state || 'pending';
  const displayName = job.name || job.id;
  document.title = 'agentic — ' + displayName;
  document.getElementById('job-id-title').textContent = displayName;
  document.getElementById('state-badge-header').innerHTML =
    `<span class="state-badge badge-${escHtml(s)}">${escHtml(s)}</span>`;

  const actions = [];
  if (s === 'running')
    actions.push(`<button class="btn btn-red" onclick="abandonJob('${escHtml(job.id)}')">Abandon</button>`);
  if (s === 'done') {
    actions.push(`<button class="btn btn-ghost" onclick="reviewJob('${escHtml(job.id)}')">Review in IDE</button>`);
    actions.push(`<button class="btn btn-blue" onclick="acceptJob('${escHtml(job.id)}')">Accept</button>`);
  }
  if (s === 'done' || s === 'failed')
    actions.push(`<button class="btn btn-red" onclick="rejectJob('${escHtml(job.id)}')">Reject</button>`);
  document.getElementById('header-actions').innerHTML = actions.join('');

  let html = '';

  // ── 1. Agent Activity ──
  if (activity && activity.available) {
    const act = activity;
    html += `<div class="card" id="activity-card"><h2>Agent Activity</h2>`;

    // ── Anomaly banner: prominent "something's fishy" indicator ──
    if (act.is_flagged && (act.risk_flags||[]).length) {
      const labels = {network:'🌐 network', exfil:'🌐 network', destructive:'💥 destructive', sensitive_read:'🔑 secret read', oob:'📤 out-of-project access', oob_write:'📤 out-of-project write'};
      const seen = [...new Set(act.risk_flags.map(f => labels[f.risk_class] || f.risk_class))];
      html += `<div style="margin-bottom:16px;padding:12px 14px;background:#3d1416;border:1px solid #f85149;border-radius:6px">
        <div style="font-size:13px;font-weight:700;color:#ff7b72;display:flex;align-items:center;gap:8px">
          ⚠️ ${act.risk_flags.length} notable action(s) flagged — review before merging</div>
        <div style="font-size:11px;color:#f0a0a0;margin-top:6px">This agent took actions that can indicate a prompt-injection hijack: ${seen.join(', ')}. Inspect them below; merging requires acknowledgement.</div>
        <div style="margin-top:8px">`;
      act.risk_flags.forEach(f => {
        html += `<div style="font-size:11px;color:#e6edf3;padding:4px 8px;background:#010409;border-radius:4px;margin-top:4px;font-family:monospace">
          <span style="color:#ff7b72">[${escHtml(f.risk_class)}]</span> ${escHtml(f.tool)}: ${escHtml((f.detail||'').slice(0,160))}</div>`;
      });
      html += `</div></div>`;
    }

    // Stats row
    html += `<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px;padding:12px;background:#010409;border-radius:6px;border:1px solid #21262d">`;
    html += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:#e6edf3">${(act.files_modified||[]).length}</div><div style="font-size:11px;color:#8b949e">files changed</div></div>`;
    html += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:#e6edf3">${(act.tool_calls||[]).length}</div><div style="font-size:11px;color:#8b949e">tool calls</div></div>`;
    if (act.build_result) {
      const bc = act.build_result === 'passed' ? '#3fb950' : '#f85149';
      const bi = act.build_result === 'passed' ? '✓' : '✗';
      html += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:${bc}">${bi}</div><div style="font-size:11px;color:#8b949e">build</div></div>`;
    }
    if (act.lint_result) {
      const lc = act.lint_result === 'passed' ? '#3fb950' : '#f85149';
      const li = act.lint_result === 'passed' ? '✓' : '✗';
      html += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:${lc}">${li}</div><div style="font-size:11px;color:#8b949e">lint</div></div>`;
    }
    if (act.total_tokens) {
      html += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:#e6edf3">${(act.total_tokens/1000).toFixed(1)}k</div><div style="font-size:11px;color:#8b949e">tokens</div></div>`;
    }
    const estCost = estimateCost(job.model_hint || '', act.input_tokens || 0, act.output_tokens || 0);
    if (estCost !== null) {
      html += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:#e6edf3">${escHtml(estCost)}</div><div style="font-size:11px;color:#8b949e">est. cost</div></div>`;
    }
    html += `</div>`;

    // Phase summary
    const readCount   = (act.tool_calls||[]).filter(tc => tc.name === 'Read').length;
    const editCount   = (act.tool_calls||[]).filter(tc => tc.name === 'Edit' || tc.name === 'Write').length;
    const buildOk     = act.build_result === 'passed' ? '✓' : act.build_result === 'failed' ? '✗' : '—';
    const buildColor  = act.build_result === 'passed' ? '#3fb950' : act.build_result === 'failed' ? '#f85149' : '#6e7681';
    const hasCommit   = (act.tool_calls||[]).some(tc => tc.name === 'Bash' && (tc.input.command||'').includes('git commit'));
    const commitMark  = hasCommit ? '✓' : '—';
    const commitColor = hasCommit ? '#3fb950' : '#6e7681';
    html += `<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px;padding:10px 12px;background:#010409;border-radius:6px;border:1px solid #21262d;font-size:12px">
      <span style="color:#8b949e">📖 <strong style="color:#e6edf3">${readCount}</strong> reads</span>
      <span style="color:#8b949e">✏️ <strong style="color:#e6edf3">${editCount}</strong> edits</span>
      <span style="color:#8b949e">🔨 build <strong style="color:${buildColor}">${buildOk}</strong></span>
      <span style="color:#8b949e">📦 committed <strong style="color:${commitColor}">${commitMark}</strong></span>
    </div>`;

    // Files modified (split diff per file) — diff is already loaded
    const buildErrFiles = new Set(act.build_error_files || []);
    if ((act.files_modified||[]).length) {
      const byFile = {};
      if (diff) parseSplitDiff(diff).forEach(f => { byFile[f.name] = f; });
      const norm = p => p.replace(/^\.\//, '');
      html += `<div style="margin-bottom:14px">
        <div style="font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">Files Modified</div>`;
      act.files_modified.forEach(f => {
        const hasBuildErr = buildErrFiles.has(f);
        const nf = norm(f);
        const fileObj = byFile[nf] || byFile[f]
          || Object.values(byFile).find(o => norm(o.name) === nf || o.name.endsWith('/'+nf) || nf.endsWith('/'+norm(o.name)));
        let diffHtml = diff
          ? '<div style="padding:8px;font-size:11px;color:#6e7681">Diff not available for this file.</div>'
          : '<div style="padding:8px;font-size:11px;color:#6e7681">Diff not yet available (job still running).</div>';
        if (fileObj && fileObj.hunks.length) {
          const tbl = fileObj.hunks.map(h => {
            const dataRows = buildSplitRows(h).map(row => {
              const L=row.l, R=row.r, lt=L?L.t:'e', rt=R?R.t:'e';
              return `<tr>`
                + `<td class="sd-ln sd-${lt}">${L?L.n:''}</td>`
                + `<td class="sd-cell sd-${lt}">${L?escHtml(L.c):''}</td>`
                + `<td class="sd-div"></td>`
                + `<td class="sd-ln sd-${rt}">${R?R.n:''}</td>`
                + `<td class="sd-cell sd-${rt}">${R?escHtml(R.c):''}</td>`
                + `</tr>`;
            }).join('');
            return `<tr class="sd-hunk-row"><td colspan="5">${escHtml(h.header)}</td></tr>${dataRows}`;
          }).join('');
          diffHtml = `<div data-scroll-key="diff:${escHtml(f)}" style="overflow-x:auto;max-height:420px;overflow-y:auto"><table class="sd-table">${tbl}</table></div>`;
        } else if (fileObj) {
          diffHtml = '<div style="padding:8px;font-size:11px;color:#6e7681">No changes in this file.</div>';
        }
        html += `<details data-key="diff:${escHtml(f)}" style="border:1px solid #21262d;border-radius:6px;margin-bottom:6px;overflow:hidden">
          <summary style="display:flex;align-items:center;gap:8px;cursor:pointer;list-style:none;padding:6px 10px;background:#161b22">
            <span style="color:#3fb950;font-size:13px">±</span>
            <span style="font-family:monospace;font-size:12px;color:#e6edf3">${escHtml(f)}</span>
            ${hasBuildErr?'<span style="color:#f85149;font-size:11px;font-weight:600" title="Build error in this file">✗ build error</span>':''}
          </summary>
          ${diffHtml}
        </details>`;
      });
      html += `</div>`;
    }

    // Files read
    if ((act.files_read||[]).length) {
      html += `<div style="margin-bottom:14px">
        <div style="font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Files Read</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">`;
      act.files_read.forEach(f => {
        html += `<span style="font-family:monospace;font-size:11px;color:#6e7681;background:#161b22;border:1px solid #30363d;border-radius:4px;padding:2px 6px">${escHtml(f)}</span>`;
      });
      html += `</div></div>`;
    }

    // Commands run
    const cmds = (act.tool_calls||[]).filter(tc => tc.name === 'Bash');
    if (cmds.length) {
      html += `<div style="margin-bottom:14px">
        <div style="font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Commands Run</div>`;
      cmds.forEach(tc => {
        const cmd = (tc.input.command||'').trim().slice(0, 150);
        const ok  = tc.success;
        const risk = tc.risk_class;
        const riskBadge = risk ? `<span style="font-size:10px;font-weight:700;color:#fff;background:#da3633;padding:1px 6px;border-radius:8px;white-space:nowrap">⚠ ${escHtml(risk)}</span>` : '';
        html += `<div style="margin-bottom:6px">
          <div style="display:flex;align-items:center;gap:8px;padding:6px 10px;background:#010409;border-radius:4px;border-left:3px solid ${risk?'#da3633':(ok?'#3fb950':'#f85149')}">
            <span style="font-size:12px">${ok?'✓':'✗'}</span>
            <code style="font-size:12px;color:#e6edf3;word-break:break-all;flex:1">$ ${escHtml(cmd)}</code>
            ${riskBadge}
          </div>
          ${!ok && tc.output ? `<details data-key="cmd-err:${escHtml(cmd)}" style="margin-top:4px"><summary style="font-size:11px;color:#f85149;cursor:pointer;padding-left:10px">Show error output</summary><pre style="margin-top:4px;max-height:200px;overflow-y:auto;font-size:11px">${escHtml(tc.output.slice(0,2000))}</pre></details>` : ''}
        </div>`;
      });
      html += `</div>`;
    }

    // Agent reasoning
    if (act.assistant_text && act.assistant_text.trim().length > 50) {
      html += `<details data-key="agent-reasoning">
        <summary style="font-size:12px;color:#8b949e;cursor:pointer;user-select:none;padding:6px 0">
          Agent reasoning (${act.assistant_text.length.toLocaleString()} chars)
        </summary>
        <pre data-scroll-key="agent-reasoning" style="margin-top:8px;max-height:400px;overflow-y:auto;font-size:12px;white-space:pre-wrap">${escHtml(act.assistant_text.slice(0,8000))}</pre>
      </details>`;
    }

    // Token detail
    if (act.input_tokens || act.output_tokens) {
      html += `<div style="margin-top:12px;padding-top:12px;border-top:1px solid #21262d;font-size:12px;color:#6e7681">
        ${act.input_tokens.toLocaleString()} input + ${act.output_tokens.toLocaleString()} output = <strong style="color:#8b949e">${act.total_tokens.toLocaleString()}</strong> total tokens
      </div>`;
    }

    html += `</div>`; // close activity card
  }

  // ── 2. Request ──
  html += `<div class="card"><h2>Request</h2><pre>${escHtml(job.request || '')}</pre></div>`;

  // ── 3. State History ──
  const hist = job.state_history || [];
  if (hist.length) {
    html += `<div class="card"><h2>State History</h2><div class="timeline">`;
    hist.forEach(entry => {
      const st = entry.state || '';
      html += `<div class="tl-entry">
        <div class="tl-dot tl-dot-${escHtml(st)}"></div>
        <div class="tl-body">
          <div class="tl-state">${escHtml(st)}</div>
          <div class="tl-meta">${escHtml(entry.at || '')}${entry.at ? ' · ' + relTime(entry.at) : ''}${entry.worker ? ' · ' + escHtml(entry.worker) : ''}</div>
        </div>
      </div>`;
    });
    html += `</div></div>`;
  }

  // ── 4. Chain visualizer ──
  if (chain && chain.ok) {
    const hasParent = chain.parent !== null;
    const hasChildren = (chain.children || []).length > 0;
    if (hasParent || hasChildren) {
      html += `<div class="card"><h2>Job Chain</h2><div class="chain-flow">`;
      if (hasParent) {
        const p = chain.parent;
        html += `<a class="chain-chip" href="/job/${escHtml(p.id)}" title="${escHtml(p.request||'')}">↑ ${escHtml(p.name || p.id)}</a>`;
        html += `<span class="chain-arrow">→</span>`;
      }
      html += `<span class="chain-chip current">${escHtml(job.name || job.id)}</span>`;
      if (hasChildren) {
        chain.children.forEach(c => {
          html += `<span class="chain-arrow">→</span>`;
          html += `<a class="chain-chip" href="/job/${escHtml(c.id)}" title="${escHtml(c.request||'')}">↓ ${escHtml(c.name || c.id)}</a>`;
        });
      }
      html += `</div></div>`;
    }
  }

  // ── 5. Session tasks (legacy) ──
  const sess = job.session;
  if (sess && sess.tasks && sess.tasks.tasks && sess.tasks.tasks.length) {
    html += `<div class="card"><h2>Tasks</h2>`;
    sess.tasks.tasks.forEach(task => {
      const taskKey = 'task_' + String(task.id).padStart(3, '0');
      const actionCls = 'action-' + (task.action || '').toUpperCase();
      const outputText = sess.outputs && sess.outputs[taskKey];
      const lineCount = outputText ? outputText.split('\n').length : 0;
      html += `<div class="task-card">
        <div class="task-header">
          <span class="task-id">#${escHtml(String(task.id))}</span>
          <span class="action-badge ${escHtml(actionCls)}">${escHtml((task.action || '').toUpperCase())}</span>
          <span class="task-file">${escHtml(task.file_path || task.path || '')}</span>
        </div>
        ${task.description ? `<div class="task-desc">${escHtml(task.description)}</div>` : ''}
        ${task.modification_type ? `<div class="task-modtype">${escHtml(task.modification_type)}</div>` : ''}
        ${outputText ? `<details data-key="task-out:${escHtml(String(task.id))}"><summary>View output (${lineCount} lines)</summary><pre>${escHtml(outputText)}</pre></details>` : ''}
      </div>`;
    });
    html += `</div>`;
  }

  // ── 6. Token Usage (legacy) ──
  if (sess && sess.usage && Object.keys(sess.usage).length) {
    html += `<div class="card"><h2>Token Usage</h2>
      <table>
        <thead><tr><th>Step</th><th>Input</th><th>Output</th><th>Cache Read</th><th>Cache Write</th><th>Total</th></tr></thead>
        <tbody>`;
    let totIn = 0, totOut = 0, totCR = 0, totCW = 0;
    Object.entries(sess.usage).forEach(([key, u]) => {
      const label = key.replace(/_usage$/, '');
      const inp  = u.input_tokens || 0;
      const out  = u.output_tokens || 0;
      const cr   = u.cache_read_input_tokens || 0;
      const cw   = u.cache_creation_input_tokens || 0;
      const tot  = inp + out + cr + cw;
      totIn += inp; totOut += out; totCR += cr; totCW += cw;
      html += `<tr>
        <td>${escHtml(label)}</td>
        <td>${inp.toLocaleString()}</td>
        <td>${out.toLocaleString()}</td>
        <td>${cr.toLocaleString()}</td>
        <td>${cw.toLocaleString()}</td>
        <td>${tot.toLocaleString()}</td>
      </tr>`;
    });
    const totAll = totIn + totOut + totCR + totCW;
    html += `</tbody>
        <tfoot><tr class="totals-row">
          <td>TOTAL</td>
          <td>${totIn.toLocaleString()}</td>
          <td>${totOut.toLocaleString()}</td>
          <td>${totCR.toLocaleString()}</td>
          <td>${totCW.toLocaleString()}</td>
          <td>${totAll.toLocaleString()}</td>
        </tr></tfoot>
      </table></div>`;
  }

  // ── 7. Validation ──
  if (sess && (sess.validation_issues || sess.validation_warnings)) {
    html += `<div class="card"><h2>Validation</h2>`;
    if (sess.validation_issues)
      html += `<div class="validation-issues"><div class="validation-label">Issues</div><pre>${escHtml(sess.validation_issues)}</pre></div>`;
    if (sess.validation_warnings)
      html += `<div class="validation-warnings"><div class="validation-label">Warnings</div><pre>${escHtml(sess.validation_warnings)}</pre></div>`;
    html += `</div>`;
  }

  // ── 8. AI Review ──
  if (sess && sess.review) {
    html += `<div class="card"><h2>AI Review</h2><pre>${escHtml(sess.review)}</pre></div>`;
  }

  document.getElementById('main-content').innerHTML = html;
  scheduleRefresh(s);
}

function saveUIState() {
  const container = document.getElementById('main-content');
  const openDetails = Array.from(container.querySelectorAll('details[open]'))
    .map(el => el.dataset.key || el.querySelector('summary')?.textContent?.trim() || '');
  // Inner scroll containers (reasoning <pre>, diff tables, output) keep their own
  // scroll. Save each by a stable key; record whether it was pinned to the bottom
  // so a streaming log stays at the bottom instead of jumping to a stale offset.
  const scrolls = {};
  container.querySelectorAll('[data-scroll-key]').forEach(el => {
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    scrolls[el.dataset.scrollKey] = { top: el.scrollTop, atBottom };
  });
  return { openDetails, scrollTop: container.scrollTop, scrolls };
}

function restoreUIState(state) {
  if (!state) return;
  const container = document.getElementById('main-content');
  container.scrollTop = state.scrollTop;
  container.querySelectorAll('details').forEach(el => {
    const key = el.dataset.key || el.querySelector('summary')?.textContent?.trim() || '';
    if (state.openDetails.includes(key)) el.open = true;
  });
  const scrolls = state.scrolls || {};
  container.querySelectorAll('[data-scroll-key]').forEach(el => {
    const s = scrolls[el.dataset.scrollKey];
    if (!s) return;
    el.scrollTop = s.atBottom ? el.scrollHeight : s.top;
  });
}

async function loadJob() {
  const uiState = saveUIState();
  try {
    const r = await fetch('/api/job-full/' + encodeURIComponent(JOB_ID));
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      document.getElementById('main-content').innerHTML =
        `<div id="error-msg">Job not found: ${escHtml(d.error || r.status)}</div>`;
      return;
    }
    const data = await r.json();
    const diff = await loadDiff(data.job._state);
    renderPage(data.job, data.activity, data.chain, diff);
    restoreUIState(uiState);
  } catch(e) {
    document.getElementById('main-content').innerHTML =
      `<div id="error-msg">Failed to load job: ${escHtml(String(e))}</div>`;
  }
}

let _refreshTimer = null;
function scheduleRefresh(state) {
  if (_refreshTimer) { clearTimeout(_refreshTimer); _refreshTimer = null; }
  if (state === 'running') {
    _refreshTimer = setTimeout(() => loadJob(), 5000);
  }
}
window.addEventListener('beforeunload', () => {
  if (_refreshTimer) clearTimeout(_refreshTimer);
});

loadJob();

// If loaded inside the detail drawer, refresh parent job list after actions
function _notifyParent() {
  if (window.parent && window.parent !== window && window.parent.fetchJobs) {
    window.parent.fetchJobs();
    window.parent.closeDrawer();
  } else {
    location.href = '/';
  }
}
