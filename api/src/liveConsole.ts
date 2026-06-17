/**
 * Per-job "live console" page.
 *
 * Embeds the noVNC viewer (the job's liveUrl) in an iframe on the left and a
 * control sidebar on the right so the operator can read the params, click
 * Done (intervene) after filling an OTP / completing payment, and Cancel —
 * all without tabbing back to the dashboard.
 *
 * Same-origin with the worker's vnc.html (both behind Caddy on DOMAIN), so the
 * iframe embed works. Reuses the existing endpoints:
 *   GET  /api/jobs/:id/status     (poll)
 *   POST /api/jobs/:id/intervene  (Done / quick actions / free text)
 *   POST /api/jobs/:id/cancel     (Cancel)
 */
export function liveConsoleHtml(jobId: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live · ${jobId}</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { height:100%; }
  body { font-family:system-ui,sans-serif; background:#111; color:#eee; }
  .wrap { display:flex; height:100vh; }

  .stage { flex:1; min-width:0; background:#000; position:relative; }
  .stage iframe { width:100%; height:100%; border:0; display:block; }
  .stage-msg {
    position:absolute; inset:0; display:none; align-items:center; justify-content:center;
    color:#888; font-size:15px; text-align:center; padding:24px;
  }

  .side {
    width:340px; flex-shrink:0; background:#1a1a1a; border-left:1px solid #333;
    padding:16px; overflow-y:auto; display:flex; flex-direction:column; gap:14px;
  }
  .hd { display:flex; align-items:center; gap:10px; }
  .jid { font-family:monospace; font-size:12px; color:#666; word-break:break-all; }

  .badge {
    display:inline-block; padding:3px 10px; border-radius:12px; font-size:12px; font-weight:600;
    text-transform:uppercase; letter-spacing:0.5px; white-space:nowrap; background:#2a2a2a; color:#888;
  }
  .badge.running            { background:#0a3d0a; color:#4ade80; }
  .badge.queued             { background:#3d3d0a; color:#facc15; }
  .badge.done               { background:#0a2d3d; color:#60a5fa; }
  .badge.failed             { background:#3d0a0a; color:#f87171; }
  .badge.cancelled          { background:#2a2a2a; color:#888; }
  .badge.partial            { background:#2d1f0a; color:#fb923c; }
  .badge.verifyingPayment   { background:#0a2d3d; color:#60a5fa; }
  .badge.waiting_for_human  { background:#3d1f0a; color:#fb923c; animation:pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.7;} }

  .reason {
    display:none; font-size:13px; color:#fb923c; padding:10px 12px;
    background:#1f1510; border:1px solid #fb923c44; border-radius:8px;
  }

  .block { background:#141414; border:1px solid #2a2a2a; border-radius:8px; padding:12px; }
  .block-h {
    display:flex; align-items:center; justify-content:space-between;
    font-size:12px; text-transform:uppercase; letter-spacing:0.5px; color:#888; margin-bottom:10px;
  }
  .params { display:flex; flex-direction:column; gap:6px; }
  .prow { display:flex; gap:8px; font-size:13px; line-height:1.4; }
  .prow .k { color:#777; flex-shrink:0; min-width:96px; }
  .prow .v { color:#eee; font-family:monospace; word-break:break-all; }
  .copy-msg { font-size:12px; color:#4ade80; margin-top:8px; min-height:14px; }

  .mini {
    background:#222; border:1px solid #444; color:#ccc; padding:3px 9px;
    border-radius:5px; font-size:11px; cursor:pointer;
  }
  .mini:hover { background:#333; border-color:#888; }

  .intervene { display:none; flex-direction:column; gap:10px;
    padding:12px; background:#1f1510; border:1px solid #fb923c44; border-radius:8px; }
  .intervene-hint { font-size:13px; line-height:1.5; color:#fbbf77; }
  .btn.done {
    background:#fb923c; color:#111; padding:11px 16px; font-size:14px; font-weight:700;
    animation:pulse 2s infinite;
  }
  .btn.done:hover { background:#f97316; }
  #msg { font-size:13px; min-height:16px; }
  .sent { color:#4ade80; } .err { color:#f87171; }

  .foot { margin-top:auto; display:flex; flex-direction:column; gap:8px; }
  .btn {
    padding:9px 14px; border-radius:6px; font-size:13px; font-weight:600;
    text-decoration:none; text-align:center; cursor:pointer; border:none;
  }
  .btn.cancel { background:#dc2626; color:#fff; }
  .btn.cancel:hover { background:#b91c1c; }
  .btn.cancel:disabled { opacity:0.4; cursor:not-allowed; }
  .btn.ghost { background:transparent; border:1px solid #444; color:#aaa; }
  .btn.ghost:hover { border-color:#888; color:#ddd; }
</style>
</head>
<body>
<div class="wrap">
  <div class="stage">
    <iframe id="vnc" title="Live browser"></iframe>
    <div class="stage-msg" id="stageMsg"></div>
  </div>
  <aside class="side">
    <div class="hd"><span id="badge" class="badge">loading…</span></div>
    <div class="jid" id="jid"></div>
    <div class="reason" id="reason"></div>

    <div class="block">
      <div class="block-h"><span>Params</span><button class="mini" id="copyBtn">Copy payload</button></div>
      <div class="params" id="params"></div>
      <div class="copy-msg" id="copyMsg"></div>
    </div>

    <div class="intervene" id="intervene">
      <div class="intervene-hint" id="interveneHint">Click <b>Done</b> after the payment is complete in the browser on the left.</div>
      <button class="btn done" id="doneBtn">✅ Done — continue</button>
      <div id="msg"></div>
    </div>

    <div class="foot">
      <button class="btn cancel" id="cancelBtn">Cancel job</button>
      <a class="btn ghost" id="popout" target="_blank" style="display:none">Pop out raw VNC ↗</a>
    </div>
  </aside>
</div>

<script>
const JOB_ID = ${JSON.stringify(jobId)};
const CANCELABLE = ['running','queued','waiting_for_human','verifyingPayment'];
let vncSet = false;
let paramsRendered = false;

const $ = function (id) { return document.getElementById(id); };

function hintFor(reason) {
  return 'Click Done after the payment is complete in the browser on the left.';
}

function renderParams(params, taskId, source) {
  if (paramsRendered) return;
  const box = $('params');
  box.innerHTML = '';
  const keys = Object.keys(params || {});
  if (!keys.length) { box.innerHTML = '<div class="prow"><span class="v" style="color:#666">no params</span></div>'; }
  keys.forEach(function (k) {
    const row = document.createElement('div'); row.className = 'prow';
    const kk = document.createElement('span'); kk.className = 'k'; kk.textContent = k;
    const vv = document.createElement('span'); vv.className = 'v'; vv.textContent = String(params[k]);
    row.appendChild(kk); row.appendChild(vv); box.appendChild(row);
  });
  $('copyBtn').onclick = function () {
    const payload = JSON.stringify({ taskId: taskId, source: source || 'web', params: params }, null, 2);
    navigator.clipboard.writeText(payload).then(function () {
      $('copyMsg').textContent = 'Copied run payload ✓';
      setTimeout(function () { $('copyMsg').textContent = ''; }, 2000);
    }, function () {
      $('copyMsg').textContent = 'Copy failed — select manually';
    });
  };
  paramsRendered = true;
}

function renderIntervene(waiting, reason) {
  const panel = $('intervene');
  if (!waiting) { panel.style.display = 'none'; return; }
  panel.style.display = 'flex';
  $('interveneHint').textContent = hintFor(reason);
}

async function poll() {
  try {
    const res = await fetch('/api/jobs/' + JOB_ID + '/status');
    if (!res.ok) { $('badge').textContent = 'not found'; return; }
    const j = await res.json();

    $('jid').textContent = JOB_ID + '  ·  ' + (j.taskId || '');
    const badge = $('badge');
    badge.className = 'badge ' + (j.status || '');
    badge.textContent = (j.status || '?').replace(/_/g,' ');

    if (j.liveUrl && !vncSet) {
      $('vnc').src = j.liveUrl;
      const po = $('popout'); po.href = j.liveUrl; po.style.display = 'block';
      vncSet = true;
    }

    let params = {};
    try { params = JSON.parse(j.params || '{}'); } catch (e) {}
    renderParams(params, j.taskId, j.source);

    const reasonEl = $('reason');
    if (j.waitReason) { reasonEl.style.display = 'block'; reasonEl.textContent = '⏳ ' + j.waitReason; }
    else { reasonEl.style.display = 'none'; }

    renderIntervene(j.status === 'waiting_for_human', j.waitReason);

    const cancelBtn = $('cancelBtn');
    cancelBtn.disabled = !CANCELABLE.includes(j.status);

    // Terminal states: stop the VNC iframe being misleading
    if (['done','failed','cancelled','partial'].includes(j.status)) {
      const sm = $('stageMsg');
      if (!j.liveUrl) {
        $('vnc').style.display = 'none';
        sm.style.display = 'flex';
        sm.textContent = 'Job ' + j.status + ' — live view ended.';
      }
    }
  } catch (e) { /* transient network error; next tick retries */ }
}

async function submitDone() {
  const msg = $('msg');
  $('doneBtn').disabled = true;
  msg.className = ''; msg.textContent = 'Sending…';
  try {
    const res = await fetch('/api/jobs/' + JOB_ID + '/intervene', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input: 'done' }),
    });
    const data = await res.json().catch(function () { return {}; });
    if (res.ok) {
      msg.className = 'sent'; msg.textContent = 'Sent! Agent will resume shortly.';
      setTimeout(poll, 1200);
    } else {
      msg.className = 'err'; msg.textContent = data.error || 'Failed';
    }
  } catch (e) {
    msg.className = 'err'; msg.textContent = 'Network error';
  } finally {
    $('doneBtn').disabled = false;
  }
}

async function cancelJob() {
  if (!confirm('Cancel this job?')) return;
  $('cancelBtn').disabled = true;
  try { await fetch('/api/jobs/' + JOB_ID + '/cancel', { method: 'POST' }); } catch (e) {}
  setTimeout(poll, 600);
}

$('cancelBtn').onclick = cancelJob;
$('doneBtn').onclick = submitDone;

poll();
setInterval(poll, 3000);
</script>
</body>
</html>`;
}
