const byId = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const relative = iso => {
  if (!iso) return "—";
  const seconds = Math.round((new Date(iso) - Date.now()) / 1000);
  if (Math.abs(seconds) < 60) return seconds > 0 ? `in ${seconds}s` : `${-seconds}s ago`;
  return new Date(iso).toLocaleString();
};
function issueRow(row, retry=false) {
  if (retry) return `<div class="row retry">
    <div><strong>${esc(row.identifier)} · ${esc(row.title || "Queued retry")}</strong><small>${esc(row.issue_id)}</small></div>
    <div><span class="badge">retry ${esc(row.attempt)}</span><small>${esc(row.error || "continuation")}</small></div>
    <div><small>${relative(row.due_at)}</small></div>
  </div>`;
  const session = row.session || {};
  const commands = (session.validation_commands || []).map(command => `
    <details class="command ${Number(command.exit_code) === 0 ? "passed" : "failed"}">
      <summary><span>${esc(command.name)}</span><code>exit ${esc(command.exit_code)}</code></summary>
      <div class="command-line">${esc(command.command)}</div>
      <pre>${esc(command.output || "No output")}</pre>
    </details>`).join("");
  const events = (session.recent_events || []).slice().reverse().map(event => {
    const text = event.text ? `<pre>${esc(event.text)}</pre>` : "";
    return `<article class="activity ${esc(event.kind || "system")}">
      <div class="activity-head">
        <span class="activity-dot"></span>
        <strong>${esc(event.title || event.event)}</strong>
        <time>${relative(event.at)}</time>
      </div>
      ${text}
    </article>`;
  }).join("");
  const validationClass = session.validation_status === "passed" ? "passed" : session.validation_status === "failed" ? "failed" : "";
  return `<details class="run-card" data-run-id="${esc(row.issue_id)}">
    <summary class="run-summary">
      <div class="run-primary">
        <span class="pulse"></span>
        <div><strong>${esc(row.identifier)} · ${esc(row.title)}</strong><small>${esc(row.state)} · started ${relative(row.started_at)}</small></div>
      </div>
      <div><span class="badge">${esc(row.phase)}</span><small>turn ${esc(session.turn_count || 0)} · attempt ${esc(row.attempt || 1)}</small></div>
      <div class="validation-chip ${validationClass}">${esc(session.validation_status || "pending")}</div>
      <div class="token-stat"><strong>${Number(session.codex_total_tokens || 0).toLocaleString()}</strong><small>of ${Number(row.max_tokens || 0).toLocaleString()} tokens</small></div>
    </summary>
    <div class="run-detail">
      <section>
        <div class="detail-heading">
          <h3>Live agent feed</h3>
          <span class="streaming-label"><i></i> Streaming</span>
        </div>
        <div class="activity-feed">${events || '<div class="empty compact-empty">Waiting for the first agent update.</div>'}</div>
        <h3 class="section-heading">Local validation</h3>
        <p>${esc(session.validation_summary || "Waiting for the agent to discover the project’s native validation flow.")}</p>
        ${session.current_validation_command ? `<div class="current-command"><span class="spinner"></span><code>${esc(session.current_validation_command)}</code></div>` : ""}
        <div class="commands">${commands || '<div class="empty compact-empty">No validation commands have completed.</div>'}</div>
      </section>
      <aside>
        <h3>Session</h3>
        <dl class="mini-dl">
          <dt>Thread</dt><dd>${esc(session.thread_id)}</dd>
          <dt>Turn</dt><dd>${esc(session.turn_id)}</dd>
          <dt>Process</dt><dd>${esc(session.codex_app_server_pid)}</dd>
          <dt>Input</dt><dd>${Number(session.codex_input_tokens || 0).toLocaleString()}</dd>
          <dt>Output</dt><dd>${Number(session.codex_output_tokens || 0).toLocaleString()}</dd>
          <dt>Validations</dt><dd>${Number(session.validation_attempt_count || 0)} / ${Number(row.max_validation_attempts || 0)}</dd>
          <dt>Last activity</dt><dd>${relative(session.last_codex_timestamp)}</dd>
        </dl>
        ${row.url ? `<a class="issue-link" href="${esc(row.url)}" target="_blank" rel="noreferrer">Open issue ↗</a>` : ""}
      </aside>
    </div>
  </details>`;
}
function render(data) {
  const openRuns = new Set(
    [...document.querySelectorAll(".run-card[open]")].map(node => node.dataset.runId)
  );
  byId("unavailable").hidden = true;
  byId("content").hidden = false;
  const running = Array.isArray(data.running) ? data.running : [];
  const retries = Array.isArray(data.retries) ? data.retries : [];
  const totals = data.totals || {};
  const service = data.service || {};
  byId("running-count").textContent = running.length;
  byId("retry-count").textContent = retries.length;
  byId("claimed-count").textContent = Number(data.claimed_count || 0).toLocaleString();
  byId("completed-count").textContent = Number(data.completed_count || 0).toLocaleString();
  byId("safety-blocked-count").textContent = Number(data.safety_blocked_count || 0).toLocaleString();
  byId("validated-count").textContent = Number(totals.validated_runs || 0).toLocaleString();
  byId("validation-pass-count").textContent = Number(totals.validation_passes || 0).toLocaleString();
  byId("token-count").textContent = Number(totals.total_tokens || 0).toLocaleString();
  byId("capacity").textContent = `${running.length} / ${service.max_concurrent_agents || 0} slots`;
  byId("running").innerHTML = running.length ? running.map(row => issueRow(row)).join("") : '<div class="empty">No agents are running.</div>';
  for (const node of document.querySelectorAll(".run-card")) {
    if (openRuns.has(node.dataset.runId) || running.length === 1) node.open = true;
  }
  byId("retries").innerHTML = retries.length ? retries.map(row => issueRow(row, true)).join("") : '<div class="empty">The retry queue is empty.</div>';
  const fields = {
    "Tracker": service.tracker_kind,
    "Workflow": service.workflow_path,
    "Workspace root": service.workspace_root,
    "Poll interval": `${service.poll_interval_ms} ms`,
    "Last poll": relative(service.last_tick_at),
    "Last error": service.workflow_error || service.last_tick_error || "None",
  };
  byId("service").innerHTML = Object.entries(fields).map(([k,v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}
async function update() {
  try {
    const response = await fetch("/api/v1/state", {cache: "no-store"});
    if (!response.ok) throw new Error(response.status);
    render(await response.json());
    byId("status").textContent = "LIVE · POLLING";
  } catch {
    byId("status").textContent = "RECONNECTING";
  }
}
byId("refresh").addEventListener("click", async () => {
  await fetch("/api/v1/refresh", {method: "POST"});
  setTimeout(update, 250);
});
update();

let pendingState = null;
let renderFrame = null;
const queueRender = data => {
  pendingState = data;
  if (renderFrame) return;
  renderFrame = requestAnimationFrame(() => {
    renderFrame = null;
    render(pendingState);
    pendingState = null;
  });
};

const stream = new EventSource("/api/v1/events");
stream.onopen = () => {
  byId("status").textContent = "LIVE · STREAMING";
};
stream.onmessage = event => {
  try {
    queueRender(JSON.parse(event.data));
    byId("status").textContent = "LIVE · STREAMING";
  } catch {
    byId("status").textContent = "RECONNECTING";
  }
};
stream.onerror = () => {
  byId("status").textContent = "RECONNECTING";
  update();
};

// Reconcile periodically even when an intermediary leaves the SSE connection
// open but delays event delivery.
setInterval(update, 5000);
