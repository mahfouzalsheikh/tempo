// Use tests/browser/serve.py. All progress and mutation routes are intercepted.
module.exports = async function productProgressChecks(page) {
  await page.unrouteAll({behavior: "ignoreErrors"});
  const base = "http://127.0.0.1:8049";
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  let state = "queued", failure = "", requests = 0, mutations = 0;
  const fragment = () => `<div data-progress-content data-plan-id="${failure === "revision" ? 3 : 2}"
    data-poll="${state !== "succeeded"}"><h3>Run #7 · ${state}</h3>
    <form method="post" action="/mutation/"><button>Cancel run</button></form></div>`;
  await page.route(`${base}/ideas/1/`, route => route.fulfill({contentType: "text/html", body:
    `<main><h1>Drawing preparation help</h1><section data-progress-url="/ideas/1/progress/?revision=1&plan=2"
      data-progress-plan="2"><a data-refresh-progress href="?revision=1&plan=2">Refresh status</a>
      <p data-progress-status role="status">Waiting for updates</p>${fragment()}</section></main>
      <script src="/static/product-progress.js"></script>`}));
  await page.route(`${base}/ideas/1/progress/**`, async route => {
    requests++;
    assert(route.request().method() === "GET", "Progress update mutated state");
    await route.fulfill({status: failure === "http" ? 503 : 200,
      contentType: "text/html", body: failure === "login" ? "<h1>Sign in</h1>" : fragment()});
  });
  await page.route(`${base}/mutation/`, route => { mutations++; return route.abort(); });
  await page.clock.install();
  await page.goto(`${base}/ideas/1/`);
  state = "running";
  await page.clock.runFor(10100);
  await page.getByRole("heading", {name: "Run #7 · running"}).waitFor();
  await page.getByRole("button", {name: "Cancel run"}).focus();
  const beforeFocus = requests;
  state = "integrating";
  await page.clock.runFor(10100);
  assert(requests === beforeFocus, "Polling replaced a focused control");
  assert(await page.getByRole("button", {name: "Cancel run"}).evaluate(e => e === document.activeElement),
    "Polling lost keyboard focus");
  await page.getByRole("link", {name: "Refresh status"}).click();
  await page.getByRole("heading", {name: "Run #7 · integrating"}).waitFor();
  for (failure of ["http", "login", "revision"]) {
    await page.getByRole("link", {name: "Refresh status"}).click();
    await page.getByRole("status").filter({hasText: "Updates are interrupted"}).waitFor();
    assert(await page.getByRole("heading", {name: "Run #7 · integrating"}).count() === 1,
      "Failed or mismatched response replaced saved status");
  }
  failure = "";
  state = "succeeded";
  await page.getByRole("link", {name: "Refresh status"}).click();
  await page.getByRole("heading", {name: "Run #7 · succeeded"}).waitFor();
  const finished = requests;
  await page.clock.runFor(30100);
  assert(requests === finished, "Terminal work kept polling");
  assert(mutations === 0, "A status refresh triggered an action");
  return {automaticUpdates: true, focusPreserved: true, failedResponsesRetained: true,
    terminalPollingStopped: true, mutations};
};
