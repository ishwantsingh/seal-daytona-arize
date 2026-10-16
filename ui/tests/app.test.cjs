const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const html = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");
const tick = () => new Promise((resolve) => setTimeout(resolve, 15));
const run = (id, outcome = "answered") => ({
  run_id: id,
  question: "Question " + id,
  outcome,
  version: "abcdef123456",
  created_at: "2026-10-07T12:00:00Z",
  message: "18 available units",
  resources: { elapsed_seconds: 0.2 },
  trace: { status: "disabled" },
});
const routes = {
  "/api/health": {
    sqlite: "connected",
    active_version: "inventory-v1",
    logistics_active_version: "logistics-v1",
  },
  "/api/datasets": {
    datasets: [{ id: "inventory", input_kind: "inventory_table" }],
  },
  "/api/runs": { runs: [run("a"), run("b", "error")] },
  "/api/evaluations": { evaluations: [] },
  "/api/versions": { versions: [], base_version: "v1" },
  "/api/harness-workflows": { workflows: [] },
};
async function setup(t, overrides = {}, hash = "", draft = "") {
  const dom = new JSDOM(html, {
    url: "http://localhost/" + hash,
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  t.after(() => dom.window.close());
  const w = dom.window,
    calls = [];
  w.scrollTo = () => {};
  w.HTMLElement.prototype.scrollTo = () => {};
  w.sessionStorage.setItem("selfHealLastQuestion", draft);
  w.fetch = async (url, options = {}) => {
    calls.push([url, options]);
    const key = String(url).split("?")[0],
      value = Object.hasOwn(overrides, key) ? overrides[key] : routes[key];
    if (value instanceof Error) throw value;
    const body =
      typeof value === "function" ? await value(url, options) : value;
    return {
      ok: body !== undefined,
      json: async () => body || { error: "Missing record" },
    };
  };
  w.eval(script);
  await tick();
  return { w, d: w.document, calls };
}
test("navigation isolates pages and retains the draft when inspecting a linked run", async (t) => {
  const { w, d } = await setup(
    t,
    { "/api/runs/a": run("a") },
    "#runs",
    "My unfinished\nquestion",
  );
  assert.equal(d.querySelector("#ask").hidden, true);
  assert.equal(
    d.querySelector('[data-nav="runs"]').getAttribute("aria-current"),
    "page",
  );
  d.querySelector(".row-link").click();
  await tick();
  assert.equal(
    d.querySelector("[data-run-question]").textContent,
    "Question a",
  );
  d.querySelector("[data-new-analysis]").click();
  await tick();
  assert.equal(d.querySelector("#ask").hidden, false);
  assert.equal(d.querySelector("#runs").hidden, true);
  assert.equal(d.querySelector("#question").value, "My unfinished\nquestion");
});
test("Ask shows the selected SQLite snapshot and switches logistics tables", async (t) => {
  const { w, d, calls } = await setup(t, {
    "/api/datasets": {
      datasets: [
        { id: "inventory", input_kind: "inventory_table" },
        { id: "logistics", input_kind: "logistics_bundle" },
      ],
    },
    "/api/datasets/preview": (url) => {
      const params = new URL(url, "http://localhost").searchParams;
      if (params.get("input_kind") === "inventory_table")
        return {
          relation: "analyst_rows",
          relations: { analyst_rows: 1 },
          columns: ["sku", "on_hand"],
          row_count: 1,
          rows: [{ sku: "A-100", on_hand: 10 }],
        };
      const customers = params.get("relation") === "customers";
      return {
        relation: customers ? "customers" : "shipments",
        relations: { customers: 1, shipments: 1 },
        columns: customers ? ["customer_id"] : ["shipment_id"],
        row_count: 1,
        rows: [customers ? { customer_id: "C-1" } : { shipment_id: "S-1" }],
      };
    },
  });
  assert.match(d.querySelector("[data-database-content]").textContent, /S-1/);
  d.querySelector("[data-database-relations] button").click();
  await tick();
  assert.match(d.querySelector("[data-database-content]").textContent, /C-1/);
  const picker = d.querySelector("#data-source");
  picker.value = "inventory:inventory";
  picker.dispatchEvent(new w.Event("change"));
  await tick();
  assert.match(d.querySelector("[data-database-content]").textContent, /A-100/);
  assert.equal(d.querySelector("[data-database-source]").textContent, "inventory");
  assert.equal(calls.filter(([url]) => url.startsWith("/api/datasets/preview?")).length, 3);
});
test("disconnected service has an honest status, disabled analysis, and retryable history", async (t) => {
  const { d } = await setup(t, {
    "/api/health": new Error("offline"),
    "/api/runs": new Error("offline"),
  });
  assert.equal(d.querySelector("[data-connection]").dataset.state, "error");
  assert.equal(
    d.querySelector("[data-active-version]").textContent,
    "Unavailable",
  );
  assert.equal(d.querySelector("[data-run-button]").disabled, true);
  assert.match(d.querySelector("[data-run-button]").title, /Connect/);
  assert.equal(
    d.querySelector("[data-history-list] button").textContent,
    "Retry",
  );
});
test("search, outcome filtering, and pagination operate on recorded history", async (t) => {
  const { w, d } = await setup(
    t,
    {
      "/api/runs": {
        runs: Array.from({ length: 15 }, (_, i) =>
          run(String(i), i % 2 ? "error" : "answered"),
        ),
      },
    },
    "#runs",
  );
  assert.equal(d.querySelectorAll("[data-history-list] tr").length, 10);
  d.querySelector("[data-history-pager] button:last-child").click();
  assert.equal(d.querySelectorAll("[data-history-list] tr").length, 5);
  const filter = d.querySelector("[data-run-filter]");
  filter.value = "error";
  filter.dispatchEvent(new w.Event("change"));
  assert.equal(d.querySelectorAll("[data-history-list] tr").length, 7);
  const search = d.querySelector("[data-run-search]");
  search.value = "Question 13";
  search.dispatchEvent(new w.Event("input"));
  assert.equal(d.querySelectorAll("[data-history-list] tr").length, 1);
  assert.match(
    d.querySelector("[data-history-list]").textContent,
    /Question 13/,
  );
});
test("failed analysis preserves input and re-enables submit; Ctrl+Enter submits", async (t) => {
  const { w, d, calls } = await setup(
    t,
    {
      "/api/runs": (_, options) => {
        if (options.method === "POST") throw Error("Execution unavailable");
        return { runs: [] };
      },
    },
    "",
    "Draft question",
  );
  const question = d.querySelector("#question");
  question.dispatchEvent(
    new w.KeyboardEvent("keydown", {
      key: "Enter",
      ctrlKey: true,
      bubbles: true,
    }),
  );
  await tick();
  assert.equal(
    calls.filter(
      ([url, options]) => url === "/api/runs" && options.method === "POST",
    ).length,
    1,
  );
  assert.equal(question.value, "Draft question");
  assert.match(
    d.querySelector("[data-analysis-error]").textContent,
    /Execution unavailable/,
  );
  assert.equal(d.querySelector("[data-run-button]").disabled, false);
});
test("rejected evolution does not imply completed activation and tabs support keyboard focus", async (t) => {
  const { w, d } = await setup(
    t,
    {
      "/api/evolution-jobs/rejected": {
        stage: "rejected",
        status: "rejected",
        incident_run_id: "a",
      },
      "/api/evolution-jobs/rejected/events": { events: [] },
      "/api/evolution-jobs/rejected/evaluations": {
        groups: [],
        trials: [{ passed: null }],
      },
    },
    "#evolve/rejected",
  );
  assert.equal(d.querySelector("[data-evolve-status]").textContent, "Rejected");
  assert.equal(d.querySelectorAll("[data-evolve-stages] .complete").length, 0);
  assert.equal(d.querySelectorAll("[data-evolve-stages] .current").length, 0);
  assert.match(d.querySelector("[data-evolve-trials]").textContent, /Pending/);
  const tab = d.querySelector('[data-evolution-tab="overview"]');
  tab.focus();
  tab.dispatchEvent(
    new w.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }),
  );
  assert.equal(d.activeElement.dataset.evolutionTab, "changes");
  assert.equal(
    d.querySelector('[data-evolution-panel="changes"]').hidden,
    false,
  );
});
test("evolution discovery failure is caught and offers retry", async (t) => {
  const { d } = await setup(
    t,
    { "/api/runs": new Error("Service unavailable") },
    "#evolve",
  );
  assert.equal(
    d.querySelector("[data-evolve-status]").textContent,
    "Unavailable",
  );
  assert.match(
    d.querySelector("[data-evolve-error]").textContent,
    /Service unavailable/,
  );
  assert.equal(
    d.querySelector("[data-evolve-error] button").textContent,
    "Retry",
  );
});
test("workflow fits actual bounds, selects a component, and supports zoom", async (t) => {
  const { d } = await setup(
    t,
    {
      "/api/harness-workflows": {
        workflows: [
          { workflow_revision_id: "wf", source_commit: "abc", node_count: 1 },
        ],
      },
      "/api/harness-workflows/wf": {
        graph: {
          nodes: [
            {
              id: "agent",
              label: "Analyst",
              kind: "agent",
              layout: { x: 800, y: 700 },
            },
          ],
          edges: [],
        },
      },
    },
    "#harness",
  );
  const svg = d.querySelector("[data-harness-workflow] svg");
  assert.equal(svg.getAttribute("viewBox"), "776 676 212 106");
  d.querySelector(".workflow-node").dispatchEvent(
    new d.defaultView.MouseEvent("click"),
  );
  assert.equal(
    d.querySelector(".workflow-node").classList.contains("selected"),
    true,
  );
  assert.match(
    d.querySelector("[data-harness-inspector]").textContent,
    /Analyst/,
  );
  d.querySelector('[aria-label="Zoom in"]').click();
  assert.equal(svg.style.width, "125%");
  d.querySelector('[aria-label="Fit workflow"]').click();
  assert.equal(svg.style.width, "100%");
});
test("slow run response cannot replace a newer page", async (t) => {
  let finish;
  const { w, d } = await setup(
    t,
    { "/api/runs/a": () => new Promise((resolve) => (finish = resolve)) },
    "#run-details/a",
  );
  w.location.hash = "#versions";
  await tick();
  finish(run("a"));
  await tick();
  assert.equal(d.querySelector("#versions").hidden, false);
  assert.equal(d.querySelector("#run-details").hidden, true);
});
test("workflow comparison uses saved structural evidence", async (t) => {
  const { w, d } = await setup(
    t,
    {
      "/api/harness-workflows": {
        workflows: [
          { workflow_revision_id: "new", node_count: 1 },
          { workflow_revision_id: "old", node_count: 1 },
        ],
      },
      "/api/harness-workflows/new": {
        graph: { nodes: [{ id: "a", label: "Agent" }], edges: [] },
      },
      "/api/harness-workflows/new/diff": {
        nodes: {
          added: [{ label: "Shipment tool" }],
          changed: [],
          removed: [],
        },
        edges: { added: [], removed: [] },
      },
    },
    "#harness",
  );
  const compare = d.querySelector("[data-compare-revision]");
  compare.value = "old";
  compare.dispatchEvent(new w.Event("change"));
  await tick();
  assert.equal(d.querySelector("[data-workflow-diff]").hidden, false);
  assert.match(
    d.querySelector("[data-workflow-diff]").textContent,
    /Added: Shipment tool/,
  );
});
test("source switches preserve the draft and revision deep links resolve their own family", async (t) => {
  const { w, d } = await setup(
    t,
    {
      "/api/harness-workflows": (url) => ({
        workflows: url.includes("inventory-totals")
          ? []
          : [{ workflow_revision_id: "wf" }],
      }),
      "/api/harness-workflows/wf": {
        task_family: "logistics-shipment-threshold",
        graph: { nodes: [{ id: "a", label: "Agent" }], edges: [] },
      },
    },
    "#harness/wf",
    "Keep this question",
  );
  await tick();
  assert.equal(
    d.querySelector("[data-harness-family]").value,
    "logistics-shipment-threshold",
  );
  assert.match(
    d.querySelector("[data-workspace-title]").textContent,
    /Logistics/,
  );
  assert.ok(d.querySelector("[data-harness-workflow] svg"));
  d.querySelector("[data-new-analysis]").click();
  await tick();
  const source = d.querySelector("#data-source");
  source.value = "logistics:pending";
  source.dispatchEvent(new w.Event("change"));
  assert.equal(d.querySelector("#question").value, "Keep this question");
});
test("evaluation filters paginate and missing results remain pending", async (t) => {
  const { w, d } = await setup(
    t,
    {
      "/api/evaluations": {
        evaluations: Array.from({ length: 15 }, (_, i) => ({
          case_id: "case-" + i,
          passed: i === 0 ? null : i % 2 === 0,
        })),
      },
    },
    "#evaluations",
  );
  assert.equal(
    d.querySelectorAll("[data-evaluations-list] tbody tr").length,
    10,
  );
  assert.match(
    d.querySelector("[data-evaluations-list]").textContent,
    /Pending/,
  );
  const filter = d.querySelector("[data-evaluation-filter]");
  filter.value = "passed";
  filter.dispatchEvent(new w.Event("change"));
  assert.equal(
    d.querySelectorAll("[data-evaluations-list] tbody tr").length,
    7,
  );
});
test("no evolution jobs provides one useful empty state instead of an empty workspace", async (t) => {
  const { d } = await setup(t, {}, "#evolve");
  assert.equal(d.querySelector("#evolve").classList.contains("no-job"), true);
  assert.equal(d.querySelector("[data-evolve-empty]").hidden, false);
  assert.equal(
    d.querySelector("[data-evolve-empty] button").textContent,
    "New analysis",
  );
});
test("unmatched multiline questions retain normal cursor navigation", async (t) => {
  const { w, d } = await setup(t);
  const question = d.querySelector("#question");
  question.value = "A custom question\nwith another line";
  question.dispatchEvent(new w.Event("input"));
  assert.equal(d.querySelector("#suggestions").hidden, true);
  const arrow = new w.KeyboardEvent("keydown", {
    key: "ArrowDown",
    bubbles: true,
    cancelable: true,
  });
  question.dispatchEvent(arrow);
  assert.equal(arrow.defaultPrevented, false);
});
test("activation exposes the stored automatic rerun and its actual outcome", async (t) => {
  const { d } = await setup(
    t,
    {
      "/api/evolution-jobs/fixed": {
        job_id: "fixed",
        stage: "activated",
        status: "activated",
        incident_run_id: "a",
        rerun_run_id: "success",
      },
      "/api/evolution-jobs/fixed/events": { events: [] },
      "/api/evolution-jobs/fixed/evaluations": { groups: [], trials: [] },
      "/api/runs/success": {
        ...run("success"),
        message: "18 available units after repair",
        version: "repaired-version",
      },
    },
    "#evolve/fixed",
  );
  const verification = d.querySelector("[data-evolve-verification]");
  assert.equal(verification.hidden, false);
  assert.match(verification.textContent, /18 available units after repair/);
  assert.equal(
    verification.querySelector("a").getAttribute("href"),
    "#run-details/success",
  );
  assert.equal(
    d.querySelector("[data-active-version]").textContent,
    "repaired-version",
  );
});
test("activation alone never claims that the original question now succeeds", async (t) => {
  const { d } = await setup(
    t,
    {
      "/api/evolution-jobs/fixed": { stage: "activated", status: "activated" },
      "/api/evolution-jobs/fixed/events": { events: [] },
      "/api/evolution-jobs/fixed/evaluations": { groups: [], trials: [] },
    },
    "#evolve/fixed",
  );
  assert.match(
    d.querySelector("[data-evolve-verification]").textContent,
    /no verification run was recorded/,
  );
  assert.equal(d.querySelector("[data-evolve-verification] .answered"), null);
});
