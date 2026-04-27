export const DASHBOARD_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Dashboard</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:system-ui,sans-serif; background:#111; color:#eee; padding:24px; }
  h1 { margin-bottom:4px; }
  .header { display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; }
  .meta { color:#888; font-size:13px; }
  .stats { display:flex; gap:16px; font-size:13px; color:#888; }
  .stats span { background:#1a1a1a; padding:4px 10px; border-radius:6px; }
  .grid { display:flex; flex-direction:column; gap:10px; }

  .job {
    background:#1a1a1a; border:1px solid #333; border-radius:8px; padding:16px 18px;
    transition: border-color 0.2s;
  }
  .job.waiting { border-color:#fb923c; }
  .job-top {
    display:flex; align-items:center; justify-content:space-between; gap:12px;
  }
  .left { flex:1; min-width:0; }
  .task { font-weight:600; font-size:15px; }
  .id { font-size:11px; color:#555; font-family:monospace; margin-top:2px; word-break:break-all; }
  .params { font-size:13px; color:#aaa; margin-top:4px; }
  .time { font-size:11px; color:#555; margin-top:3px; }

  .badge {
    display:inline-block; padding:3px 10px; border-radius:12px; font-size:12px; font-weight:600;
    text-transform:uppercase; letter-spacing:0.5px; white-space:nowrap;
  }
  .badge.running            { background:#0a3d0a; color:#4ade80; }
  .badge.queued             { background:#3d3d0a; color:#facc15; }
  .badge.done               { background:#0a2d3d; color:#60a5fa; }
  .badge.failed             { background:#3d0a0a; color:#f87171; }
  .badge.cancelled          { background:#2a2a2a; color:#888; }
  .badge.waiting_for_human  { background:#3d1f0a; color:#fb923c; animation:pulse 2s infinite; }

  @keyframes pulse {
    0%,100% { opacity:1; }
    50% { opacity:0.7; }
  }

  .actions { display:flex; gap:8px; align-items:center; flex-shrink:0; }
  a.btn, button.btn {
    padding:6px 14px; border-radius:6px; font-size:13px; text-decoration:none;
    cursor:pointer; border:none; font-weight:500; transition:background 0.15s;
  }
  a.btn        { background:#2563eb; color:#fff; }
  a.btn:hover  { background:#1d4ed8; }
  button.cancel       { background:#dc2626; color:#fff; }
  button.cancel:hover { background:#b91c1c; }

  /* Intervention panel */
  .intervene {
    margin-top:12px; padding:12px 14px; background:#1f1510; border:1px solid #fb923c44;
    border-radius:8px;
  }
  .intervene-reason {
    font-size:13px; color:#fb923c; margin-bottom:10px; display:flex; align-items:start; gap:6px;
  }
  .intervene-reason .icon { font-size:16px; flex-shrink:0; }
  .intervene-form { display:flex; gap:8px; }
  .intervene-form input {
    flex:1; background:#111; border:1px solid #444; color:#eee; padding:8px 12px;
    border-radius:6px; font-size:14px; outline:none;
  }
  .intervene-form input:focus { border-color:#fb923c; }
  .intervene-form input::placeholder { color:#666; }
  .intervene-form button {
    background:#fb923c; color:#111; border:none; padding:8px 18px; border-radius:6px;
    font-size:13px; font-weight:600; cursor:pointer; white-space:nowrap;
  }
  .intervene-form button:hover { background:#f97316; }
  .intervene-form button:disabled { opacity:0.5; cursor:not-allowed; }
  .intervene-sent { font-size:13px; color:#4ade80; margin-top:8px; }
  .intervene-error { font-size:13px; color:#f87171; margin-top:8px; }

  .quick-btns { display:flex; gap:6px; margin-bottom:8px; flex-wrap:wrap; }
  .quick-btns button {
    background:#222; border:1px solid #444; color:#ccc; padding:4px 10px;
    border-radius:4px; font-size:12px; cursor:pointer;
  }
  .quick-btns button:hover { background:#333; border-color:#888; }

  .empty { color:#666; text-align:center; padding:40px; }
  .result-preview { font-size:12px; color:#4ade80; margin-top:4px; max-width:500px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .error-preview { font-size:12px; color:#f87171; margin-top:4px; max-width:500px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>Agent Dashboard</h1>
    <p class="meta">Auto-refreshes every 3s</p>
  </div>
  <div class="stats" id="stats"></div>
</div>
<div id="grid" class="grid"><div class="empty">Loading...</div></div>

<script>
const sentJobs = new Set();

function timeAgo(iso) {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + 'm ago';
  const hrs = Math.floor(mins / 60);
  return hrs + 'h ' + (mins % 60) + 'm ago';
}

function detectType(reason) {
  if (!reason) return 'text';
  const r = reason.toLowerCase();
  if (r.includes('otp')) return 'otp';
  if (r.includes('captcha')) return 'captcha';
  return 'text';
}

function placeholder(type) {
  if (type === 'otp') return 'Enter OTP (e.g. 143562)';
  if (type === 'captcha') return 'Type what you see or "done" after solving in browser';
  return 'Type your response...';
}

function quickActions(type) {
  const common = [{label:'done', value:'done'}];
  if (type === 'captcha') {
    return [{label:"I solved it in browser", value:"done"}, ...common];
  }
  return common;
}

async function refresh() {
  try {
    const res = await fetch('/api/jobs');
    const { jobs } = await res.json();
    const grid = document.getElementById('grid');
    const stats = document.getElementById('stats');

    if (!jobs.length) {
      grid.innerHTML = '<div class="empty">No jobs yet</div>';
      stats.innerHTML = '';
      return;
    }

    const counts = {};
    jobs.forEach(j => { counts[j.status] = (counts[j.status]||0) + 1; });
    stats.innerHTML = Object.entries(counts)
      .map(([s,n]) => '<span>' + n + ' ' + s.replace(/_/g,' ') + '</span>').join('');

    grid.innerHTML = jobs.map(j => {
      const params = (() => { try { return JSON.parse(j.params || '{}'); } catch { return {}; } })();
      const paramStr = Object.entries(params).map(([k,v]) => k + '=' + v).join(', ');
      const isWaiting = j.status === 'waiting_for_human';
      const canCancel = ['running','queued','waiting_for_human'].includes(j.status);
      const hasLive = j.liveUrl && ['running','waiting_for_human'].includes(j.status);
      const type = detectType(j.waitReason);
      const wasSent = sentJobs.has(j.id);

      let html = '<div class="job' + (isWaiting ? ' waiting' : '') + '">';
      html += '<div class="job-top">';
      html += '<div class="left">';
      html += '<div class="task">' + (j.taskId||'?') + '</div>';
      html += '<div class="id">' + j.id + '</div>';
      if (paramStr) html += '<div class="params">' + paramStr + '</div>';
      html += '<div class="time">' + timeAgo(j.createdAt) + '</div>';
      if (j.status === 'done' && j.result) {
        html += '<div class="result-preview" title="' + j.result.replace(/"/g,'&quot;') + '">' + j.result.substring(0,120) + '</div>';
      }
      if (j.status === 'failed' && j.error) {
        html += '<div class="error-preview">' + j.error.substring(0,120) + '</div>';
      }
      html += '</div>'; // .left

      html += '<div class="actions">';
      html += '<span class="badge ' + j.status + '">' + j.status.replace(/_/g,' ') + '</span>';
      if (hasLive) html += '<a class="btn" href="' + j.liveUrl + '" target="_blank">Live</a>';
      if (canCancel) html += '<button class="btn cancel" onclick="cancelJob(\\'' + j.id + '\\')">Cancel</button>';
      html += '</div>'; // .actions
      html += '</div>'; // .job-top

      if (isWaiting) {
        html += '<div class="intervene">';
        html += '<div class="intervene-reason"><span class="icon">⏳</span><span>' + (j.waitReason || 'Waiting for human input') + '</span></div>';

        const qas = quickActions(type);
        html += '<div class="quick-btns">';
        qas.forEach(qa => {
          html += '<button onclick="submitInput(\\'' + j.id + '\\', \\'' + qa.value + '\\')">' + qa.label + '</button>';
        });
        html += '</div>';

        html += '<div class="intervene-form">';
        html += '<input type="text" id="input-' + j.id + '" placeholder="' + placeholder(type) + '" '
              + 'onkeydown="if(event.key===\\'Enter\\')submitInput(\\'' + j.id + '\\')" '
              + (type === 'otp' ? 'inputmode="numeric" pattern="[0-9]*" autocomplete="one-time-code"' : '')
              + '>';
        html += '<button id="btn-' + j.id + '" onclick="submitInput(\\'' + j.id + '\\')">Send</button>';
        html += '</div>';
        html += '<div id="msg-' + j.id + '"></div>';
        html += '</div>'; // .intervene
      }

      html += '</div>'; // .job
      return html;
    }).join('');

  } catch(e) { console.error('refresh error', e); }
}

async function submitInput(jobId, quickValue) {
  const inputEl = document.getElementById('input-' + jobId);
  const btnEl = document.getElementById('btn-' + jobId);
  const msgEl = document.getElementById('msg-' + jobId);
  const value = quickValue || (inputEl ? inputEl.value.trim() : '');

  if (!value) {
    if (inputEl) inputEl.focus();
    return;
  }

  if (btnEl) btnEl.disabled = true;
  if (msgEl) msgEl.innerHTML = '<span style="color:#888">Sending...</span>';

  try {
    const res = await fetch('/api/jobs/' + jobId + '/intervene', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input: value }),
    });
    const data = await res.json();

    if (res.ok) {
      sentJobs.add(jobId);
      if (msgEl) msgEl.innerHTML = '<span class="intervene-sent">Sent! Agent will resume shortly.</span>';
      if (inputEl) inputEl.value = '';
      setTimeout(refresh, 1500);
    } else {
      if (msgEl) msgEl.innerHTML = '<span class="intervene-error">' + (data.error || 'Failed') + '</span>';
      if (btnEl) btnEl.disabled = false;
    }
  } catch(e) {
    if (msgEl) msgEl.innerHTML = '<span class="intervene-error">Network error</span>';
    if (btnEl) btnEl.disabled = false;
  }
}

async function cancelJob(id) {
  if (!confirm('Cancel this job?')) return;
  await fetch('/api/jobs/' + id + '/cancel', { method: 'POST' });
  refresh();
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>`;
