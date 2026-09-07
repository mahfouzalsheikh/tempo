const byId = id => document.getElementById(id);
const authenticated = document.body.dataset.authenticated === "true";
const csrfToken = document.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
const esc = value => String(value ?? "").replace(
  /[&<>"']/g,
  character => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[character]),
);
const number = value => Number(value || 0).toLocaleString();
const compactNumber = value => Intl.NumberFormat(undefined, {
  notation: Number(value || 0) > 9999 ? "compact" : "standard",
  maximumFractionDigits: 1,
}).format(Number(value || 0));
const relative = iso => {
  if (!iso) return "—";
  const date = new Date(iso);
  const seconds = Math.round((date - Date.now()) / 1000);
  if (Math.abs(seconds) < 60) return seconds > 0 ? `in ${seconds}s` : `${-seconds}s ago`;
  if (Math.abs(seconds) < 3600) {
    const minutes = Math.round(Math.abs(seconds) / 60);
    return seconds > 0 ? `in ${minutes}m` : `${minutes}m ago`;
  }
  return date.toLocaleString();
};
const emptyState = (title, copy, icon="·") => `
  <div class="empty-state"><div><span>${esc(icon)}</span><strong>${esc(title)}</strong><p>${esc(copy)}</p></div></div>
`;
const actionButton = (label, action, id, extraClass="") => `
  <button type="button" class="button small ${extraClass}" data-focus-key="${esc(id)}:${esc(action)}" data-run-action="${esc(action)}" data-run-id="${esc(id)}">${esc(label)}</button>
`;

let latestState = null;
let latestApprovals = [];
let latestAttention = [];
let dialogContext = null;
let auxiliaryStatus = authenticated ? "loading" : "signed_out";
let auxiliaryRequest = null;
let auxiliaryUpdatedAt = 0;
let stateAvailable = false;
let streaming = false;
let lastUpdatedAt = null;

const readable = value => String(value || "Waiting to start")
  .replace(/([a-z])([A-Z])/g, "$1 $2").replaceAll("_", " ");
const phaseLabel = phase => ({
  StreamingTurn: "Building", ValidatingProject: "Running checks",
  ValidationRequired: "Checks needed", LaunchingReviewAgent: "Starting review",
  ReviewingPullRequest: "Reviewing", WaitingForApproval: "Waiting for a decision",
  SafetyLimitReached: "Stopped by a safety rule", PreparingPullRequest: "Preparing pull request",
  ApplyingMergePolicy: "Checking merge requirements", HumanReviewRequired: "Needs human review",
}[phase] || readable(phase));
const checksLabel = status => ({
  pending: "Not checked", running: "Checking", passed: "Checks passed", failed: "Checks failed",
}[status] || readable(status));
const canAct = () => authenticated && stateAvailable && auxiliaryStatus === "ready";
const safeLink = value => {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : "";
  } catch { return ""; }
};

function replaceContent(id, html) {
  const container = byId(id);
  const opened = new Set([...container.querySelectorAll("details[open][data-disclosure]")]
    .map(node => node.dataset.disclosure));
  const focused = container.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
  container.innerHTML = html;
  for (const node of container.querySelectorAll("details[data-disclosure]")) {
    node.open = opened.has(node.dataset.disclosure);
  }
  if (focused) [...container.querySelectorAll("[data-focus-key]")]
    .find(node => node.dataset.focusKey === focused)?.focus({preventScroll: true});
}

function projectRows() {
  if (latestState?.projects?.length) return latestState.projects;
  const service = latestState?.service || {};
  return service.project ? [{...service, key: service.project, name: service.project.split("/").pop(),
    running: latestState.running?.length, retries: latestState.retries?.length,
    completed: latestState.completed_count}] : [];
}

function projectSetup(project) {
  if (project.configuration_error) return ["warning", "Configuration needs attention", "An edit could not be loaded. The previous configuration remains active."];
  if (project.validation_enabled === false) return ["warning", "Checks turned off", "Validation is disabled. Completed work has no required check evidence."];
  if (project.validation_policy_configured === false) return ["warning", "Required checks missing", project.publication_required
    ? "Publication runs are blocked until required checks are configured."
    : "Add required checks before using this project for publication."];
  if (project.validation_policy === "discovered") return ["warning", "Agent-selected checks", "This project allows agents to choose the checks they run."];
  if (project.validation_policy_configured === true) return ["configured", "Checks configured", "Run results will show whether the checks passed."];
  return ["unknown", "Setup status unavailable", "Open configuration to inspect this project’s setup."];
}

function renderBrief() {
  const pending = latestApprovals.length;
  const attention = latestAttention.length;
  const setup = projectRows().filter(project => projectSetup(project)[0] === "warning");
  let title, copy, label, href;
  if (authenticated && auxiliaryStatus === "signed_out") {
    [title, copy, label, href] = ["Your session has expired", "Sign in again to refresh this workspace and manage work.", "Sign in", "/login/?next=/"];
  } else if (!stateAvailable) {
    [title, copy, label, href] = ["Updates are interrupted", "The last received data is shown below. Actions are paused until the connection recovers.", "View system details", "#system-details"];
  } else if (!authenticated || auxiliaryStatus === "signed_out") {
    [title, copy, label, href] = ["You’re viewing workspace activity", "Sign in to see decisions, blocked work, and the actions available to you.", "Sign in", "/login/?next=/"];
  } else if (auxiliaryStatus !== "ready") {
    [title, copy, label, href] = [auxiliaryStatus === "loading" ? "Checking what needs you…" : "Decision inbox is unavailable", "Run activity is available. Decisions and run controls will return when operator data loads.", "View active work", "#runs"];
  } else if (pending) {
    [title, copy, label, href] = [`${pending} decision${pending === 1 ? " needs" : "s need"} your input`, "Review what the agent is requesting before approving or rejecting it.", "Review decisions", "#approvals"];
  } else if (attention) {
    [title, copy, label, href] = [`${attention} run${attention === 1 ? " needs" : "s need"} attention`, "Inspect the reason each run stopped, then choose how to continue.", "Review stopped work", "#attention"];
  } else if (setup.length) {
    [title, copy, label, href] = ["Review your project setup", `${setup.length} project${setup.length === 1 ? " has" : "s have"} missing or relaxed validation requirements.`, "Review projects", "#projects"];
  } else if (latestState?.running?.length) {
    [title, copy, label, href] = ["Work is moving forward", "No decisions or stopped runs currently need your input. Open a run to follow its progress.", "View active work", "#runs"];
  } else if (latestState?.retries?.length) {
    [title, copy, label, href] = ["Work is scheduled to try again", "No action is needed unless you want to change a retry’s priority or cancel it.", "View scheduled retries", "#queue"];
  } else {
    [title, copy, label, href] = ["No active work right now", "Tempo starts work from matching tracker issues. Review the project’s repository, labels, and checks before adding an issue.", "View project setup", "/ops/configuration/"];
  }
  byId("brief-title").textContent = title;
  byId("brief-copy").textContent = copy;
  byId("brief-action").textContent = label;
  byId("brief-action").href = href;
  byId("updated-at").textContent = lastUpdatedAt ? `Last received ${lastUpdatedAt.toLocaleTimeString()} · Counts cover this workspace; search filters active work only.` : "Waiting for the first update";
  const notice = !stateAvailable ? "Connection lost. Displayed data may be out of date; reconnecting automatically."
    : auxiliaryStatus === "error" ? "Decisions could not be refreshed. Their counts are unknown, and actions are temporarily disabled."
    : auxiliaryStatus === "signed_out" && authenticated ? "Your operator session expired. Sign in again to manage work."
    : !streaming ? "Live updates are reconnecting. This view refreshes every 10 seconds." : "";
  byId("data-notice").textContent = notice;
  byId("data-notice").hidden = !notice;
  document.querySelectorAll("[data-run-action], [data-approval-action]").forEach(button => {
    button.disabled = !canAct();
  });
}

function toast(message, kind="success") {
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  byId("toast-region").append(node);
  setTimeout(() => node.remove(), 4200);
}

async function request(url, options={}) {
  const headers = {"Accept": "application/json", ...(options.headers || {})};
  if (options.method && options.method !== "GET") {
    headers["Content-Type"] = "application/json";
    headers["X-CSRFToken"] = csrfToken;
  }
  const response = await fetch(url, {signal: AbortSignal.timeout(8000), ...options, headers, cache: "no-store"});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.message || payload.error || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function projectCard(project) {
  const key = project.key || project.project || "default/project";
  const name = project.name || key.split("/").pop();
  const initials = name.split(/[\s-]+/).slice(0, 2).map(part => part[0]).join("").toUpperCase();
  const [tone, setup, guidance] = projectSetup(project);
  return `<article class="project-card">
    <div class="project-top">
      <div class="project-identity">
        <span class="project-mark">${esc(initials || "P")}</span>
        <span><strong>${esc(name)}</strong><small>${esc(key)} · ${esc(project.environment || "default")}</small></span>
      </div>
      <span class="health-pill ${tone}">${esc(setup)}</span>
    </div>
    <p class="project-guidance">${esc(guidance)}</p>
    <a class="issue-link" href="/ops/configuration/">View configuration →</a>
    <div class="project-stats">
      <div><b>${number(project.running)}</b><span>running</span></div>
      <div><b>${number(project.retries)}</b><span>queued</span></div>
      <div><b>${number(project.completed)}</b><span>completed</span></div>
    </div>
  </article>`;
}

function runControls(row) {
  if (!authenticated || !row.run_id) return "";
  return `<div class="run-controls">
    ${actionButton("Pause", "pause", row.run_id)}
    ${actionButton("Feedback", "feedback", row.run_id)}
    ${actionButton("Priority", "reprioritize", row.run_id)}
    ${actionButton("Cancel", "cancel", row.run_id, "danger")}
  </div>`;
}

function runCard(row) {
  const session = row.session || {};
  const graph = (row.graph || []).map(node => `<div class="run-node ${esc(node.status)}" title="${esc(node.error || node.role || node.node_type)}">
    <span></span><strong>${esc(node.name || node.node_id)}</strong><small>${esc(node.role || node.node_type)} · ${esc(node.status)}</small>
  </div>`).join("");
  const commands = (session.validation_commands || []).map((command, index) => `
    <details data-disclosure="${esc(row.run_id)}:command:${index}" class="command ${Number(command.exit_code) === 0 ? "passed" : "failed"}">
      <summary data-focus-key="${esc(row.run_id)}:command:${index}"><span>${esc(command.name)}</span><code>exit ${esc(command.exit_code)}</code></summary>
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
      </div>${text}
    </article>`;
  }).join("");
  const validationStatus = session.validation_status || "pending";
  return `<details class="run-card" data-disclosure="run:${esc(row.run_id || row.issue_id)}" data-run-card="${esc(row.run_id || row.issue_id)}">
    <summary class="run-summary" data-focus-key="run:${esc(row.run_id || row.issue_id)}">
      <div class="run-primary">
        <span class="run-status"></span>
        <span><strong class="run-title">${esc(row.identifier)} · ${esc(row.title)}</strong><small class="run-subtitle">${esc(row.project || "default project")} · ${relative(row.started_at)}</small></span>
      </div>
      <div class="phase-cell"><span class="phase-label">${esc(phaseLabel(row.phase))}</span><small>turn ${number(session.turn_count)} · attempt ${number((row.attempt || 0) + 1)}</small></div>
      <span class="chip ${esc(validationStatus)}">${esc(checksLabel(validationStatus))}</span>
      <div class="token-stat"><b>${compactNumber(session.codex_total_tokens)}</b><span>tokens</span></div>
    </summary>
    <div class="run-detail">
      <section>
        <div class="detail-heading"><h3>Steps & agents</h3><span class="panel-count">${number((row.graph || []).length)} steps</span></div>
        <div class="run-graph">${graph || emptyState("Legacy workflow", "This run has no explicit graph state.", "⌁")}</div>
        <div class="detail-heading"><h3>Agent timeline</h3><span class="live-caption"><i></i> live</span></div>
        <div class="activity-feed">${events || emptyState("Waiting for activity", "The first agent event will appear here.", "↯")}</div>
        <h3>Validation evidence</h3>
        <p class="validation-copy">${esc(session.validation_summary || "Waiting for repository-native validation.")}</p>
        ${session.current_validation_command ? `<div class="current-command"><span class="spinner"></span><code>${esc(session.current_validation_command)}</code></div>` : ""}
        <div class="commands">${commands}</div>
      </section>
      <aside>
        <h3>Run context</h3>
        <dl class="mini-dl">
          <dt>Run</dt><dd>#${esc(row.run_id || "—")}</dd>
          <dt>Thread</dt><dd>${esc(session.thread_id || "—")}</dd>
          <dt>Turn</dt><dd>${esc(session.turn_id || "—")}</dd>
          <dt>Input</dt><dd>${number(session.codex_input_tokens)}</dd>
          <dt>Output</dt><dd>${number(session.codex_output_tokens)}</dd>
          <dt>Checks</dt><dd>${number(session.validation_attempt_count)} / ${number(row.max_validation_attempts)}</dd>
          <dt>Activity</dt><dd>${relative(session.last_codex_timestamp)}</dd>
        </dl>
        ${runControls(row)}
        ${safeLink(row.url) ? `<a class="issue-link" href="${esc(safeLink(row.url))}" target="_blank" rel="noreferrer">Open source issue ↗</a>` : ""}
      </aside>
    </div>
  </details>`;
}

function queueRow(row) {
  const controls = authenticated && row.run_id
    ? `<div class="queue-actions">${actionButton("Priority", "reprioritize", row.run_id)}${actionButton("Cancel", "cancel", row.run_id, "danger")}</div>`
    : "<div></div>";
  return `<div class="queue-row">
    <div><strong>${esc(row.identifier)}</strong><small>${esc(row.project || row.issue_id)}</small></div>
    <div><span class="queue-state">retry ${number(row.attempt)}</span><small>${relative(row.due_at)}</small></div>
    <div class="queue-error"><small>${esc(row.error || "Continuation checkpoint")}</small></div>
    ${controls}
  </div>`;
}

function approvalRow(approval) {
  const actions = authenticated ? `<div class="compact-actions">
    <button class="button small primary" data-focus-key="approval:${approval.id}" data-approval-action="approve" data-approval-id="${approval.id}">Review request</button>
    <button class="button small" data-approval-action="edit" data-approval-id="${approval.id}">Edit arguments</button>
    <button class="button small danger" data-approval-action="reject" data-approval-id="${approval.id}">Reject</button>
  </div>` : "";
  return `<article class="compact-row">
    <div class="compact-row-head">
      <div><strong>${esc(approval.issue)} · ${esc(approval.title)}</strong><small>${esc(approval.project || "default project")} · ${relative(approval.requested_at)}</small></div>
      <span class="chip waiting_approval">Decision needed</span>
    </div>
    <p class="request-description">${esc(typeof approval.details === "string" ? approval.details : approval.details?.message || readable(approval.kind))}</p>
    ${actions}
  </article>`;
}

function attentionRow(row) {
  let controls = "";
  if (authenticated) {
    if (row.status === "paused") controls += actionButton("Resume", "resume", row.run_id, "primary");
    if (row.phase === "SafetyLimitReached") controls += actionButton("Unblock", "unblock", row.run_id, "primary");
    controls += actionButton("Feedback", "feedback", row.run_id);
    controls += actionButton("Cancel", "cancel", row.run_id, "danger");
  }
  return `<article class="compact-row">
    <div class="compact-row-head">
      <div><strong>${esc(row.identifier)} · ${esc(row.title)}</strong><small>${esc(row.project || "default project")} · ${esc(phaseLabel(row.phase))}</small></div>
      <span class="priority">P${esc(row.priority || 5)}</span>
    </div>
    ${row.error ? `<p class="error-copy">${esc(row.error)}</p>` : ""}
    <p class="request-description">${row.status === "paused" ? "Resume when you’re ready for the agent to continue." : "Review the stop reason before unblocking. Check configuration if a required check or permission is missing."}</p>
    ${controls ? `<div class="compact-actions">${controls}</div>` : ""}
  </article>`;
}

function renderService(service) {
  const values = {
    "Control plane": service.project_count ? `${service.project_count} projects` : service.project,
    "Environment": service.environment,
    "Tracker": service.tracker_kind,
    "Workflow": service.workflow_path,
    "Workspace": service.workspace_root,
    "Poll interval": service.poll_interval_ms ? `${service.poll_interval_ms} ms` : "Per project",
    "Last poll": relative(service.last_tick_at),
    "Last error": service.workflow_error || service.last_tick_error || "None",
  };
  byId("service").innerHTML = Object.entries(values).map(([key, value]) => `
    <div><dt>${esc(key)}</dt><dd>${esc(value || "—")}</dd></div>
  `).join("");
}

function renderRuns() {
  const rows = latestState?.running || [];
  const query = byId("run-search").value.trim().toLocaleLowerCase();
  const project = byId("project-filter").value;
  const filtered = rows.filter(row => (!project || row.project === project)
    && `${row.identifier} ${row.title} ${row.project}`.toLocaleLowerCase().includes(query));
  byId("run-caption").textContent = `${filtered.length} of ${rows.length} runs`;
  byId("clear-filters").hidden = !query && !project;
  replaceContent("running", filtered.length ? filtered.map(runCard).join("")
    : emptyState(rows.length ? "No matching runs" : "No active runs",
      rows.length ? "Try a different search or clear your filters." : "Matching tracker issues will appear here when work starts.", "↯"));
  renderBrief();
}

function render(data) {
  latestState = data;
  stateAvailable = true;
  lastUpdatedAt = new Date();
  const running = Array.isArray(data.running) ? data.running : [];
  const retries = Array.isArray(data.retries) ? data.retries : [];
  const projects = projectRows();
  const totals = data.totals || {};
  const service = data.service || {};
  byId("unavailable").hidden = true;
  byId("content").hidden = false;
  byId("running-count").textContent = number(running.length);
  byId("retry-count").textContent = number(retries.length);
  byId("completed-count").textContent = number(data.completed_count);
  byId("safety-blocked-count").textContent = number(data.safety_blocked_count);
  byId("validated-count").textContent = number(totals.validated_runs);
  byId("validation-pass-count").textContent = number(totals.validation_passes);
  byId("token-count").textContent = compactNumber(totals.total_tokens);
  const capacity = Number(service.max_concurrent_agents || 0);
  byId("capacity").textContent = capacity
    ? `${running.length} of ${capacity} run slots in use` : `${running.length} active runs`;
  byId("project-count").textContent = `${projects.length} project${projects.length === 1 ? "" : "s"}`;
  replaceContent("project-grid", projects.length ? projects.map(projectCard).join("")
    : emptyState("No projects configured", "Open configuration to check the connected workflows.", "⌘"));
  const selected = byId("project-filter").value;
  const keys = [...new Set([...projects.map(project => project.key), ...running.map(row => row.project)])].filter(Boolean).sort();
  const options = '<option value="">All projects</option>' + keys.map(key => `<option value="${esc(key)}">${esc(key)}</option>`).join("");
  if (byId("project-filter").innerHTML !== options) {
    byId("project-filter").innerHTML = options;
    byId("project-filter").value = keys.includes(selected) ? selected : "";
  }
  renderRuns();
  replaceContent("retries", retries.length ? retries.map(queueRow).join("")
    : emptyState("No retries scheduled", "Work that needs another attempt will appear here.", "✓"));
  renderService(service);
  renderBrief();
  byId("status").className = "status live";
  byId("status").innerHTML = streaming ? "<i></i> Live updates" : "<i></i> Connected";
}

function renderAuxiliary() {
  const known = auxiliaryStatus === "ready";
  const pending = latestApprovals.length;
  const attention = latestAttention.length;
  byId("approval-count").textContent = known ? number(pending) : "—";
  byId("approval-caption").textContent = known ? `${pending} pending` : "Unavailable";
  byId("attention-count").textContent = known ? number(attention) : "—";
  byId("attention-caption").textContent = known ? `${attention} run${attention === 1 ? "" : "s"}` : "Unavailable";
  byId("nav-approval-count").hidden = !known || pending === 0;
  byId("nav-approval-count").textContent = number(pending);
  if (!known) {
    const signedOut = auxiliaryStatus === "signed_out";
    const title = signedOut ? "Sign in to manage work" : auxiliaryStatus === "loading" ? "Loading decisions…" : "Could not refresh decisions";
    const copy = signedOut ? "Decisions and stopped work are visible to signed-in operators." : "We’ll retry automatically. Run controls are paused until this data is available.";
    replaceContent("approvals-list", emptyState(title, copy, "◇"));
    replaceContent("attention-list", emptyState(title, copy, "!"));
  } else {
    replaceContent("approvals-list", pending ? latestApprovals.map(approvalRow).join("")
      : emptyState("No decisions waiting", "Requests for your input will appear here.", "✓"));
    replaceContent("attention-list", attention ? latestAttention.map(attentionRow).join("")
      : emptyState("No stopped work", "Paused runs and safety stops will appear here.", "✓"));
  }
  renderBrief();
}

async function loadAuxiliary(force=false) {
  if (!authenticated) { renderAuxiliary(); return; }
  if (auxiliaryRequest) return auxiliaryRequest;
  if (!force && Date.now() - auxiliaryUpdatedAt < 5000) return;
  auxiliaryRequest = (async () => {
    try {
      const [approvals, control] = await Promise.all([
        request("/api/v1/approvals"), request("/api/v1/control"),
      ]);
      latestApprovals = approvals.approvals || [];
      const approvalRuns = new Set(latestApprovals.map(row => row.run_id));
      latestAttention = (control.runs || []).filter(row => row.status !== "waiting_approval" || !approvalRuns.has(row.run_id));
      auxiliaryStatus = "ready";
    } catch (error) {
      auxiliaryStatus = error.status === 401 ? "signed_out" : "error";
    } finally {
      auxiliaryUpdatedAt = Date.now();
      renderAuxiliary();
    }
  })();
  try { await auxiliaryRequest; } finally { auxiliaryRequest = null; }
}

async function update() {
  try {
    render(await request("/api/v1/state"));
    await loadAuxiliary(true);
  } catch (error) {
    stateAvailable = false;
    if (error.status === 401) {
      auxiliaryStatus = "signed_out";
      renderAuxiliary();
    }
    byId("status").className = "status error";
    byId("status").innerHTML = "<i></i> Reconnecting";
    renderBrief();
  }
}

async function postRunAction(runId, action, payload={}) {
  if (!canAct()) throw new Error("Reconnect and sign in before managing work.");
  try {
    const requestId = globalThis.crypto?.randomUUID?.()
      || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const result = await request(`/api/v1/runs/${runId}/${action}`, {
      method: "POST",
      headers: {"Idempotency-Key": `${runId}:${action}:${requestId}`},
      body: JSON.stringify(payload),
    });
    toast(result.message || `${action} applied`);
    await update();
  } catch (error) {
    toast(error.message, "error");
    throw error;
  }
}

async function decideApproval(id, decision, args) {
  if (!canAct()) throw new Error("Reconnect and sign in before making a decision.");
  if (!latestApprovals.some(row => String(row.id) === String(id))) {
    throw new Error("This request is no longer pending. Refresh the decision inbox.");
  }
  try {
    await request(`/api/v1/approvals/${id}/decision`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        ...(args ? {arguments: args} : {}),
      }),
    });
    toast(decision === "approve" ? "Tool call approved" : "Tool call rejected");
    await update();
  } catch (error) {
    toast(error.message, "error");
    throw error;
  }
}

function openDialog(context) {
  dialogContext = context;
  byId("dialog-kicker").textContent = context.kicker || "RUN CONTROL";
  byId("dialog-title").textContent = context.title;
  byId("dialog-description").textContent = context.description || "";
  byId("dialog-context").hidden = !context.preview;
  byId("dialog-context").textContent = context.preview || "";
  byId("dialog-field-label").textContent = context.label || "Value";
  byId("dialog-field-label").hidden = Boolean(context.hideInput);
  byId("dialog-value").hidden = Boolean(context.hideInput);
  byId("dialog-value").rows = context.rows || 7;
  byId("dialog-value").value = context.value || "";
  byId("dialog-error").hidden = true;
  byId("dialog-submit").textContent = context.submit || "Apply";
  byId("dialog-submit").className = `button ${context.danger ? "danger" : "primary"}`;
  byId("action-dialog").showModal();
  if (!context.hideInput) setTimeout(() => byId("dialog-value").focus(), 50);
}

document.addEventListener("click", event => {
  if (event.target.closest("[data-dialog-close]")) {
    byId("action-dialog").close();
    dialogContext = null;
    return;
  }
  const runButton = event.target.closest("[data-run-action]");
  if (runButton) {
    const {runAction: action, runId} = runButton.dataset;
    if (action === "feedback") {
      openDialog({
        mode: "run",
        runId,
        action,
        title: "Send operator feedback",
        description: "This message is durable and will be placed at the front of the agent’s next turn.",
        label: "Feedback",
        submit: "Send feedback",
      });
    } else if (action === "reprioritize") {
      openDialog({
        mode: "run",
        runId,
        action,
        title: "Change run priority",
        description: "Lower numbers are claimed first across this project.",
        label: "Priority",
        value: "1",
        rows: 2,
        submit: "Update priority",
      });
    } else if (action === "cancel") {
      openDialog({
        mode: "run",
        runId,
        action,
        title: "Cancel this run?",
        description: "The active worker will stop and the cancellation will be written to the audit log.",
        hideInput: true,
        submit: "Cancel run",
        danger: true,
      });
    } else {
      postRunAction(runId, action, {}).catch(() => {});
    }
    return;
  }
  const approvalButton = event.target.closest("[data-approval-action]");
  if (!approvalButton) return;
  const {approvalAction: action, approvalId} = approvalButton.dataset;
  const approval = latestApprovals.find(row => String(row.id) === String(approvalId));
  if (action === "edit") {
    openDialog({
      mode: "approval",
      approvalId,
      action: "approve",
      kicker: "APPROVAL GATE",
      title: "Review and edit arguments",
      description: "Edit the JSON payload Tempo will return to the waiting tool call.",
      label: "Tool arguments (JSON)",
      value: JSON.stringify(approval?.proposed_arguments || {}, null, 2),
      rows: 12,
      submit: "Approve edited call",
    });
  } else if (action === "reject") {
    openDialog({
      mode: "approval",
      approvalId,
      action,
      kicker: "APPROVAL GATE",
      title: "Reject this tool call?",
      description: `${approval?.issue || "This run"} will receive a rejection and continue safely.`,
      hideInput: true,
      submit: "Reject call",
      danger: true,
    });
  } else {
    openDialog({
      mode: "approval", approvalId, action: "approve", kicker: "REVIEW REQUEST",
      title: approval?.title || "Review this request",
      description: `${approval?.issue || "This run"} is waiting for permission. Check the request below before approving.`,
      preview: JSON.stringify({details: approval?.details, arguments: approval?.proposed_arguments || {}}, null, 2),
      hideInput: true, submit: "Approve request",
    });
  }
});

byId("action-form").addEventListener("submit", async event => {
  event.preventDefault();
  const context = dialogContext;
  if (!context) return;
  const submit = byId("dialog-submit");
  submit.disabled = true;
  byId("dialog-error").hidden = true;
  try {
    if (context.mode === "run") {
      let payload = {};
      if (context.action === "feedback") {
        const message = byId("dialog-value").value.trim();
        if (!message) throw new Error("Feedback cannot be empty.");
        payload = {message};
      } else if (context.action === "reprioritize") {
        const priority = Number(byId("dialog-value").value);
        if (!Number.isInteger(priority) || priority < 1) {
          throw new Error("Priority must be a positive whole number.");
        }
        payload = {priority};
      }
      await postRunAction(context.runId, context.action, payload);
    } else if (context.mode === "approval") {
      let args;
      if (!context.hideInput) {
        try {
          args = JSON.parse(byId("dialog-value").value);
        } catch {
          throw new Error("Tool arguments must be valid JSON.");
        }
      }
      await decideApproval(
        context.approvalId,
        context.action === "reject" ? "reject" : "approve",
        args,
      );
    }
    byId("action-dialog").close();
    dialogContext = null;
  } catch (error) {
    byId("dialog-error").textContent = error.message;
    byId("dialog-error").hidden = false;
  } finally {
    submit.disabled = false;
  }
});

byId("refresh").addEventListener("click", async () => {
  const button = byId("refresh");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    await update();
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
});

let pendingState = null;
let renderFrame = null;
const queueRender = data => {
  pendingState = data;
  if (renderFrame) return;
  renderFrame = requestAnimationFrame(() => {
    renderFrame = null;
    render(pendingState);
    pendingState = null;
    loadAuxiliary();
  });
};
const stream = new EventSource("/api/v1/events");
stream.onopen = () => {
  streaming = true;
  renderBrief();
};
stream.onmessage = event => {
  try {
    queueRender(JSON.parse(event.data));
  } catch {
    // A malformed event will be corrected by the periodic state refresh.
  }
};
stream.onerror = () => {
  streaming = false;
  const status = byId("status");
  status.className = "status error";
  status.innerHTML = "<i></i> Live updates paused";
  renderBrief();
};

byId("run-filters").addEventListener("submit", event => event.preventDefault());
byId("run-search").addEventListener("input", renderRuns);
byId("project-filter").addEventListener("change", renderRuns);
byId("clear-filters").addEventListener("click", () => {
  byId("run-search").value = "";
  byId("project-filter").value = "";
  renderRuns();
  byId("run-search").focus();
});
renderAuxiliary();
update();
setInterval(update, 10000);
