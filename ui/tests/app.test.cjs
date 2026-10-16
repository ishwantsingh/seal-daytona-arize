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
  evolution_job_id: null,
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
async function setup(t, overrides = {}, hash = "", draft = "", savedChat, beforeLoad) {
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
  if (savedChat !== undefined)
    w.localStorage.setItem("selfHealChat", typeof savedChat === "string" ? savedChat : JSON.stringify(savedChat));
  if (beforeLoad) beforeLoad(w);
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
  d.querySelector('[data-nav="ask"]').click();
  await tick();
  assert.equal(d.querySelector("#ask").hidden, false);
  assert.equal(d.querySelector("#question").value, "My unfinished\nquestion");
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
    d.querySelector('[data-chat-role="assistant"]').textContent,
    /Execution unavailable/,
  );
  assert.equal(d.querySelector("[data-run-button]").disabled, false);
});
test("chat appends question-answer pairs, keeps Ask open, and links to recorded evidence", async (t) => {
  let next = 0;
  const { w, d, calls } = await setup(t, {
    "/api/runs": (_, options) => options.method === "POST"
      ? { ...run(String(++next)), message: "Answer " + next }
      : { runs: [run("1")] },
    "/api/runs/1": run("1"),
  }, "#ask");
  const question = d.querySelector("#question");
  assert.equal(d.querySelector("#runs").hidden, true);
  assert.equal(d.querySelector("[data-examples]").hidden, false);
  for (const prompt of ["First question", "Second question"]) {
    question.value = prompt;
    d.querySelector("#analysis-form").requestSubmit();
    await tick();
    assert.equal(w.location.hash, "#ask");
    assert.equal(d.querySelector("#run-details").hidden, true);
    assert.equal(d.querySelector("#ask").hidden, false);
    assert.equal(d.querySelector("[data-examples]").hidden, true);
    assert.equal(d.querySelector(".ask-heading").hidden, true);
    assert.equal(question.value, "");
    assert.equal(w.sessionStorage.getItem("selfHealLastQuestion"), "");
    assert.equal(d.activeElement, question);
    assert.equal(d.querySelector("#suggestions").hidden, true);
    assert.equal(d.querySelector("[data-announcement]").textContent, "Analyst replied");
  }
  const messages = [...d.querySelectorAll("[data-chat-role] p")].map(node => node.textContent);
  assert.deepEqual(messages, ["First question", "Answer 1", "Second question", "Answer 2"]);
  assert.deepEqual(calls.filter(([, options]) => options.method === "POST")
    .map(([, options]) => JSON.parse(options.body)), [
      { question: "First question", input_kind: "inventory_table", dataset_id: "inventory" },
      { question: "Second question", input_kind: "inventory_table", dataset_id: "inventory" },
    ]);
  assert.equal(calls.some(([url]) => url === "/api/runs/1"), false);
  d.querySelector('[data-chat-role="assistant"] a').click();
  await tick();
  assert.equal(d.querySelector("#run-details").hidden, false);
  d.querySelector('[data-nav="ask"]').click();
  await tick();
  assert.equal(d.querySelectorAll("[data-chat-role]").length, 4);
  assert.equal(d.querySelector('[data-chat-role="assistant"] p').textContent, "Answer 1");
  assert.equal(d.querySelector('[data-chat-role="assistant"]').className, "chat-message");
  assert.equal(d.querySelector('[data-chat-role="assistant"] a').getAttribute("href"), "#run-details/1");
  assert.equal(d.querySelector('[data-nav="ask"]').getAttribute("aria-current"), "page");
});
test("pending chat prevents duplicate requests, captures its source, and preserves a new draft", async (t) => {
  let resolve;
  const { w, d, calls } = await setup(t, {
    "/api/datasets": { datasets: [
      { id: "inventory", input_kind: "inventory_table" },
      { id: "logistics", input_kind: "logistics_bundle" },
    ] },
    "/api/runs": (_, options) => options.method === "POST"
      ? new Promise(done => { resolve = done; }) : { runs: [] },
  });
  const question = d.querySelector("#question"), source = d.querySelector("#data-source"),
    form = d.querySelector("#analysis-form"), button = d.querySelector("[data-run-button]");
  source.value = "inventory:inventory";
  source.dispatchEvent(new w.Event("change"));
  question.value = "Sent question";
  form.requestSubmit();
  const reply = d.querySelector('[data-chat-role="assistant"]');
  assert.match(reply.textContent, /Running analysis/);
  assert.equal(reply.getAttribute("aria-busy"), "true");
  assert.equal(button.disabled, true);
  assert.equal(d.querySelector("[data-new-analysis]").disabled, true);
  source.value = "logistics:logistics";
  source.dispatchEvent(new w.Event("change"));
  question.value = "Next draft";
  question.dispatchEvent(new w.Event("input"));
  form.dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  const posts = calls.filter(([, options]) => options.method === "POST");
  assert.equal(posts.length, 1);
  assert.equal(JSON.parse(posts[0][1].body).dataset_id, "inventory");
  assert.match(d.querySelector('[data-chat-role="user"]').textContent, /Inventory · inventory/);
  resolve(run("pending"));
  await tick();
  assert.equal(question.value, "Next draft");
  assert.equal(w.sessionStorage.getItem("selfHealLastQuestion"), "Next draft");
  assert.equal(source.value, "logistics:logistics");
  assert.equal(reply.hasAttribute("aria-busy"), false);
  assert.equal(button.disabled, false);
  assert.equal(d.querySelector("[data-new-analysis]").disabled, false);
});
test("reload restores full replies, errors, links, draft, and source even when opening a run first", async (t) => {
  const sources = { datasets: [
    { id: "inventory", input_kind: "inventory_table" },
    { id: "logistics", input_kind: "logistics_bundle" },
  ] };
  let next = 0;
  const overrides = {
    "/api/datasets": sources,
    "/api/runs": (_, options) => {
      if (options.method !== "POST") return { runs: [] };
      if (++next === 3) throw Error("Offline");
      return next === 1 ? { ...run("1"), message: "Full answer" }
        : { ...run("2", "unsupported"), message: "Tool required", evolution_job_id: "repair" };
    },
    "/api/runs/1": { ...run("1"), dataset: { id: "logistics", input_kind: "logistics_bundle" } },
  };
  const first = await setup(t, overrides);
  const picker = first.d.querySelector("#data-source");
  picker.value = "inventory:inventory";
  picker.dispatchEvent(new first.w.Event("change"));
  for (const prompt of ["First", "Second", "Third"]) {
    first.d.querySelector("#question").value = prompt;
    first.d.querySelector("#analysis-form").requestSubmit();
    await tick();
  }
  first.d.querySelector("#question").value = "Unsent\ndraft";
  first.d.querySelector("#question").dispatchEvent(new first.w.Event("input"));
  first.d.querySelector('[data-chat-role="assistant"] a').click();
  await tick();
  assert.equal(picker.value, "inventory:inventory");
  const saved = first.w.localStorage.getItem("selfHealChat");
  assert.equal(JSON.parse(saved).messages.length, 6);
  const restored = await setup(t, overrides, "#run-details/1", "Old draft", saved);
  assert.equal(restored.d.querySelector("#ask").hidden, true);
  restored.d.querySelector('[data-nav="ask"]').click();
  await tick();
  const replies = restored.d.querySelectorAll('[data-chat-role="assistant"]');
  assert.deepEqual([...replies].map(node => node.querySelector("p").textContent), [
    "Full answer", "Tool required", "Analysis could not complete. Offline",
  ]);
  assert.equal(replies[0].querySelector(".chat-status").textContent, "Answered · 0.2 s");
  assert.equal(replies[0].querySelector("a").getAttribute("href"), "#run-details/1");
  assert.equal(replies[1].querySelector('a[href^="#evolve/"]').getAttribute("href"), "#evolve/repair");
  assert.equal(replies[2].dataset.state, "error");
  assert.equal(restored.d.querySelector("#question").value, "Unsent\ndraft");
  assert.equal(restored.d.querySelector("#data-source").value, "inventory:inventory");
  assert.equal(restored.calls.some(([, options]) => options.method === "POST"), false);
  restored.d.querySelector("[data-new-analysis]").click();
  await tick();
  const cleared = restored.w.localStorage.getItem("selfHealChat");
  assert.deepEqual(JSON.parse(cleared).messages, []);
  assert.equal(JSON.parse(cleared).draft, "");
  const fresh = await setup(t, overrides, "#ask", "", cleared);
  assert.equal(fresh.d.querySelectorAll("[data-chat-role]").length, 0);
  assert.equal(fresh.d.querySelector("[data-examples]").hidden, false);
});
test("a pending request restored after reload becomes interrupted and is never resubmitted", async (t) => {
  let resolve;
  const first = await setup(t, {
    "/api/runs": (_, options) => options.method === "POST"
      ? new Promise(done => { resolve = done; }) : { runs: [] },
  });
  first.d.querySelector("#question").value = "Slow question";
  first.d.querySelector("#analysis-form").requestSubmit();
  const saved = first.w.localStorage.getItem("selfHealChat");
  assert.equal(JSON.parse(saved).messages[1].pending, true);
  const restored = await setup(t, {}, "#ask", "", saved);
  const reply = restored.d.querySelector('[data-chat-role="assistant"]');
  assert.match(reply.textContent, /Request interrupted/);
  assert.match(reply.querySelector("p").textContent, /Check Runs/);
  assert.equal(reply.hasAttribute("aria-busy"), false);
  assert.equal(restored.d.querySelector("[data-run-button]").disabled, false);
  assert.equal(restored.calls.some(([, options]) => options.method === "POST"), false);
  assert.equal(JSON.parse(restored.w.localStorage.getItem("selfHealChat")).messages[1].pending, false);
  resolve(run("slow"));
  await tick();
});
test("restored content is text and saved scroll position survives route changes and reload", async (t) => {
  const unsafe = '<img src=x onerror="alert(1)">';
  const saved = {
    version: 1, draft: unsafe, source: { id: "inventory", input_kind: "inventory_table" }, scrollTop: 240,
    messages: [{ role: "assistant", text: unsafe, source: null, status: "Answered", runId: "unsafe/#id" }],
  };
  const { w, d } = await setup(t, {}, "#ask", "", saved);
  await tick();
  const reply = d.querySelector('[data-chat-role="assistant"]');
  assert.equal(reply.querySelector("p").textContent, unsafe);
  assert.equal(reply.querySelector("img"), null);
  assert.equal(reply.querySelector("a").getAttribute("href"), "#run-details/unsafe%2F%23id");
  const scroll = d.querySelector("[data-chat-scroll]");
  assert.equal(scroll.scrollTop, 240);
  scroll.scrollTop = 320;
  scroll.dispatchEvent(new w.Event("scroll"));
  d.querySelector('[data-nav="runs"]').click();
  await tick();
  assert.equal(JSON.parse(w.localStorage.getItem("selfHealChat")).scrollTop, 320);
  d.querySelector('[data-nav="ask"]').click();
  await tick();
  assert.equal(scroll.scrollTop, 320);
});
test("invalid saved state and unavailable browser storage do not break chat", async (t) => {
  for (const invalid of ["not json", { version: 2 }, {
    version: 1, messages: [{ role: "script", text: "Bad state" }], draft: "", source: null,
  }]) {
    const { d } = await setup(t, {}, "#ask", "Existing draft", invalid);
    assert.equal(d.querySelectorAll("[data-chat-role]").length, 0);
    assert.equal(d.querySelector("#question").value, "Existing draft");
  }
  const { d } = await setup(t, {
    "/api/runs": (_, options) => options.method === "POST" ? run("nostorage") : { runs: [] },
  }, "#ask", "", undefined, w => {
    Object.defineProperty(w, "localStorage", {
      get() { throw Error("Storage unavailable"); },
    });
  });
  d.querySelector("#question").value = "Question";
  d.querySelector("#analysis-form").requestSubmit();
  await tick();
  assert.equal(d.querySelector('[data-chat-role="assistant"] p').textContent, "18 available units");
  assert.equal(d.querySelector("[data-run-button]").disabled, false);
});
test("chat presents capability gaps, execution errors, and numeric or grouped answers without redirects", async (t) => {
  const responses = [
    { ...run("gap", "unsupported"), message: "", limitation_reason: "Tool required", evolution_job_id: "job/1" },
    { ...run("error", "error"), message: "", error: "Runner unavailable" },
    { ...run("zero"), message: "", answer: { value: 0 } },
    { ...run("groups"), message: "", answer: { groups: { East: 18, West: 7 } } },
  ];
  let next = 0;
  const { w, d } = await setup(t, {
    "/api/runs": (_, options) => options.method === "POST" ? responses[next++] : { runs: [] },
  });
  for (const response of responses) {
    d.querySelector("#question").value = "Question " + response.run_id;
    d.querySelector("#analysis-form").requestSubmit();
    await tick();
    assert.equal(w.location.hash, "");
    assert.equal(d.querySelector("#ask").hidden, false);
  }
  const replies = d.querySelectorAll('[data-chat-role="assistant"]');
  assert.deepEqual([...replies].map(node => node.querySelector("p").textContent), [
    "Tool required", "Runner unavailable", '{"value":0}', '{"groups":{"East":18,"West":7}}',
  ]);
  assert.equal(replies[0].querySelector('a[href^="#evolve/"]').getAttribute("href"), "#evolve/job%2F1");
});
test("late chat replies do not navigate away from another page", async (t) => {
  let resolve;
  const { w, d } = await setup(t, {
    "/api/runs": (_, options) => options.method === "POST"
      ? new Promise(done => { resolve = done; }) : { runs: [] },
  });
  d.querySelector("#question").value = "Question";
  d.querySelector("#analysis-form").requestSubmit();
  w.location.hash = "#versions";
  await tick();
  resolve(run("late"));
  await tick();
  assert.equal(w.location.hash, "#versions");
  assert.equal(d.querySelector("#ask").hidden, true);
  assert.equal(d.querySelector("#versions").hidden, false);
  assert.equal(d.querySelector('[data-chat-role="assistant"] p').textContent, "18 available units");
});
test("failed requests leave an inline reply and retry without overwriting a newer draft", async (t) => {
  let reject;
  let attempts = 0;
  const { w, d } = await setup(t, {
    "/api/runs": (_, options) => {
      if (options.method !== "POST") return { runs: [] };
      return ++attempts === 1 ? new Promise((_, fail) => { reject = fail; }) : run("retry");
    },
  });
  const question = d.querySelector("#question");
  question.value = "Original question";
  d.querySelector("#analysis-form").requestSubmit();
  question.value = "New draft";
  question.dispatchEvent(new w.Event("input"));
  reject(Error("Offline"));
  await tick();
  assert.equal(question.value, "New draft");
  const failed = d.querySelector('[data-chat-role="assistant"]');
  assert.equal(failed.dataset.state, "error");
  assert.match(failed.textContent, /Offline/);
  assert.equal(failed.hasAttribute("aria-busy"), false);
  assert.equal(failed.querySelector("a"), null);
  d.querySelector("#analysis-form").requestSubmit();
  await tick();
  assert.equal(d.querySelectorAll('[data-chat-role="assistant"]').length, 2);
  assert.equal(question.value, "");
});
test("chat treats question and response HTML as text and New analysis clears the conversation", async (t) => {
  const unsafe = '<img src=x onerror="alert(1)">';
  const { w, d } = await setup(t, {
    "/api/runs": (_, options) => options.method === "POST" ? { ...run("html"), message: unsafe } : { runs: [] },
  }, "#ask");
  const question = d.querySelector("#question");
  question.value = unsafe;
  d.querySelector("#analysis-form").requestSubmit();
  await tick();
  const messages = d.querySelector("[data-chat-messages]");
  assert.equal(messages.querySelector("img"), null);
  assert.deepEqual([...messages.querySelectorAll("p")].map(node => node.textContent), [unsafe, unsafe]);
  question.value = "Another draft";
  question.dispatchEvent(new w.Event("input"));
  d.querySelector("[data-new-analysis]").click();
  await tick();
  assert.equal(messages.children.length, 0);
  assert.equal(messages.hidden, true);
  assert.equal(d.querySelector("[data-examples]").hidden, false);
  assert.equal(d.querySelector(".ask-heading").hidden, false);
  assert.equal(question.value, "");
  assert.equal(w.sessionStorage.getItem("selfHealLastQuestion"), "");
  assert.equal(d.querySelector("[data-new-analysis]").hidden, true);
  assert.equal(d.activeElement, question);
  question.value = "   \n ";
  d.querySelector("#analysis-form").requestSubmit();
  assert.equal(messages.children.length, 0);
  assert.equal(question.validity.customError, true);
});
test("Enter sends, Shift+Enter and IME preserve input, and suggestions can be selected first", async (t) => {
  const { w, d, calls } = await setup(t, {
    "/api/runs": (_, options) => options.method === "POST" ? run("keyboard") : { runs: [] },
  });
  const question = d.querySelector("#question");
  question.focus();
  assert.equal(d.querySelector("#suggestions").hidden, true);
  question.value = "How many available";
  question.dispatchEvent(new w.Event("input"));
  question.dispatchEvent(new w.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
  const newline = new w.KeyboardEvent("keydown", { key: "Enter", shiftKey: true, bubbles: true, cancelable: true });
  question.dispatchEvent(newline);
  assert.equal(newline.defaultPrevented, false);
  assert.equal(question.value, "How many available");
  const composition = new w.KeyboardEvent("keydown", { key: "Enter", isComposing: true, bubbles: true, cancelable: true });
  question.dispatchEvent(composition);
  assert.equal(composition.defaultPrevented, false);
  question.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
  assert.equal(question.value, "How many available units are in the East warehouse?");
  assert.equal(calls.filter(([, options]) => options.method === "POST").length, 0);
  question.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
  await tick();
  assert.equal(calls.filter(([, options]) => options.method === "POST").length, 1);
  assert.equal(d.querySelector("[data-run-button]").getAttribute("aria-label"), "Send question");
});
test("sending follows the newest message while a later reply respects reading position", async (t) => {
  let resolve;
  const { d } = await setup(t, {
    "/api/runs": (_, options) => options.method === "POST"
      ? new Promise(done => { resolve = done; }) : { runs: [] },
  });
  const scroll = d.querySelector("[data-chat-scroll]"), scrolls = [];
  Object.defineProperties(scroll, {
    scrollHeight: { value: 1200 },
    clientHeight: { value: 500 },
  });
  scroll.scrollTo = options => { scrolls.push(options.top); scroll.scrollTop = options.top; };
  d.querySelector("#question").value = "First question";
  d.querySelector("#analysis-form").requestSubmit();
  assert.deepEqual(scrolls, [1200]);
  scroll.scrollTop = 0;
  resolve(run("read"));
  await tick();
  assert.deepEqual(scrolls, [1200]);
  d.querySelector("#question").value = "Next question";
  d.querySelector("#analysis-form").requestSubmit();
  scroll.scrollTop = 700;
  resolve(run("follow"));
  await tick();
  assert.deepEqual(scrolls, [1200, 1200, 1200]);
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
  d.querySelector('[data-nav="ask"]').click();
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
