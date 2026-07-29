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
  <button type="button" class="button small ${extraClass}" data-run-action="${esc(action)}" data-run-id="${esc(id)}">${esc(label)}</button>
`;

let latestState = null;
let latestApprovals = [];
let latestAttention = [];
let dialogContext = null;

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
  const response = await fetch(url, {...options, headers, cache: "no-store"});
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
  return `<article class="project-card">
    <div class="project-top">
      <div class="project-identity">
        <span class="project-mark">${esc(initials || "P")}</span>
        <span><strong>${esc(name)}</strong><small>${esc(key)} · ${esc(project.environment || "default")}</small></span>
      </div>
      <span class="health-pill"><i></i> healthy</span>
    </div>
    <div class="project-stats">
      <div><b>${number(project.running)}</b><span>running</span></div>
      <div><b>${number(project.retries)}</b><span>queued</span></div>
      <div><b>${number(project.completed)}</b><span>complete</span></div>
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
      </div>${text}
    </article>`;
  }).join("");
  const validationStatus = session.validation_status || "pending";
  return `<details class="run-card" data-run-card="${esc(row.run_id || row.issue_id)}">
    <summary class="run-summary">
      <div class="run-primary">
        <span class="run-status"></span>
        <span><strong class="run-title">${esc(row.identifier)} · ${esc(row.title)}</strong><small class="run-subtitle">${esc(row.project || "default project")} · ${relative(row.started_at)}</small></span>
      </div>
      <div class="phase-cell"><span class="phase-label">${esc(row.phase)}</span><small>turn ${number(session.turn_count)} · try ${number((row.attempt || 0) + 1)}</small></div>
      <span class="chip ${esc(validationStatus)}">${esc(validationStatus)}</span>
      <div class="token-stat"><b>${compactNumber(session.codex_total_tokens)}</b><span>tokens</span></div>
    </summary>
    <div class="run-detail">
      <section>
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
        ${row.url ? `<a class="issue-link" href="${esc(row.url)}" target="_blank" rel="noreferrer">Open source issue ↗</a>` : ""}
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
    <button class="button small primary" data-approval-action="approve" data-approval-id="${approval.id}">Approve</button>
    <button class="button small" data-approval-action="edit" data-approval-id="${approval.id}">Review & edit</button>
    <button class="button small danger" data-approval-action="reject" data-approval-id="${approval.id}">Reject</button>
  </div>` : "";
  return `<article class="compact-row">
    <div class="compact-row-head">
      <div><strong>${esc(approval.issue)} · ${esc(approval.title)}</strong><small>${esc(approval.project || "default project")} · ${relative(approval.requested_at)}</small></div>
      <span class="chip waiting_approval">gate</span>
    </div>
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
      <div><strong>${esc(row.identifier)} · ${esc(row.title)}</strong><small>${esc(row.project || "default project")} · ${esc(row.phase)}</small></div>
      <span class="priority">P${esc(row.priority || 5)}</span>
    </div>
    ${row.error ? `<p class="error-copy">${esc(row.error)}</p>` : ""}
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

function render(data) {
  latestState = data;
  const openRuns = new Set(
    [...document.querySelectorAll("[data-run-card][open]")].map(node => node.dataset.runCard),
  );
  const running = Array.isArray(data.running) ? data.running : [];
  const retries = Array.isArray(data.retries) ? data.retries : [];
  const projects = Array.isArray(data.projects) ? data.projects : [];
  const totals = data.totals || {};
  const service = data.service || {};
  byId("unavailable").hidden = true;
  byId("content").hidden = false;
  byId("running-count").textContent = number(running.length);
  byId("retry-count").textContent = number(retries.length);
  byId("claimed-count").textContent = number(data.claimed_count);
  byId("completed-count").textContent = number(data.completed_count);
  byId("safety-blocked-count").textContent = number(data.safety_blocked_count);
  byId("validated-count").textContent = number(totals.validated_runs);
  byId("validation-pass-count").textContent = number(totals.validation_passes);
  byId("token-count").textContent = compactNumber(totals.total_tokens);
  const capacity = Number(service.max_concurrent_agents || 0);
  byId("capacity").textContent = capacity
    ? `${Math.max(0, capacity - running.length)} of ${capacity} slots available`
    : `${running.length} leased workers`;
  byId("project-count").textContent = `${projects.length} project${projects.length === 1 ? "" : "s"}`;
  byId("project-grid").innerHTML = projects.length
    ? projects.map(projectCard).join("")
    : emptyState("No projects configured", "Add project metadata to a workflow.", "⌘");
  byId("running").innerHTML = running.length
    ? running.map(runCard).join("")
    : emptyState("No active runs", "Accepted work will appear here when a worker claims it.", "↯");
  for (const node of document.querySelectorAll("[data-run-card]")) {
    if (openRuns.has(node.dataset.runCard) || running.length === 1) node.open = true;
  }
  byId("retries").innerHTML = retries.length
    ? retries.map(queueRow).join("")
    : emptyState("Queue is clear", "There are no retries scheduled.", "✓");
  renderService(service);
}

function renderAuxiliary() {
  const pending = latestApprovals.length;
  const attention = latestAttention.length;
  byId("approval-count").textContent = number(pending);
  byId("approval-caption").textContent = `${pending} pending`;
  byId("attention-count").textContent = number(attention);
  byId("attention-caption").textContent = `${attention} run${attention === 1 ? "" : "s"}`;
  byId("nav-approval-count").hidden = pending === 0;
  byId("nav-approval-count").textContent = number(pending);
  if (!authenticated) {
    byId("approvals-list").innerHTML = emptyState("Sign in to review", "Approval details are restricted to operators.", "◇");
    byId("attention-list").innerHTML = emptyState("Sign in to intervene", "Paused and blocked run details are restricted.", "!");
    return;
  }
  byId("approvals-list").innerHTML = pending
    ? latestApprovals.map(approvalRow).join("")
    : emptyState("Inbox clear", "No tools are waiting for approval.", "✓");
  byId("attention-list").innerHTML = attention
    ? latestAttention.map(attentionRow).join("")
    : emptyState("Nothing blocked", "Paused and safety-stopped runs appear here.", "✓");
}

async function loadAuxiliary() {
  if (!authenticated) {
    latestApprovals = [];
    latestAttention = [];
    renderAuxiliary();
    return;
  }
  try {
    const [approvals, control] = await Promise.all([
      request("/api/v1/approvals"),
      request("/api/v1/control"),
    ]);
    latestApprovals = approvals.approvals || [];
    latestAttention = control.runs || [];
    renderAuxiliary();
  } catch (error) {
    if (error.status !== 401) toast(`Could not load operator state: ${error.message}`, "error");
  }
}

async function update() {
  try {
    const data = await request("/api/v1/state");
    render(data);
    await loadAuxiliary();
    const status = byId("status");
    status.className = "status live";
    status.innerHTML = "<i></i> Live";
  } catch (error) {
    const status = byId("status");
    status.className = "status error";
    status.innerHTML = "<i></i> Reconnecting";
  }
}

async function postRunAction(runId, action, payload={}) {
  if (!authenticated) {
    toast("Sign in through Django admin to intervene in runs.", "error");
    return;
  }
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
      postRunAction(runId, action).catch(() => {});
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
    decideApproval(approvalId, "approve").catch(() => {});
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
  if (authenticated) {
    try {
      await request("/api/v1/refresh", {method: "POST", body: "{}"});
      toast("Poll scheduled");
    } catch (error) {
      toast(error.message, "error");
    }
  }
  await update();
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
  const status = byId("status");
  status.className = "status live";
  status.innerHTML = "<i></i> Streaming";
};
stream.onmessage = event => {
  try {
    queueRender(JSON.parse(event.data));
  } catch {
    // A malformed event will be corrected by the periodic state refresh.
  }
};
stream.onerror = () => {
  const status = byId("status");
  status.className = "status error";
  status.innerHTML = "<i></i> Reconnecting";
};

update();
setInterval(update, 10000);
