const byId = id => document.getElementById(id);
const csrfToken = document.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
const authenticated = document.body.dataset.authenticated === "true";
const esc = value => String(value ?? "—").replace(
  /[&<>"']/g,
  character => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[character]),
);
const pretty = value => Array.isArray(value)
  ? value.join(", ") || "None"
  : typeof value === "object" && value !== null ? JSON.stringify(value) : value;
const definitionList = (id, values) => {
  const node = byId(id);
  if (node) {
    node.innerHTML = Object.entries(values).map(
      ([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(pretty(value))}</dd>`,
    ).join("");
  }
};
const projectCard = project => {
  const service = project.service || {};
  const runtime = project.runtime || {};
  const tracker = project.tracker || {};
  const key = service.project || tracker.repository || "default/project";
  const name = key.split("/").pop();
  const initials = name.split(/[\s-]+/).slice(0, 2).map(part => part[0]).join("").toUpperCase();
  return `<article class="project-card">
    <div class="project-top">
      <div class="project-identity">
        <span class="project-mark">${esc(initials)}</span>
        <span><strong>${esc(name)}</strong><small>${esc(key)} · ${esc(service.environment || "default")}</small></span>
      </div>
      <span class="health-pill"><i></i> configured</span>
    </div>
    <div class="project-stats">
      <div><b>${Number(runtime.running || 0).toLocaleString()}</b><span>running</span></div>
      <div><b>${Number(runtime.queued_retries || 0).toLocaleString()}</b><span>queued</span></div>
      <div><b>${Number(runtime.safety_blocked || 0).toLocaleString()}</b><span>blocked</span></div>
    </div>
  </article>`;
};
let platformProjects = [];

const providerCard = (name, type, details) => `<article class="provider-card">
  <span class="provider-type">${esc(type)}</span>
  <strong>${esc(name)}</strong>
  <small>${esc(details || "Configured")}</small>
</article>`;

function renderPlatform(project) {
  if (!project) return;
  const workflow = project.workflow || {};
  const nodes = workflow.nodes || [];
  const edges = workflow.edges || [];
  const incoming = Object.fromEntries(nodes.map(node => [node.id, []]));
  for (const edge of edges) (incoming[edge.to] ||= []).push(`${edge.from} [${edge.condition}]`);
  byId("graph-summary").textContent = `${nodes.length} nodes · ${edges.length} edges · parallel ${workflow.max_parallel_nodes || 1}`;
  byId("config-graph").innerHTML = nodes.map(node => `<article class="graph-node ${esc(node.type)}">
    <div><span>${esc(node.type)}</span><strong>${esc(node.name || node.id)}</strong></div>
    <small>${esc(node.agent || "control node")}</small>
    <p>${esc((incoming[node.id] || []).length ? `after ${incoming[node.id].join(", ")}` : "entry node")}</p>
  </article>`).join("") || "<p>No graph nodes configured.</p>";
  byId("config-team").innerHTML = Object.entries(project.agents || {}).map(([name, agent]) =>
    providerCard(name, agent.role, `${agent.runtime} · ${agent.model} · ${agent.completion}`),
  ).join("");
  const runtimeCards = Object.entries(project.runtime_providers || {}).map(([name, item]) =>
    providerCard(name, `runtime · ${item.kind}`, item.command || "Default command"),
  );
  const modelCards = Object.entries(project.model_providers || {}).map(([name, item]) =>
    providerCard(name, `model · ${item.kind}`, item.model || "Runtime default"),
  );
  const toolCards = Object.entries(project.tool_providers || {}).map(([name, item]) =>
    providerCard(name, `tools · ${item.kind}`, item.allow_all
      ? "All native tools"
      : (item.tools || []).join(", ") || "No dynamic tools"),
  );
  byId("config-providers").innerHTML = [...runtimeCards, ...modelCards, ...toolCards].join("");
}

async function updatePlatform() {
  if (!byId("config-graph")) return;
  if (!authenticated) {
    byId("config-graph").innerHTML = "<p>Sign in to inspect and manage workflow graphs.</p>";
    return;
  }
  const response = await fetch("/api/v1/platform", {cache: "no-store"});
  if (!response.ok) return;
  const data = await response.json();
  platformProjects = data.projects || [];
  renderPlatform(platformProjects[0]);
}

function renderProjects(data) {
  const projects = Array.isArray(data.projects) ? data.projects : [data];
  for (const id of ["admin-projects", "config-projects"]) {
    const node = byId(id);
    if (node) node.innerHTML = projects.map(projectCard).join("");
  }
  if (byId("admin-project-count")) {
    byId("admin-project-count").textContent = `${projects.length} project${projects.length === 1 ? "" : "s"}`;
  }
}

async function updateAdmin() {
  try {
    const response = await fetch("/api/v1/admin", {cache: "no-store"});
    if (!response.ok) throw new Error(response.status);
    const data = await response.json();
    const runtime = data.runtime || {};
    const service = data.service || {};
    if (byId("admin-running")) {
      byId("admin-running").textContent = runtime.running ?? 0;
      byId("admin-claimed").textContent = runtime.claimed ?? "—";
      byId("admin-retries").textContent = runtime.queued_retries ?? 0;
      byId("admin-completed").textContent = runtime.completed ?? "—";
      byId("admin-safety-blocked").textContent = runtime.safety_blocked ?? "—";
      definitionList("admin-runtime", {
        "Started": service.started_at ? new Date(service.started_at).toLocaleString() : "Per project",
        "Last poll": service.last_tick_at ? new Date(service.last_tick_at).toLocaleString() : "Not yet",
        "Projects": service.project_count || 1,
        "Tracker": service.tracker_kind,
        "Agent capacity": service.max_concurrent_agents,
        "Workspace": service.workspace_root || "Per project",
        "Sandbox": service.thread_sandbox || "Per project",
        "Validation": service.validation_enabled === false ? "Disabled" : "Required",
        "Last error": service.workflow_error || service.last_tick_error || "None",
      });
      byId("admin-rate-limits").textContent = runtime.rate_limits
        ? JSON.stringify(runtime.rate_limits, null, 2)
        : "No rate-limit telemetry received.";
    }
    const tracker = data.tracker || {};
    const agents = data.agents || {};
    const validation = data.validation || {};
    const workflow = data.workflow || {};
    definitionList("config-tracker", {
      "Adapter": tracker.kind,
      "Repository": tracker.repository,
      "Required labels": tracker.required_labels,
      "Active states": tracker.active_states,
      "Terminal states": tracker.terminal_states,
    });
    definitionList("config-agents", {
      "Max concurrent": agents.max_concurrent,
      "Max turns": agents.max_turns,
      "Token limit per attempt": agents.max_tokens_per_run,
      "Max retries": agents.max_retries,
      "Per-state limits": agents.per_state,
      "Max retry backoff": agents.max_retry_backoff_ms ? `${agents.max_retry_backoff_ms} ms` : "—",
    });
    definitionList("config-validation", {
      "Status": validation.enabled === false ? "Disabled" : "Required",
      "Runner": validation.runner,
      "Command timeout": validation.command_timeout_ms ? `${validation.command_timeout_ms} ms` : "—",
      "Cleanup timeout": validation.cleanup_timeout_ms ? `${validation.cleanup_timeout_ms} ms` : "—",
      "Max commands": validation.max_commands,
      "Max attempts per run": validation.max_attempts_per_run,
      "PR gate": validation.publication_gate,
      "Merge policy": validation.merge_policy,
    });
    definitionList("config-hooks", data.hooks || {});
    definitionList("config-workflow", {
      "Path": workflow.path,
      "Prompt size": workflow.prompt_chars ? `${workflow.prompt_chars} characters` : "—",
      "Reload error": workflow.last_reload_error || "None",
    });
    renderProjects(data);
    await updatePlatform();
    const status = byId("status");
    status.className = "status live";
    status.innerHTML = "<i></i> Live";
  } catch {
    const status = byId("status");
    status.className = "status error";
    status.innerHTML = "<i></i> Reconnecting";
  }
}

if (byId("refresh")) {
  byId("refresh").addEventListener("click", async () => {
    await fetch("/api/v1/refresh", {
      method: "POST",
      headers: {"X-CSRFToken": csrfToken},
    });
    setTimeout(updateAdmin, 250);
  });
}
updateAdmin();
setInterval(updateAdmin, 5000);

if (byId("edit-platform")) {
  const selectedSections = project => ({
    runtime_providers: project.runtime_providers,
    model_providers: project.model_providers,
    tool_providers: project.tool_providers,
    agents: project.agents,
    workflow: project.workflow,
  });
  const selectProject = () => {
    const project = platformProjects.find(item => item.key === byId("platform-project").value);
    if (project) byId("platform-json").value = JSON.stringify(selectedSections(project), null, 2);
  };
  byId("edit-platform").addEventListener("click", async () => {
    await updatePlatform();
    byId("platform-project").innerHTML = platformProjects.map(project =>
      `<option value="${esc(project.key)}">${esc(project.name)} · ${esc(project.key)}</option>`,
    ).join("");
    selectProject();
    byId("platform-error").hidden = true;
    byId("platform-dialog").showModal();
  });
  byId("platform-project").addEventListener("change", selectProject);
  document.addEventListener("click", event => {
    if (event.target.closest("[data-platform-close]")) byId("platform-dialog").close();
  });
  byId("platform-form").addEventListener("submit", async event => {
    event.preventDefault();
    const save = byId("platform-save");
    save.disabled = true;
    byId("platform-error").hidden = true;
    try {
      let sections;
      try {
        sections = JSON.parse(byId("platform-json").value);
      } catch {
        throw new Error("Configuration must be valid JSON.");
      }
      const key = byId("platform-project").value;
      const response = await fetch(`/api/v1/platform/${key}`, {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken},
        body: JSON.stringify(sections),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || result.error || "Save failed");
      byId("platform-dialog").close();
      setTimeout(updateAdmin, 500);
    } catch (error) {
      byId("platform-error").textContent = error.message;
      byId("platform-error").hidden = false;
    } finally {
      save.disabled = false;
    }
  });
}
