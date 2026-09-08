"use strict";

(() => {
  const panel = document.querySelector("[data-progress-url]");
  if (!panel) return;
  const status = panel.querySelector("[data-progress-status]");
  let content = panel.querySelector("[data-progress-content]");
  let pending = false;
  let timer;
  let retry = false;
  let lastChecked = "";

  function schedule() {
    clearTimeout(timer);
    if (retry || content.dataset.poll === "true") timer = setTimeout(refresh, 10000);
  }

  async function refresh(manual = false) {
    clearTimeout(timer);
    if (pending) return;
    // Keep focused controls and selected evidence stable while the operator uses them.
    if (document.hidden || content.contains(document.activeElement)
        || (window.getSelection()?.toString() && !manual)) {
      schedule();
      return;
    }
    pending = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(panel.dataset.progressUrl, {
        credentials: "same-origin", cache: "no-store", redirect: "error",
        signal: controller.signal,
      });
      if (!response.ok || !response.headers.get("Content-Type")?.includes("text/html")) {
        throw new Error("Progress unavailable");
      }
      const document = new DOMParser().parseFromString(await response.text(), "text/html");
      const next = document.querySelector("[data-progress-content]");
      if (!next || next.dataset.planId !== panel.dataset.progressPlan) {
        throw new Error("Progress revision changed");
      }
      // Focus can move while a request is in flight. Do not replace a live control.
      if (content.contains(window.document.activeElement) || window.document.hidden
          || window.getSelection()?.toString()) return;
      content.replaceWith(next);
      content = next;
      retry = false;
      lastChecked = new Date().toLocaleTimeString();
      status.textContent = `Updated ${lastChecked}. ${content.dataset.poll === "true"
        ? "Updates continue automatically." : "No work is pending. Refresh to check for changes."}`;
    } catch {
      retry = true;
      status.textContent = "Updates are interrupted. Showing the last saved status. "
        + (lastChecked ? `Last checked ${lastChecked}. ` : "")
        + "Refresh to retry, or sign in again if your session expired.";
    } finally {
      clearTimeout(timeout);
      pending = false;
      schedule();
    }
  }

  panel.querySelector("[data-refresh-progress]").addEventListener("click", event => {
    event.preventDefault();
    refresh(true);
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
  schedule();
})();
