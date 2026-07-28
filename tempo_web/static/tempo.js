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
  const events = (session.recent_events || []).slice(-6).reverse().map(event =>
    `<li><span>${esc(event.event)}</span><time>${relative(event.at)}</time></li>`).join("");
  const validationClass = session.validation_status === "passed" ? "passed" : session.validation_status === "failed" ? "failed" : "";
  return `<details class="run-card">
    <summary class="run-summary">
      <div class="run-primary">
        <span class="pulse"></span>
        <div><strong>${esc(row.identifier)} · ${esc(row.title)}</strong><small>${esc(row.state)} · started ${relative(row.started_at)}</small></div>
      </div>
      <div><span class="badge">${esc(row.phase)}</span><small>turn ${esc(session.turn_count || 0)} · attempt ${esc(row.attempt || 1)}</small></div>
      <div class="validation-chip ${validationClass}">${esc(session.validation_status || "pending")}</div>
      <div class="token-stat"><strong>${Number(session.codex_total_tokens || 0).toLocaleString()}</strong><small>tokens</small></div>
    </summary>
    <div class="run-detail">
      <section>
        <h3>Local validation</h3>
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
          <dt>Last activity</dt><dd>${relative(session.last_codex_timestamp)}</dd>
        </dl>
        <h3>Recent activity</h3>
        <ul class="event-list">${events || "<li><span>Waiting for Codex</span></li>"}</ul>
        ${row.url ? `<a class="issue-link" href="${esc(row.url)}" target="_blank" rel="noreferrer">Open issue ↗</a>` : ""}
      </aside>
    </div>
  </details>`;
}
function render(data) {
  byId("unavailable").hidden = true;
  byId("content").hidden = false;
  byId("running-count").textContent = data.running.length;
  byId("retry-count").textContent = data.retries.length;
  byId("validated-count").textContent = Number(data.totals.validation_passes || 0).toLocaleString();
  byId("token-count").textContent = Number(data.totals.total_tokens || 0).toLocaleString();
  byId("capacity").textContent = `${data.running.length} / ${data.service.max_concurrent_agents} slots`;
  byId("running").innerHTML = data.running.length ? data.running.map(row => issueRow(row)).join("") : '<div class="empty">No agents are running.</div>';
  byId("retries").innerHTML = data.retries.length ? data.retries.map(row => issueRow(row, true)).join("") : '<div class="empty">The retry queue is empty.</div>';
  const fields = {
    "Tracker": data.service.tracker_kind,
    "Workflow": data.service.workflow_path,
    "Workspace root": data.service.workspace_root,
    "Poll interval": `${data.service.poll_interval_ms} ms`,
    "Last poll": relative(data.service.last_tick_at),
    "Last error": data.service.workflow_error || data.service.last_tick_error || "None",
  };
  byId("service").innerHTML = Object.entries(fields).map(([k,v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}
async function update() {
  try {
    const response = await fetch("/api/v1/state", {cache: "no-store"});
    if (!response.ok) throw new Error(response.status);
    render(await response.json());
    byId("status").textContent = "LIVE";
  } catch {
    byId("status").textContent = "RECONNECTING";
  }
}
byId("refresh").addEventListener("click", async () => {
  await fetch("/api/v1/refresh", {method: "POST"});
  setTimeout(update, 250);
});
update();
setInterval(update, 2000);
