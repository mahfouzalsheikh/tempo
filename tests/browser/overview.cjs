// Run against an isolated local Django server. All application APIs are intercepted;
// no agent work, approval, or tracker mutation reaches the server.
module.exports = async function overviewChecks(page) {
  await page.unrouteAll({behavior: "ignoreErrors"});
  const base = "http://127.0.0.1:8049";
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  let operator = true;
  let failure = "";
  let showDecisions = true;
  let emptyWork = false;
  const decisions = [];
  const fixture = {
    service: {max_concurrent_agents: 3}, totals: {},
    running: [
      {run_id: 1, issue_id: "1", identifier: "GH-12", title: "Add booking availability",
        project: "acme/booking", phase: "StreamingTurn", attempt: 0,
        url: "javascript:alert('unsafe')", started_at: new Date().toISOString(),
        graph: [{name: "Implement API", role: "implementer", status: "running"}],
        session: {turn_count: 2, validation_status: "passed", codex_total_tokens: 12500,
          validation_commands: [{name: "API tests", command: "pytest", output: "12 passed", exit_code: 0}]}},
      {run_id: 2, identifier: "GH-19", title: "Review calendar navigation",
        project: "acme/calendar", phase: "ReviewingPullRequest", session: {validation_status: "pending"}},
    ],
    retries: [{identifier: "GH-8", project: "acme/booking", attempt: 1, error: "Provider unavailable"}],
    projects: [
      {key: "acme/booking", name: "Booking", running: 1, validation_policy_configured: true},
      {key: "acme/calendar", name: "Calendar", running: 1, validation_policy_configured: false, publication_required: true},
    ],
  };
  const approval = {id: 7, run_id: 3, issue: "GH-21", title: "Confirm interface contract",
    kind: "human_gate", project: "acme/booking", details: "Review the agreed API before implementation.",
    proposed_arguments: {contract: "booking-v1", note: "<img src=x onerror=alert(1)>"}};
  await page.addInitScript(() => {
    window.EventSource = class {
      constructor() { window.testStream = this; setTimeout(() => this.onopen?.(), 0); }
    };
  });
  await page.route(`${base}/`, async route => {
    const response = await route.fetch();
    const html = (await response.text()).replace('data-authenticated="false"', `data-authenticated="${operator}"`);
    await route.fulfill({response, body: html});
  });
  await page.route("**/api/v1/**", async route => {
    const path = `/api/v1/${route.request().url().split("/api/v1/")[1].split("?")[0]}`;
    const status = failure === "all" || (failure === "operator" && path !== "/api/v1/state") ? 503
      : failure === "expired" && path !== "/api/v1/state" ? 401 : 200;
    let payload = {};
    if (path === "/api/v1/state") payload = emptyWork ? {...fixture, running: [], retries: []} : fixture;
    else if (path === "/api/v1/approvals") payload = {approvals: showDecisions ? [approval] : []};
    else if (path === "/api/v1/control") payload = {runs: showDecisions ? [
      {run_id: 3, status: "waiting_approval", identifier: "GH-21"},
      {run_id: 4, identifier: "GH-24", title: "Prepare deployment checks", status: "paused", phase: "PausedByOperator"},
    ] : []};
    else if (path.endsWith("/decision")) decisions.push(route.request().postDataJSON());
    else throw new Error(`Unexpected API request: ${path}`);
    await route.fulfill({status, json: payload});
  });
  await page.setViewportSize({width: 1440, height: 1100});
  await page.goto(base);
  await page.getByRole("heading", {name: "1 decision needs your input"}).waitFor();
  assert(await page.locator("#attention-count").textContent() === "1", "Approval run counted twice");
  assert(await page.getByText("Required checks missing", {exact: true}).count() === 1, "Setup blocker missing");
  assert(await page.locator('a[href^="javascript:"]').count() === 0, "Unsafe issue URL rendered");
  await page.getByLabel("Find a run").fill("calendar");
  assert(await page.locator("#running .run-card").count() === 1, "Search did not filter runs");
  await page.getByRole("button", {name: "Clear filters"}).click();
  await page.getByLabel("Project", {exact: true}).selectOption("acme/booking");
  assert(await page.locator("#running .run-card").count() === 1, "Project filter failed");
  await page.getByRole("button", {name: "Clear filters"}).click();
  await page.locator('[data-run-card="1"] > summary').click();
  await page.locator('[data-disclosure="1:command:0"] > summary').click();
  await page.locator('[data-focus-key="1:feedback"]').focus();
  fixture.running[0].title = "Add booking availability (updated)";
  await page.evaluate(data => window.testStream.onmessage({data: JSON.stringify(data)}), fixture);
  await page.getByText("GH-12 · Add booking availability (updated)", {exact: true}).waitFor();
  await page.waitForFunction(() => document.activeElement.dataset.focusKey === "1:feedback"
    && document.querySelector('[data-disclosure="1:command:0"]').open);
  assert(await page.locator('[data-run-card="1"]').getAttribute("open") !== null, "Run collapsed on update");
  await page.getByRole("button", {name: "Review request", exact: true}).click();
  assert(decisions.length === 0, "Review click approved without showing context");
  assert((await page.locator("#dialog-context").textContent()).includes("booking-v1"), "Approval context missing");
  assert(await page.locator("#dialog-context img").count() === 0, "Approval context was interpreted as HTML");
  await page.getByRole("button", {name: "Approve request", exact: true}).click();
  await page.waitForFunction(() => !document.querySelector("dialog").open);
  assert(decisions.length === 1 && decisions[0].decision === "approve", "Approval submission failed");
  failure = "operator";
  await page.getByRole("button", {name: "Refresh", exact: true}).click();
  await page.getByRole("heading", {name: "Decision inbox is unavailable"}).waitFor();
  assert(await page.locator("#approval-count").textContent() === "—", "Unavailable inbox reported zero");
  assert(await page.locator('[data-run-action]:enabled').count() === 0, "Stale actions stayed enabled");
  failure = "all";
  await page.getByRole("button", {name: "Refresh", exact: true}).click();
  await page.getByRole("heading", {name: "Updates are interrupted"}).waitFor();
  assert(await page.locator("#running .run-card").count() === 2, "Connection failure discarded last data");
  failure = "expired";
  await page.getByRole("button", {name: "Refresh", exact: true}).click();
  await page.getByRole("link", {name: "Sign in", exact: true}).waitFor();
  failure = "";
  await page.getByRole("button", {name: "Refresh", exact: true}).click();
  await page.getByRole("heading", {name: "1 decision needs your input"}).waitFor();
  await page.setViewportSize({width: 390, height: 844});
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Mobile page overflows horizontally");
  assert(await page.locator('[data-run-card="1"] .phase-cell').isVisible(), "Mobile hides run stage");
  showDecisions = false;
  emptyWork = true;
  await page.getByRole("button", {name: "Refresh", exact: true}).click();
  await page.getByRole("heading", {name: "Review your project setup"}).waitFor();
  fixture.projects[1].validation_policy_configured = true;
  await page.getByRole("button", {name: "Refresh", exact: true}).click();
  await page.getByRole("heading", {name: "No active work right now"}).waitFor();
  assert(await page.locator("#approval-count").textContent() === "0", "Empty inbox should report zero");
  assert(await page.getByText("No active runs", {exact: true}).count() === 1, "Empty work state missing");
  showDecisions = true;
  emptyWork = false;
  fixture.projects[1].validation_policy_configured = false;
  operator = false;
  await page.reload();
  await page.getByRole("heading", {name: "You’re viewing workspace activity"}).waitFor();
  assert(await page.locator("#approval-count").textContent() === "—", "Read-only inbox reported zero");
  assert(await page.locator('[data-run-action]').count() === 0, "Read-only view has run controls");
  assert(!errors.length, `Browser errors: ${errors.join(", ")}`);
  operator = true;
  await page.setViewportSize({width: 1440, height: 1100});
  await page.reload();
  await page.getByRole("heading", {name: "1 decision needs your input"}).waitFor();
  return "Passed: search, project filter, setup status, disclosure/focus retention, approval preview and submit, escaping, failures, expired session, recovery, mobile, read-only.";
};
