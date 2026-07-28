const byId = id => document.getElementById(id);
const esc = value => String(value ?? "—").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pretty = value => Array.isArray(value) ? value.join(", ") || "None" : typeof value === "object" && value !== null ? JSON.stringify(value) : value;
const definitionList = (id, values) => {
  const node = byId(id);
  if (node) node.innerHTML = Object.entries(values).map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(pretty(value))}</dd>`).join("");
};
async function updateAdmin() {
  try {
    const response = await fetch("/api/v1/admin", {cache: "no-store"});
    if (!response.ok) throw new Error(response.status);
    const data = await response.json();
    if (byId("admin-running")) {
      byId("admin-running").textContent = data.runtime.running;
      byId("admin-claimed").textContent = data.runtime.claimed;
      byId("admin-retries").textContent = data.runtime.queued_retries;
      byId("admin-completed").textContent = data.runtime.completed;
      byId("admin-safety-blocked").textContent = data.runtime.safety_blocked;
      definitionList("admin-runtime", {
        "Started": new Date(data.service.started_at).toLocaleString(),
        "Last poll": data.service.last_tick_at ? new Date(data.service.last_tick_at).toLocaleString() : "Not yet",
        "Tracker": data.service.tracker_kind,
        "Agent capacity": data.service.max_concurrent_agents,
        "Workspace": data.service.workspace_root,
        "Sandbox": data.service.thread_sandbox,
        "Validation": data.service.validation_enabled ? "Required" : "Disabled",
        "Last error": data.service.workflow_error || data.service.last_tick_error || "None",
      });
      byId("admin-rate-limits").textContent = data.runtime.rate_limits ? JSON.stringify(data.runtime.rate_limits, null, 2) : "No rate-limit telemetry received.";
    }
    definitionList("config-tracker", {
      "Adapter": data.tracker.kind,
      "Repository": data.tracker.repository,
      "Required labels": data.tracker.required_labels,
      "Active states": data.tracker.active_states,
      "Terminal states": data.tracker.terminal_states,
    });
    definitionList("config-agents", {
      "Max concurrent": data.agents.max_concurrent,
      "Max turns": data.agents.max_turns,
      "Token limit per run": data.agents.max_tokens_per_run,
      "Max retries": data.agents.max_retries,
      "Per-state limits": data.agents.per_state,
      "Max retry backoff": `${data.agents.max_retry_backoff_ms} ms`,
    });
    definitionList("config-validation", {
      "Status": data.validation.enabled ? "Required" : "Disabled",
      "Runner": data.validation.runner,
      "Command timeout": `${data.validation.command_timeout_ms} ms`,
      "Cleanup timeout": `${data.validation.cleanup_timeout_ms} ms`,
      "Max commands": data.validation.max_commands,
      "Max attempts per run": data.validation.max_attempts_per_run,
      "PR gate": data.validation.publication_gate,
      "Merge policy": data.validation.merge_policy,
    });
    definitionList("config-hooks", data.hooks);
    definitionList("config-workflow", {
      "Path": data.workflow.path,
      "Prompt size": `${data.workflow.prompt_chars} characters`,
      "Reload error": data.workflow.last_reload_error || "None",
    });
    byId("status").textContent = "LIVE";
  } catch {
    byId("status").textContent = "RECONNECTING";
  }
}
if (byId("refresh")) byId("refresh").addEventListener("click", async () => {
  await fetch("/api/v1/refresh", {method: "POST"});
  setTimeout(updateAdmin, 250);
});
updateAdmin();
setInterval(updateAdmin, 3000);
