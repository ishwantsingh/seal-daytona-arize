(function () {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const question = $("#question"),
    suggestions = $("#suggestions"),
    runButton = $("[data-run-button]"),
    chatMessages = $("[data-chat-messages]");
  const sendButtonContent = runButton.innerHTML;
  try {
    question.value = sessionStorage.getItem("selfHealLastQuestion") || "";
  } catch {}
  const inventoryExamples = [
    "How many available units are in the East warehouse?",
    "How many available units are in the West warehouse?",
    "What are the available units in each warehouse?",
  ];
  const logisticsExamples = [
    "How many customers sent more than 15 shipments from warehouse 3 yesterday?",
  ];
  let examples = logisticsExamples,
    datasets = [],
    selectedSource = null,
    serviceReady = false,
    healthData = null;
  let activeSuggestion = -1,
    visibleSuggestions = [],
    selectedRun = null,
    currentPage = 0;
  let activeTab = "sqlite",
    runs = [],
    historyPage = 0,
    evaluations = [],
    versions = [],
    versionData = null;
  let toastTimer,
    submitting = false,
    routeSequence = 0,
    evolutionSequence = 0,
    harnessSequence = 0,
    previewSequence = 0;
  const PAGE_SIZE = 10;
  const CHAT_STORAGE_KEY = "selfHealChat";
  const chatEntries = [];
  let chatSource = null,
    chatScrollTop = 0,
    chatSaveTimer,
    chatSequence = 0,
    shownPage = null;
  let evaluationPage = 0,
    versionPage = 0,
    currentWorkflowId = null,
    comparisonSequence = 0;
  const sourceDiffs = new Map();
  function stateMessage(root, title, message, retry, kind = "error") {
    const box = el("div", "state-message");
    box.dataset.state = kind;
    box.setAttribute("role", kind === "error" ? "alert" : "status");
    box.append(el("strong", "", title), el("p", "", message));
    if (retry) {
      const button = el("button", "secondary-button", "Retry");
      button.type = "button";
      button.addEventListener("click", retry);
      box.append(button);
    }
    root.replaceChildren(box);
    root.hidden = false;
    root.removeAttribute("aria-busy");
  }
  function loading(root, label) {
    root.replaceChildren();
    root.setAttribute("aria-busy", "true");
    const box = el("div", "loading-state");
    box.setAttribute("role", "status");
    box.append(el("span", "sr-only", label));
    for (let i = 0; i < 3; i++) box.append(el("div", "skeleton"));
    root.append(box);
  }
  function loaded(root) {
    root.removeAttribute("aria-busy");
  }
  function autosize() {
    question.style.height = "auto";
    question.style.height =
      Math.min(180, Math.max(56, question.scrollHeight)) + "px";
  }
  function saveDraft() {
    try {
      sessionStorage.setItem("selfHealLastQuestion", question.value);
    } catch {}
    saveChat();
  }
  function saveChat() {
    try {
      localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify({
        version: 1,
        messages: chatEntries,
        draft: question.value,
        source: chatSource,
        scrollTop: chatScrollTop,
      }));
    } catch {}
  }
  function syncContext(page) {
    const source = page === "run-details" && selectedRun?.dataset
      ? selectedRun.dataset : selectedSource;
    const family =
      page === "harness"
        ? $("[data-harness-family]").value
        : page === "versions"
          ? $("[data-version-family]").value
          : null;
    const logistics = family
      ? family === "logistics-shipment-threshold"
      : source?.input_kind === "logistics_bundle";
    set(
      "[data-workspace-title]",
      page === "evolve"
        ? "Evolution jobs"
        : (logistics ? "Logistics" : "Inventory") +
            " · " +
            (family ? "Harness" : "Analyst"),
    );
    if (healthData) {
      const version = logistics
        ? healthData.logistics_active_version
        : healthData.active_version;
      set("[data-active-version]", version || "Unavailable");
      $("[data-active-version]").title = version || "Unavailable";
    }
  }
  function renderExamples() {
    const root = $("[data-examples]");
    root.replaceChildren();
    examples.forEach((text) => {
      const button = el("button", "", text);
      button.type = "button";
      button.addEventListener("click", () => {
        question.value = text;
        saveDraft();
        autosize();
        closeSuggestions();
        question.focus();
      });
      root.append(button);
    });
  }
  let selectedEvolutionId = null,
    evolutionCursor = 0,
    evolutionTimer = null,
    harnessWorkflows = [];
  function el(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null)
      node.textContent = String(content);
    return node;
  }
  function set(selector, value) {
    $(selector).textContent =
      value === undefined || value === null || value === ""
        ? "—"
        : String(value);
  }
  function announce(message) {
    const toast = $("[data-announcement]");
    clearTimeout(toastTimer);
    toast.textContent = message;
    toast.hidden = false;
    toastTimer = setTimeout(() => (toast.hidden = true), 5000);
  }
  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      path === "/api/runs" && options.method === "POST" ? 100000 : 15000,
    );
    try {
      const response = await fetch(path, {
        ...options,
        signal: controller.signal,
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(body.error || "Request failed");
        error.status = response.status;
        throw error;
      }
      return body;
    } catch (error) {
      if (error.name === "AbortError")
        throw new Error(
          "The server did not respond. Check that the UI server is running, then try again.",
        );
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }
  function outcomeName(outcome) {
    return outcome === "answered"
      ? "Answered"
      : outcome === "unsupported"
        ? "Capability gap"
        : outcome === "error"
          ? "Error"
          : outcome || "Recorded";
  }
  function date(value) {
    return value ? new Date(value).toLocaleString() : "—";
  }
  function closeSuggestions() {
    suggestions.hidden = true;
    question.removeAttribute("aria-activedescendant");
    activeSuggestion = -1;
  }
  function updateRunButton() {
    runButton.disabled = submitting || !serviceReady || !selectedSource?.id;
    runButton.title = submitting
      ? "Analysis in progress"
      : !serviceReady
        ? "Connect to SQLite before running an analysis"
        : !selectedSource?.id
          ? "Load a data source before running an analysis"
          : "Run analysis (Cmd/Ctrl + Enter)";
  }
  function renderDatabasePreview(data) {
    const relations = $("[data-database-relations]"),
      content = $("[data-database-content]");
    relations.replaceChildren();
    Object.entries(data.relations || {}).forEach(([name, count]) => {
      const button = el("button", "", `${name} · ${count}`);
      button.type = "button";
      button.setAttribute("aria-selected", String(name === data.relation));
      button.addEventListener("click", () => {
        if (name !== data.relation) loadDatabasePreview(name);
      });
      relations.append(button);
    });
    content.replaceChildren();
    if (!data.rows?.length) {
      content.append(el("p", "database-empty", "This table has no rows."));
      return;
    }
    const scroll = el("div", "database-scroll"),
      table = el("table", "database-table"),
      head = el("thead"),
      header = el("tr"),
      body = el("tbody");
    data.columns.forEach((column) => {
      const cell = el("th", "", column);
      cell.scope = "col";
      header.append(cell);
    });
    head.append(header);
    data.rows.forEach((row) => {
      const line = el("tr");
      data.columns.forEach((column) =>
        line.append(el("td", "", row[column] ?? "—")),
      );
      body.append(line);
    });
    table.append(head, body);
    scroll.append(table);
    content.append(
      scroll,
      el("div", "database-footer", `Showing ${data.rows.length} of ${data.row_count} rows · SQLite snapshot`),
    );
  }
  async function loadDatabasePreview(relation = "") {
    const sequence = ++previewSequence,
      source = selectedSource,
      content = $("[data-database-content]");
    if (!source?.id) {
      $("[data-database-relations]").replaceChildren();
      content.replaceChildren(
        el("p", "database-empty", "Load a data source to see its tables."),
      );
      return;
    }
    loading(content, "Loading database preview");
    const params = new URLSearchParams({
      dataset_id: source.id,
      input_kind: source.input_kind,
    });
    if (relation) params.set("relation", relation);
    try {
      const data = await request("/api/datasets/preview?" + params);
      if (sequence !== previewSequence) return;
      loaded(content);
      renderDatabasePreview(data);
    } catch (error) {
      if (sequence !== previewSequence) return;
      const message = error.status === 404
        ? "The running server needs an update. Restart seal ui from this repository, then retry."
        : error.message;
      stateMessage(content, "Preview unavailable", message, () =>
        loadDatabasePreview(relation),
      );
    }
  }
  function setSource(source, clearQuestion = false, remember = false) {
    selectedSource = source;
    if (remember)
      chatSource = source ? { id: source.id, input_kind: source.input_kind } : null;
    set("[data-database-source]", source?.id || "No data source selected");
    loadDatabasePreview();
    const logistics = source && source.input_kind === "logistics_bundle";
    examples = logistics ? logisticsExamples : inventoryExamples;
    syncContext(location.hash.slice(1).split("/")[0] || "ask");
    document.title =
      "SEAL · " + (logistics ? "Logistics analyst" : "Inventory analyst");
    set(
      "[data-ask-title]",
      logistics ? "Ask about shipments" : "Ask your inventory",
    );
    question.placeholder = "Ask about your data…";
    set(
      "[data-source-note]",
      healthData && !healthData.logistics_active_version
        ? "Restart the local server to enable logistics analysis."
        : logistics
          ? source.id
            ? "Customers · warehouses · shipments"
            : "Load the public logistics bundle to run this question"
          : "Inventory table",
    );
    if (healthData)
      set(
        "[data-active-version]",
        logistics
          ? healthData.logistics_active_version
          : healthData.active_version,
      );
    $("[data-load-logistics]").hidden = !logistics || !!source.id;
    $("[data-load-logistics]").disabled = Boolean(
      healthData && !healthData.logistics_active_version,
    );
    if (clearQuestion) question.value = logistics ? logisticsExamples[0] : "";
    autosize();
    renderExamples();
    closeSuggestions();
    updateRunButton();
    saveChat();
  }
  async function loadDatasets(preferredId) {
    const picker = $("#data-source");
    try {
      datasets = ((await request("/api/datasets")).datasets || []).filter(
        (item) => !/^(eval-|incident-|private-|final-)/i.test(item.id || ""),
      );
      picker.replaceChildren();
      const logistics = datasets.filter(
        (item) => item.input_kind === "logistics_bundle",
      );
      const inventory = datasets.filter(
        (item) => item.input_kind !== "logistics_bundle",
      );
      if (!logistics.length)
        picker.append(
          new Option("Logistics demo · load data", "logistics:pending"),
        );
      logistics.forEach((item) =>
        picker.append(
          new Option(
            `Logistics · ${item.id} · ${item.relations?.shipments ?? item.row_count} shipments`,
            `logistics:${item.id}`,
          ),
        ),
      );
      inventory.forEach((item) =>
        picker.append(
          new Option(`Inventory · ${item.id}`, `inventory:${item.id}`),
        ),
      );
      const chosen =
        datasets.find(
          (item) => item.id === (preferredId || selectedSource?.id || chatSource?.id),
        ) ||
        logistics[0] ||
        inventory[0] ||
        null;
      if (chosen) {
        picker.value =
          (chosen.input_kind === "logistics_bundle"
            ? "logistics:"
            : "inventory:") + chosen.id;
        setSource(chosen, false, true);
      } else {
        picker.value = "logistics:pending";
        setSource({ input_kind: "logistics_bundle", id: "" }, false);
      }
    } catch (error) {
      picker.replaceChildren(
        new Option("Logistics demo · load data", "logistics:pending"),
      );
      setSource({ input_kind: "logistics_bundle", id: "" }, false);
      if (!healthData || healthData.logistics_active_version)
        set("[data-source-note]", error.message);
    }
  }
  $("#data-source").addEventListener("change", (event) => {
    const value = event.target.value;
    setSource(
      value === "logistics:pending"
        ? { input_kind: "logistics_bundle", id: "" }
        : datasets.find((item) => value.endsWith(":" + item.id)) || null,
      false,
      true,
    );
    if (location.hash === "#versions") loadVersions();
  });
  $("[data-load-logistics]").addEventListener("click", async () => {
    if (!serviceReady) {
      set(
        "[data-source-note]",
        "Restart the UI server to enable logistics data",
      );
      return;
    }
    const button = $("[data-load-logistics]");
    button.disabled = true;
    button.textContent = "Loading logistics…";
    try {
      const data = await request("/api/datasets/logistics", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      await loadDatasets(data.id);
      announce("Logistics bundle ready");
    } catch (error) {
      announce(error.message);
      set("[data-source-note]", error.message);
    } finally {
      button.disabled = false;
      button.textContent = "Load demo data";
    }
  });
  function paintActive() {
    [...suggestions.querySelectorAll('[role="option"]')].forEach(
      (node, index) =>
        node.setAttribute("aria-selected", String(index === activeSuggestion)),
    );
    if (activeSuggestion >= 0)
      question.setAttribute(
        "aria-activedescendant",
        "suggestion-" + activeSuggestion,
      );
    else question.removeAttribute("aria-activedescendant");
  }
  function openSuggestions() {
    const query = question.value.trim().toLowerCase();
    if (!query || chatMessages.children.length) {
      closeSuggestions();
      return;
    }
    visibleSuggestions = examples.filter(
      (item) => !query || item.toLowerCase().includes(query),
    );
    if (!visibleSuggestions.length) {
      closeSuggestions();
      return;
    }
    suggestions.replaceChildren(
      el("div", "suggestion-label", "Suggested questions"),
    );
    visibleSuggestions.forEach((item, index) => {
      const option = el("button", "suggestion", item);
      option.type = "button";
      option.id = "suggestion-" + index;
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", "false");
      option.addEventListener("mousedown", (event) => event.preventDefault());
      option.addEventListener("click", () => chooseSuggestion(index));
      suggestions.append(option);
    });
    if (!visibleSuggestions.length)
      suggestions.append(
        el(
          "div",
          "empty-state",
          "No matching suggestions. You can ask your own question.",
        ),
      );
    suggestions.append(
      el(
        "div",
        "suggestion-help",
        "↑ ↓ to navigate · Enter to select · Esc to close",
      ),
    );
    suggestions.hidden = false;
    activeSuggestion = -1;
  }
  function chooseSuggestion(index) {
    if (!visibleSuggestions[index]) return;
    question.value = visibleSuggestions[index];
    question.setCustomValidity("");
    saveDraft();
    autosize();
    closeSuggestions();
    question.focus();
    announce("Suggested question selected");
  }
  question.addEventListener("focus", openSuggestions);
  question.addEventListener("input", () => {
    question.setCustomValidity("");
    saveDraft();
    autosize();
    openSuggestions();
  });
  question.addEventListener("keydown", (event) => {
    if (event.isComposing) return;
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      closeSuggestions();
      $("#analysis-form").requestSubmit();
      return;
    }
    if (event.key === "Escape") {
      if (!suggestions.hidden) {
        event.preventDefault();
        closeSuggestions();
      }
      return;
    }
    if (
      !suggestions.hidden &&
      (event.key === "ArrowDown" || event.key === "ArrowUp")
    ) {
      event.preventDefault();
      if (suggestions.hidden) openSuggestions();
      if (visibleSuggestions.length)
        activeSuggestion =
          (activeSuggestion +
            (event.key === "ArrowDown" ? 1 : -1) +
            visibleSuggestions.length) %
          visibleSuggestions.length;
      paintActive();
      return;
    }
    if (event.key === "Enter" && !event.shiftKey && !suggestions.hidden && activeSuggestion >= 0) {
      event.preventDefault();
      chooseSuggestion(activeSuggestion);
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("#analysis-form").requestSubmit();
    }
  });
  document.addEventListener("pointerdown", (event) => {
    if (!$("#analysis-form").contains(event.target)) closeSuggestions();
  });
  function disclosure(label, value) {
    const details = el("details", "disclosure"),
      summary = el("summary", "", label),
      pre = el("pre", "", JSON.stringify(value, null, 2));
    details.append(summary, pre);
    return details;
  }
  function renderSQLite(run) {
    const root = $("[data-sqlite-content]");
    root.replaceChildren();
    currentPage = 0;
    const sqlite = run.evidence && run.evidence.sqlite,
      measured = run.resources || {};
    if (
      run.dataset?.input_kind === "logistics_bundle" &&
      run.dataset.relations
    ) {
      const summary = el("div", "relation-summary");
      Object.entries(run.dataset.relations).forEach(([name, meta]) => {
        const count = typeof meta === "object" ? meta.row_count : meta;
        const card = el("div", "relation-card");
        card.append(el("strong", "", count), el("span", "", name));
        summary.append(card);
      });
      root.append(summary);
    }
    const pageCount = sqlite
      ? sqlite.pages.length || Number(measured.table_pages || 0)
      : Number(measured.table_pages || 0);
    set(
      "[data-sqlite-summary]",
      pageCount + " " + (pageCount === 1 ? "page" : "pages"),
    );
    if (!sqlite) {
      root.append(
        el(
          "p",
          "empty-state",
          pageCount
            ? "This older run did not store its row snapshot."
            : "0 table pages read. No SQLite rows were read for this run.",
        ),
      );
      return;
    }
    const source = el("div", "source-line");
    source.append(
      el("span", "", "Collection: "),
      el("strong", "", sqlite.collection || "analyst_rows"),
      el("span", "", "Source: "),
      el("strong", "", sqlite.source || (run.dataset && run.dataset.id) || "—"),
      el("span", "", sqlite.row_count + " rows actually read"),
    );
    root.append(source);
    if (!sqlite.pages.length) {
      root.append(
        el(
          "p",
          "empty-state",
          sqlite.row_count
            ? `${sqlite.row_count} shipment rows read through the bounded tool. Raw rows are not retained in UI evidence.`
            : "0 rows read. No SQLite table data was used.",
        ),
      );
      return;
    }
    const view = el("div"),
      pager = el("div", "pager");
    root.append(view, pager);
    function showPage() {
      view.replaceChildren();
      pager.replaceChildren();
      const page = sqlite.pages[currentPage],
        rows = page.rows || [],
        columns = sqlite.columns || [];
      const wrap = el("div", "table-wrap"),
        table = el("table"),
        head = el("thead"),
        headerRow = el("tr");
      columns.forEach((column) => {
        const th = el("th", "", column);
        th.scope = "col";
        headerRow.append(th);
      });
      head.append(headerRow);
      table.append(head);
      const tbody = el("tbody");
      rows.forEach((row) => {
        const tr = el("tr");
        columns.forEach((column) =>
          tr.append(
            el(
              "td",
              typeof row[column] === "number" ? "numeric" : "",
              row[column] === undefined ? "—" : row[column],
            ),
          ),
        );
        tbody.append(tr);
      });
      table.append(tbody);
      wrap.append(table);
      view.append(wrap);
      const prev = el("button", "", "← Previous"),
        next = el("button", "", "Next →");
      prev.type = next.type = "button";
      prev.disabled = currentPage === 0;
      next.disabled = currentPage === sqlite.pages.length - 1;
      prev.addEventListener("click", () => {
        currentPage--;
        showPage();
      });
      next.addEventListener("click", () => {
        currentPage++;
        showPage();
      });
      pager.append(
        prev,
        el(
          "span",
          "",
          "Page " +
            (currentPage + 1) +
            " of " +
            sqlite.pages.length +
            " · " +
            page.row_count +
            " rows",
        ),
        next,
      );
    }
    showPage();
    root.append(disclosure("Raw data read during this run", sqlite.pages));
  }
  function renderTools(run) {
    const root = $("[data-tool-content]");
    root.replaceChildren();
    let calls = run.evidence && run.evidence.tool_calls;
    let recovered = false;
    if (
      !calls &&
      Array.isArray(run.spans) &&
      run.resources &&
      run.resources.tool_calls
    ) {
      calls = run.spans
        .filter((span) => span.name && span.name.startsWith("tool."))
        .map((span) => ({
          name: span.name.slice(5),
          arguments: span.arguments,
          result: span.result_preview,
          duration_ms: span.duration_ms,
          error: span.error,
        }));
      recovered = Boolean(calls.length);
    }
    const callCount = calls
      ? calls.length
      : (run.resources && run.resources.tool_calls) || 0;
    set(
      "[data-tools-summary]",
      callCount + " " + (callCount === 1 ? "call" : "calls"),
    );
    if (!calls) {
      root.append(
        el(
          "p",
          "empty-state",
          run.resources && run.resources.tool_calls
            ? "Tool details were not stored for this older run."
            : "0 tool calls recorded.",
        ),
      );
      return;
    }
    if (!calls.length) {
      root.append(
        el(
          "p",
          "empty-state",
          "0 tool calls recorded. The analyst did not invoke a tool.",
        ),
      );
      return;
    }
    if (recovered)
      root.append(
        el("p", "muted", "Recovered from the redacted Arize Phoenix trace."),
      );
    calls.forEach((call, index) => {
      const details = el("details", "tool-row"),
        summary = el("summary"),
        name = el("strong", "", index + 1 + ". " + call.name);
      summary.append(
        name,
        el(
          "span",
          "",
          call.duration_ms == null
            ? "Timing unavailable"
            : call.duration_ms + " ms",
        ),
        el(
          "span",
          call.error ? "trace-error" : "",
          call.error ? "Error" : "Completed",
        ),
      );
      const content = el("div", "tool-detail");
      const args = el("div"),
        result = el("div");
      args.append(
        el("label", "", "Arguments"),
        el("pre", "", JSON.stringify(call.arguments, null, 2)),
      );
      result.append(
        el("label", "", "Result preview"),
        el(
          "pre",
          "",
          JSON.stringify(
            call.result || { error: call.error || "No result" },
            null,
            2,
          ),
        ),
      );
      content.append(args, result);
      details.append(summary, content);
      root.append(details);
    });
  }
  function renderTrace(run) {
    const root = $("[data-trace-content]");
    root.replaceChildren();
    const link = $("[data-trace-link]"),
      trace = run.trace || {};
    const safeUrl =
      typeof trace.url === "string" && /^https:\/\//.test(trace.url);
    link.hidden = !safeUrl;
    if (safeUrl) link.href = trace.url;
    else link.removeAttribute("href");
    if (!Array.isArray(run.spans)) {
      root.append(
        el(
          "p",
          "empty-state",
          trace.status === "disabled"
            ? "Tracing was disabled for this run."
            : trace.status === "available"
              ? "Span metadata could not be loaded. Open the full trace."
              : "No verified trace spans are available.",
        ),
      );
      return;
    }
    if (!run.spans.length) {
      root.append(el("p", "empty-state", "No trace spans were returned."));
      return;
    }
    run.spans.forEach((span) => {
      const details = el("details", "trace-row"),
        summary = el("summary");
      summary.append(
        el("strong", "", span.name || span.type || "Span"),
        el("span", "", span.type || "—"),
        el(
          "span",
          span.status === "error" ? "trace-error" : "",
          span.status || "—",
        ),
        el(
          "span",
          "",
          span.duration_ms == null ? "—" : span.duration_ms + " ms",
        ),
      );
      const meta = el("div", "source-line");
      meta.append(
        el("span", "", "Model: " + (span.model || "—")),
        el("span", "", "Tokens: " + (span.tokens ?? "—")),
        el("span", "", "Started: " + date(span.start_time)),
      );
      if (span.error) meta.append(el("span", "trace-error", span.error));
      details.append(summary, meta);
      root.append(details);
    });
  }
  function renderGap(run) {
    const panel = $("#candidate-check"),
      root = $("[data-gap-content]");
    panel.hidden = run.outcome !== "unsupported";
    root.replaceChildren();
    if (panel.hidden) return;
    const gap = run.gap || { cases: [], candidates: [], active_version: "—" };
    root.append(
      el(
        "p",
        "gap-note",
        (run.limitation_reason ||
          "The analyst could not answer with the current capability.") +
          " Active version remains " +
          gap.active_version +
          ".",
      ),
    );
    const caseLinks = el("div", "case-links");
    (gap.cases || []).forEach((item) => {
      const link = el("button", "text-button", "Evaluation case " + item.id);
      link.type = "button";
      link.addEventListener("click", async () => {
        try {
          const data = await request(item.url);
          const details = disclosure("Case details", data);
          details.open = true;
          caseLinks.after(details);
        } catch (error) {
          announce(error.message);
        }
      });
      caseLinks.append(link);
    });
    if (caseLinks.children.length) root.append(caseLinks);
    if (!(gap.candidates || []).length) {
      root.append(
        el("p", "empty-state", "No candidate test is recorded for this gap."),
      );
      return;
    }
    gap.candidates.forEach((candidate) => {
      const row = el("div", "candidate-row");
      row.append(
        el("strong", "", candidate.id),
        el(
          "span",
          candidate.status === "Rejected" ? "rejected" : "",
          candidate.status,
        ),
      );
      const checks = el("div", "checks");
      [
        ["Correctness", candidate.correctness],
        ["Regression", candidate.regression],
      ].forEach(([label, value]) =>
        checks.append(el("span", value, label + ": " + value)),
      );
      row.append(checks);
      if (candidate.diff_url) {
        const link = el("button", "text-button", "View candidate diff ↗");
        link.type = "button";
        link.addEventListener("click", async () => {
          try {
            const data = await request(candidate.diff_url);
            const details = disclosure("Candidate source diff", data.diff);
            details.open = true;
            row.after(details);
          } catch (error) {
            announce(error.message);
          }
        });
        row.append(link);
      }
      root.append(row);
      if (candidate.reasons && candidate.reasons.length)
        root.append(el("p", "muted", candidate.reasons.join("; ")));
    });
  }
  function showTab(name) {
    activeTab = name;
    document.querySelectorAll("[data-tab]").forEach((button) => {
      const selected = button.dataset.tab === name;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    document
      .querySelectorAll("[data-panel]")
      .forEach((panel) => (panel.hidden = panel.dataset.panel !== name));
  }
  function renderTimeline(run) {
    const root = $("[data-timeline-content]");
    root.replaceChildren();
    const resources = run.resources || {},
      calls = (run.evidence && run.evidence.tool_calls) || [];
    set(
      "[data-timeline-total]",
      resources.elapsed_seconds !== undefined
        ? "Total: " + resources.elapsed_seconds + " s"
        : "",
    );
    const steps = [
      ["Request received", "Question accepted for analysis"],
      ...calls.map((call) => [
        call.name || "Tool call",
        call.error
          ? "Error: " + call.error
          : "Arguments and result available under Tool calls",
      ]),
      [
        run.outcome === "unsupported"
          ? "Capability gap recorded"
          : run.outcome === "error"
            ? "Execution failed"
            : "Answer recorded",
        run.outcome === "unsupported"
          ? run.limitation_reason || "Unsupported request"
          : run.outcome === "error"
            ? run.error || run.message || "Run failed"
            : "Answer and evidence stored",
      ],
    ];
    if (run.outcome === "unsupported" && run.gap) {
      if ((run.gap.cases || []).length)
        steps.push([
          "Evaluation case created",
          `${run.gap.cases.length} recorded case${run.gap.cases.length === 1 ? "" : "s"}`,
        ]);
      if ((run.gap.candidates || []).length)
        steps.push([
          "Candidate checked",
          `${run.gap.candidates.length} recorded candidate${run.gap.candidates.length === 1 ? "" : "s"}`,
        ]);
    }
    steps.forEach(([title, detail], index) => {
      const row = el("div", "timeline-row");
      row.append(
        el("span", "timeline-number", index + 1),
        el("div", "timeline-text"),
      );
      row.lastChild.append(el("strong", "", title), el("p", "muted", detail));
      root.append(row);
    });
    const related = $("[data-related-evolution]");
    related.replaceChildren();
    if (run.evolution_job_id) {
      const link = el("a", "row-link", "Inspect evolution →");
      link.href = "#evolve/" + encodeURIComponent(run.evolution_job_id);
      related.append(link);
    } else
      related.append(
        el("p", "muted", "No evolution job is linked to this run."),
      );
  }
  function svgNode(tag) {
    return document.createElementNS("http://www.w3.org/2000/svg", tag);
  }
  function componentInspector(node, graph, root) {
    root.replaceChildren();
    if (!node) {
      root.append(
        el("p", "empty-state", "Select a workflow component to inspect it."),
      );
      return;
    }
    root.append(
      el(
        "div",
        "inspector-kind",
        (node.kind || "component").replaceAll("_", " "),
      ),
      el("h3", "", node.label),
      el("p", "muted", node.summary || "No saved summary."),
    );
    const incoming = (graph.edges || []).filter(
        (edge) => edge.target === node.id,
      ),
      outgoing = (graph.edges || []).filter((edge) => edge.source === node.id);
    const list = el("dl", "component-facts");
    [
      ["ID", node.id],
      ["Group", node.group || "—"],
      ["Inputs", incoming.length],
      ["Outputs", outgoing.length],
    ].forEach(([term, value]) => {
      list.append(el("dt", "", term), el("dd", "", value));
    });
    root.append(list);
    if (node.source && node.source.length) {
      const sources = el("div", "component-sources");
      sources.append(el("strong", "", "Source"));
      node.source.forEach((item) => sources.append(el("span", "", item.path)));
      root.append(sources);
    }
    if (node.kind === "tool_group")
      root.append(
        el(
          "p",
          "muted",
          "Tools are nested under the owning agent. Their availability is structural; recorded use is shown only in linked run evidence.",
        ),
      );
  }
  function workflowWithProposal(graph, proposal) {
    if (
      !graph ||
      !proposal ||
      !Array.isArray(proposal.additions) ||
      !proposal.additions.length
    )
      return graph;
    const next = {
      ...graph,
      nodes: [...(graph.nodes || [])],
      edges: [...(graph.edges || [])],
      metadata: { ...(graph.metadata || {}) },
    };
    const agent = next.nodes.find((node) => node.kind === "agent");
    proposal.additions.forEach((item, index) => {
      const id = item.id,
        node = {
          ...item,
          summary:
            "Proposed tool. It is not executable until screened and committed.",
          layout: { x: 545, y: 380 + index * 78 },
          fingerprint: id,
          source: [],
        };
      next.nodes.push(node);
      if (agent)
        next.edges.push({
          id: agent.id + ">proposes>" + id,
          source: agent.id,
          target: id,
          relation: "proposes",
          label: "proposes",
        });
    });
    return next;
  }
  function renderWorkflow(graph, canvas, listRoot, inspector, proposal) {
    const selectedNode = canvas.querySelector(".workflow-node.selected")
        ?.dataset.nodeId,
      scrollLeft = canvas.scrollLeft,
      scrollTop = canvas.scrollTop;
    canvas.replaceChildren();
    listRoot.replaceChildren();
    componentInspector(null, null, inspector);
    const view = workflowWithProposal(graph, proposal);
    canvas.classList.toggle("is-empty", !view?.nodes?.length);
    if (!view || !Array.isArray(view.nodes) || !view.nodes.length) {
      canvas.append(
        el(
          "p",
          "empty-state",
          "No saved workflow is available for this version.",
        ),
      );
      inspector.replaceChildren(
        el(
          "p",
          "muted",
          "Component evidence appears when a workflow is recorded.",
        ),
      );
      return;
    }
    const svg = svgNode("svg");
    const xs = view.nodes.map((node) => node.layout?.x || 0),
      ys = view.nodes.map((node) => node.layout?.y || 0);
    const left = Math.min(...xs) - 24,
      top = Math.min(...ys) - 24,
      width = Math.max(...xs) - left + 188,
      height = Math.max(...ys) - top + 82;
    svg.setAttribute("viewBox", `${left} ${top} ${width} ${height}`);
    svg.setAttribute("role", "group");
    svg.setAttribute("aria-label", "Harness workflow diagram");
    svg.classList.add("workflow-svg");
    const byId = new Map(view.nodes.map((node) => [node.id, node]));
    (view.edges || []).forEach((edge) => {
      const a = byId.get(edge.source),
        b = byId.get(edge.target);
      if (!a || !b) return;
      const line = svgNode("line"),
        pa = a.layout || {},
        pb = b.layout || {};
      line.setAttribute("x1", (pa.x || 0) + 164);
      line.setAttribute("y1", (pa.y || 0) + 28);
      line.setAttribute("x2", pb.x || 0);
      line.setAttribute("y2", (pb.y || 0) + 28);
      line.setAttribute("class", "workflow-edge " + (edge.relation || ""));
      svg.append(line);
    });
    view.nodes.forEach((node) => {
      const point = node.layout || {},
        group = svgNode("g"),
        rect = svgNode("rect"),
        title = svgNode("text"),
        subtitle = svgNode("text");
      group.dataset.nodeId = node.id;
      group.setAttribute(
        "transform",
        `translate(${point.x || 0} ${point.y || 0})`,
      );
      group.setAttribute("tabindex", "0");
      group.setAttribute("role", "button");
      group.setAttribute("aria-label", "Inspect " + node.label);
      group.setAttribute(
        "class",
        "workflow-node " + (node.kind || "") + " " + (node.status || ""),
      );
      rect.setAttribute("width", "164");
      rect.setAttribute("height", "58");
      rect.setAttribute("rx", "8");
      title.setAttribute("x", "12");
      title.setAttribute("y", "24");
      title.textContent =
        node.label?.length > 24 ? node.label.slice(0, 23) + "…" : node.label;
      subtitle.setAttribute("x", "12");
      subtitle.setAttribute("y", "43");
      subtitle.textContent = (node.kind || "component").replaceAll("_", " ");
      group.append(rect, title, subtitle);
      const inspect = () => {
        svg.querySelectorAll(".workflow-node").forEach((item) => {
          item.classList.toggle("selected", item === group);
          item.setAttribute("aria-pressed", String(item === group));
        });
        componentInspector(node, view, inspector);
      };
      group.addEventListener("click", inspect);
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          inspect();
        }
      });
      svg.append(group);
    });
    let zoom = Number(canvas.dataset.zoom) || 1;
    svg.style.width = zoom * 100 + "%";
    const controls = el("div", "canvas-controls");
    controls.append(
      el("span", "canvas-legend", "Dashed border · grouped or proposed"),
    );
    [
      ["−", "Zoom out", () => Math.max(0.5, zoom - 0.25)],
      ["+", "Zoom in", () => Math.min(4, zoom + 0.25)],
      ["Fit", "Fit workflow", () => 1],
    ].forEach(([label, name, change]) => {
      const button = el("button", "", label);
      button.type = "button";
      button.setAttribute("aria-label", name);
      button.addEventListener("click", () => {
        zoom = change();
        canvas.dataset.zoom = zoom;
        svg.style.width = zoom * 100 + "%";
        canvas.scrollTo({ left: 0, top: 0 });
      });
      controls.append(button);
    });
    canvas.append(controls, svg);
    if (!(view.metadata?.runtime_mcp_servers || []).length)
      listRoot.append(
        el(
          "p",
          "workflow-note",
          "No runtime MCP servers are configured for this harness.",
        ),
      );
    const list = el("ul", "workflow-accessible-list");
    view.nodes.forEach((node) => {
      const button = el(
        "button",
        "",
        node.label + " · " + (node.kind || "component").replaceAll("_", " "),
      );
      button.type = "button";
      button.addEventListener("click", () => {
        const match = [...svg.querySelectorAll(".workflow-node")].find(
          (group) => group.dataset.nodeId === node.id,
        );
        match?.dispatchEvent(new Event("click"));
      });
      const item = el("li");
      item.append(button);
      list.append(item);
    });
    const equivalent = el("details", ""),
      summary = el(
        "summary",
        "",
        "Browse components (" + view.nodes.length + ")",
      );
    equivalent.append(summary, list);
    listRoot.append(equivalent);
    if (selectedNode) {
      const match = [...svg.querySelectorAll(".workflow-node")].find(
        (group) => group.dataset.nodeId === selectedNode,
      );
      match?.dispatchEvent(new Event("click"));
    }
    canvas.scrollLeft = scrollLeft;
    canvas.scrollTop = scrollTop;
  }
  function stageLabel(stage) {
    return (
      {
        queued: "Queued",
        running: "Running",
        failed: "Failed",
        canceled: "Canceled",
        cancelled: "Canceled",
        completed: "Completed",
        incident_classified: "Incident recorded",
        workflow_snapshot_ready: "Workflow saved",
        diagnosing: "Diagnosing",
        trace_read: "Trace read",
        cases_frozen: "Cases frozen",
        baseline_reproduced: "Baseline reproduced",
        proposal_received: "Tool proposal received",
        candidate_ready: "Candidate ready",
        evaluating: "Evaluation running",
        selection_decided: "Selection decided",
        promotion_completed: "Promotion completed",
        activated: "Activated",
        rejected: "Rejected",
        needs_contract: "Needs contract",
        blocked: "Blocked",
        operational_error: "Operational error",
        rerun_completed: "Rerun completed",
      }[stage] ||
      stage ||
      "Waiting"
    );
  }
  function evolutionStage(job) {
    return job.stage || job.status || "incident_classified";
  }
  function markStages(stage) {
    const order = [
      "incident_classified",
      "diagnosing",
      "proposal_received",
      "candidate_ready",
      "evaluating",
      "activated",
    ];
    const aliases = {
      queued: 0,
      running: 1,
      workflow_snapshot_ready: 0,
      trace_read: 1,
      cases_frozen: 1,
      baseline_reproduced: 1,
      selection_decided: 4,
      promotion_completed: 5,
      rerun_completed: 5,
    };
    const stopped = [
      "rejected",
      "needs_contract",
      "blocked",
      "operational_error",
      "failed",
      "canceled",
      "cancelled",
    ].includes(stage);
    const index = order.includes(stage)
      ? order.indexOf(stage)
      : (aliases[stage] ?? -1);
    document.querySelectorAll("[data-evolve-stages] li").forEach((item, i) => {
      item.className = "";
      item.removeAttribute("aria-current");
      if (!stopped && index >= 0) {
        item.classList.toggle(
          "complete",
          i < index ||
            (i === 5 &&
              ["activated", "promotion_completed", "rerun_completed"].includes(
                stage,
              )),
        );
        item.classList.toggle("current", i === index);
        if (i === index) item.setAttribute("aria-current", "step");
      }
      item.title = stopped
        ? "Evolution stopped: " + stageLabel(stage)
        : i < index
          ? "Completed"
          : i === index
            ? stageLabel(stage)
            : "Pending";
    });
    const badge = $("[data-evolve-status]");
    badge.classList.toggle("fail", stopped);
  }
  function formatResource(resources) {
    if (!resources) return "—";
    return `${resources.elapsed_seconds ?? "—"} s · ${resources.total_tokens ?? 0} tokens · ${resources.table_pages ?? 0} pages`;
  }
  function renderComparison(data) {
    const root = $("[data-evolve-chart]"),
      trials = $("[data-evolve-trials]");
    root.replaceChildren();
    trials.replaceChildren();
    const groups = data.groups || [];
    if (!groups.length) {
      root.append(
        el(
          "p",
          "empty-state",
          "Trials will appear here after the frozen evaluation plan is recorded.",
        ),
      );
    } else {
      const roles = [...new Set(groups.map((item) => item.role))];
      roles.forEach((role) => {
        const group = el("div", "comparison-group"),
          heading = el("div", "comparison-heading", role.replaceAll("_", " "));
        group.append(heading);
        ["baseline", "candidate"].forEach((version) => {
          const item = groups.find(
            (entry) => entry.role === role && entry.version === version,
          ) || {
            expected: data.scheduled?.[version]?.[role] || 0,
            completed: 0,
            passed: 0,
            failed: 0,
            pending: 0,
            metrics: {},
          };
          const row = el("div", "comparison-row"),
            label = el(
              "span",
              "",
              version === "baseline" ? "Previous" : "Updated",
            ),
            track = el("div", "comparison-track"),
            bar = el("span", "comparison-bar " + version),
            text = el(
              "span",
              "",
              `${item.passed}/${item.expected} passed · ${item.failed || 0} failed · ${item.pending || 0} pending`,
            );
          bar.style.width = `${item.expected ? Math.min(100, (100 * item.passed) / item.expected) : 0}%`;
          track.append(bar);
          row.append(label, track, text);
          group.append(row);
        });
        root.append(group);
      });
    }
    (data.trials || [])
      .slice()
      .reverse()
      .forEach((item) => {
        const row = el("tr"),
          status =
            item.passed === true
              ? "Passed"
              : item.passed === false
                ? "Failed"
                : "Pending",
          trace = el("td", "", item.trace_id || "—");
        [
          item.case_label || "Protected case",
          item.version === "baseline" ? "Previous" : "Updated",
        ].forEach((value) => row.append(el("td", "", value)));
        row.append(
          el(
            "td",
            item.passed === true
              ? "pass"
              : item.passed === false
                ? "fail"
                : "muted",
            status,
          ),
          el("td", "", formatResource(item.resources)),
          trace,
        );
        trials.append(row);
      });
    set(
      "[data-evolve-evaluation-status]",
      data.plan_id
        ? "Frozen plan · " + data.watermark + " trials"
        : "Pending plan",
    );
  }
  function renderEvolutionEvents(events) {
    const root = $("[data-evolve-events]");
    root.replaceChildren();
    if (!events.length) {
      root.append(el("p", "empty-state", "No persisted progress events yet."));
      return;
    }
    events
      .slice()
      .reverse()
      .forEach((event) => {
        const row = el("div", "event-row"),
          left = el("div", "", null);
        left.append(
          el("strong", "", stageLabel(event.stage)),
          el("p", "muted", date(event.created_at)),
        );
        const payload = event.payload || {},
          detail =
            payload.reason ||
            payload.hypothesis ||
            payload.changed_mechanism ||
            payload.candidate_commit ||
            "";
        row.append(left, el("span", "event-detail", detail));
        root.append(row);
      });
  }
  async function renderVerification(job, sequence) {
    const root = $("[data-evolve-verification]");
    root.replaceChildren();
    root.hidden = true;
    if (!job.rerun_run_id) {
      if (job.stage === "activated" || job.status === "activated") {
        root.hidden = false;
        const running = ["queued", "running"].includes(job.status);
        root.append(
          el("h2", "", "Automatic verification"),
          el(
            "p",
            "muted",
            running
              ? "Harness activated. Rerunning the original question…"
              : "Harness activated, but no verification run was recorded.",
          ),
        );
      }
      return;
    }
    try {
      const run = await request(
        "/api/runs/" + encodeURIComponent(job.rerun_run_id),
      );
      if (sequence !== evolutionSequence) return;
      root.hidden = false;
      const heading = el("div", "section-heading");
      heading.append(
        el("h2", "", "Automatic verification"),
        el("span", "outcome-badge " + run.outcome, outcomeName(run.outcome)),
      );
      const link = el("a", "text-button", "Inspect rerun evidence →");
      link.href = "#run-details/" + encodeURIComponent(run.run_id);
      root.append(
        heading,
        el("p", "", run.question || "Original question"),
        el(
          "p",
          "verification-answer",
          run.message || run.error || "No answer was recorded.",
        ),
        el("p", "mono", "Version: " + (run.version || "—")),
        link,
      );
      if (job.status === "activated")
        set("[data-active-version]", run.version || "—");
    } catch (error) {
      if (sequence !== evolutionSequence) return;
      stateMessage(
        root,
        "Verification evidence unavailable",
        error.message,
        () => loadEvolve(job.job_id || selectedEvolutionId),
      );
    }
  }
  async function loadEvolve(jobId, poll = false) {
    const sequence = ++evolutionSequence;
    clearTimeout(evolutionTimer);
    const errorRoot = $("[data-evolve-error]");
    errorRoot.hidden = true;
    $("[data-evolve-empty]").hidden = true;
    $("#evolve").classList.remove("no-job");
    if (!poll) {
      $("[data-evolve-verification]").hidden = true;
      set("[data-evolve-status]", "Loading…");
      set("[data-evolve-id]", jobId || "—");
      set("[data-job-updated]", "—");
      set("[data-evolve-run]", "—");
      $("[data-evolve-run]").removeAttribute("href");
      markStages("loading");
    }
    try {
      if (!jobId) {
        const data = await request("/api/runs?limit=100");
        if (sequence !== evolutionSequence) return;
        const candidate = (data.runs || []).find((run) => run.evolution_job_id);
        if (!candidate) {
          $("#evolve").classList.add("no-job");
          stateMessage(
            $("[data-evolve-empty]"),
            "No evolution jobs yet",
            "Run an analysis. A recorded capability gap can start an evolution job.",
            () => (location.hash = "#ask"),
            "empty",
          );
          $("[data-evolve-empty] button").textContent = "New analysis";
          set("[data-evolve-status]", "No jobs");
          set(
            "[data-evolve-subtitle]",
            "Evolution starts when an analysis records a capability gap.",
          );
          renderWorkflow(
            null,
            $("[data-evolve-workflow]"),
            $("[data-evolve-workflow-list]"),
            $("[data-evolve-inspector]"),
          );
          renderComparison({});
          renderEvolutionEvents([]);
          stateMessage(
            $("[data-evolve-changes]"),
            "No candidate yet",
            "Run an analysis to record a capability gap.",
            null,
            "empty",
          );
          return;
        }
        location.hash = "#evolve/" + candidate.evolution_job_id;
        return;
      }
      if (selectedEvolutionId !== jobId) {
        selectedEvolutionId = jobId;
        evolutionCursor = 0;
      }
      const job = await request(
        "/api/evolution-jobs/" + encodeURIComponent(jobId),
      );
      if (sequence !== evolutionSequence) return;
      set("[data-evolve-status]", stageLabel(evolutionStage(job)));
      set(
        "[data-evolve-subtitle]",
        job.reason ||
          "Inspect the recorded candidate and its protected evaluation results.",
      );
      set("[data-evolve-id]", jobId);
      set("[data-job-updated]", date(job.updated_at));
      set("[data-evolve-run]", job.incident_run_id || "—");
      if (job.incident_run_id)
        $("[data-evolve-run]").href =
          "#run-details/" + encodeURIComponent(job.incident_run_id);
      set(
        "[data-evolve-workflow-version]",
        job.workflow_revision_id?.slice(0, 18) || "Workflow pending",
      );
      set("[data-evolve-updated]", date(job.updated_at));
      markStages(evolutionStage(job));
      const candidate = job.candidate || {},
        workflowId = candidate.workflow_revision_id || job.workflow_revision_id;
      const [workflowResult, eventsResult, evaluationsResult] =
        await Promise.allSettled([
          workflowId
            ? request(
                "/api/harness-workflows/" + encodeURIComponent(workflowId),
              )
            : Promise.resolve(null),
          request(
            "/api/evolution-jobs/" +
              encodeURIComponent(jobId) +
              "/events?after=0&limit=100",
          ),
          request(
            "/api/evolution-jobs/" +
              encodeURIComponent(jobId) +
              "/evaluations?limit=100",
          ),
        ]);
      if (sequence !== evolutionSequence) return;
      if (workflowResult.status === "fulfilled")
        renderWorkflow(
          workflowResult.value?.graph,
          $("[data-evolve-workflow]"),
          $("[data-evolve-workflow-list]"),
          $("[data-evolve-inspector]"),
          candidate.workflow_revision_id
            ? null
            : candidate.workflow_proposal || job.proposal,
        );
      else
        stateMessage(
          $("[data-evolve-workflow]"),
          "Workflow unavailable",
          workflowResult.reason.message,
          () => loadEvolve(jobId),
        );
      if (eventsResult.status === "fulfilled")
        renderEvolutionEvents(eventsResult.value.events || []);
      else
        stateMessage(
          $("[data-evolve-events]"),
          "Events unavailable",
          eventsResult.reason.message,
          () => loadEvolve(jobId),
        );
      if (evaluationsResult.status === "fulfilled")
        renderComparison(evaluationsResult.value);
      else {
        $("[data-evolve-trials]").replaceChildren();
        stateMessage(
          $("[data-evolve-chart]"),
          "Evaluations unavailable",
          evaluationsResult.reason.message,
          () => loadEvolve(jobId),
        );
      }
      const changes = $("[data-evolve-changes]");
      const openedChanges = [
        ...changes.querySelectorAll("details[open] > summary"),
      ].map((node) => node.textContent);
      changes.replaceChildren();
      if (candidate.changed_mechanism)
        changes.append(el("p", "", candidate.changed_mechanism));
      if (candidate.hypothesis)
        changes.append(el("p", "proposal-note", candidate.hypothesis));
      if (candidate.candidate_commit || candidate.commit)
        changes.append(
          el(
            "p",
            "mono",
            "Candidate: " + (candidate.candidate_commit || candidate.commit),
          ),
        );
      if (candidate.workflow_proposal || job.proposal)
        changes.append(
          disclosure(
            "Recorded proposal",
            candidate.workflow_proposal || job.proposal,
          ),
        );
      if (candidate.candidate_id) {
        try {
          let diff = sourceDiffs.get(candidate.candidate_id);
          if (!diff) {
            diff = await request(
              "/api/candidates/" +
                encodeURIComponent(candidate.candidate_id) +
                "/diff",
            );
            sourceDiffs.set(candidate.candidate_id, diff);
          }
          if (sequence !== evolutionSequence) return;
          changes.append(disclosure("Candidate source diff", diff.diff));
        } catch (error) {
          if (sequence !== evolutionSequence) return;
          const failure = el("div");
          stateMessage(failure, "Source diff unavailable", error.message, () =>
            loadEvolve(jobId),
          );
          changes.append(failure);
        }
      }
      if (!changes.children.length)
        stateMessage(
          changes,
          "No candidate changes recorded",
          "Candidate evidence appears here when a proposal is saved.",
          null,
          "empty",
        );
      changes.querySelectorAll("details").forEach((details) => {
        details.open = openedChanges.includes(
          details.querySelector("summary")?.textContent,
        );
      });
      await renderVerification(job, sequence);
      if (sequence !== evolutionSequence) return;
      if (
        ["queued", "running"].includes(job.status) &&
        location.hash === "#evolve/" + jobId
      )
        evolutionTimer = setTimeout(() => loadEvolve(jobId, true), 2000);
    } catch (error) {
      if (sequence !== evolutionSequence) return;
      set("[data-evolve-status]", "Unavailable");
      markStages("operational_error");
      stateMessage(errorRoot, "Could not load evolution", error.message, () =>
        loadEvolve(jobId),
      );
      if (!poll) {
        $("#evolve").classList.add("no-job");
        renderWorkflow(
          null,
          $("[data-evolve-workflow]"),
          $("[data-evolve-workflow-list]"),
          $("[data-evolve-inspector]"),
        );
        renderComparison({});
        renderEvolutionEvents([]);
        $("[data-evolve-changes]").replaceChildren();
      }
    }
  }
  async function loadHarness(revisionId) {
    const sequence = ++harnessSequence,
      family = $("[data-harness-family]").value,
      history = $("[data-workflow-history]"),
      picker = $("[data-revision-picker]");
    syncContext("harness");
    $("[data-harness-error]").hidden = true;
    $("[data-workflow-diff]").hidden = true;
    loading(history, "Loading workflow revisions");
    picker.replaceChildren(new Option("Loading revisions…", ""));
    picker.disabled = true;
    try {
      // Resolve a deep-linked revision before consulting the selected family's
      // history; that family can legitimately have no saved revisions at all.
      const linkedWorkflow = revisionId
        ? await request(
            "/api/harness-workflows/" + encodeURIComponent(revisionId),
          )
        : null;
      if (sequence !== harnessSequence) return;
      if (
        linkedWorkflow?.task_family &&
        linkedWorkflow.task_family !== family
      ) {
        $("[data-harness-family]").value = linkedWorkflow.task_family;
        return loadHarness(revisionId);
      }
      const data = await request(
        "/api/harness-workflows?limit=50&task_family=" +
          encodeURIComponent(family),
      );
      if (sequence !== harnessSequence) return;
      harnessWorkflows = data.workflows || [];
      history.replaceChildren();
      picker.replaceChildren();
      loaded(history);
      if (!harnessWorkflows.length) {
        currentWorkflowId = null;
        $("[data-compare-revision]").disabled = true;
        history.append(el("p", "empty-state", "No saved revisions."));
        picker.append(new Option("No revisions", ""));
        set("[data-harness-workflow-version]", "—");
        renderWorkflow(
          null,
          $("[data-harness-workflow]"),
          $("[data-harness-workflow-list]"),
          $("[data-harness-inspector]"),
        );
        return;
      }
      const chosen =
        revisionId ||
        data.active_workflow_revision_id ||
        harnessWorkflows[0].workflow_revision_id;
      harnessWorkflows.forEach((item) => {
        const label =
            item.source_commit?.slice(0, 12) ||
            item.workflow_revision_id.slice(0, 12),
          active =
            item.workflow_revision_id === data.active_workflow_revision_id;
        picker.append(
          new Option(
            label + (active ? " · Active" : ""),
            item.workflow_revision_id,
          ),
        );
        const button = el("button", "workflow-history-row");
        button.type = "button";
        button.classList.toggle(
          "selected",
          item.workflow_revision_id === chosen,
        );
        button.setAttribute(
          "aria-pressed",
          String(item.workflow_revision_id === chosen),
        );
        button.append(
          el("strong", "", label),
          el("span", "", date(item.created_at)),
          el(
            "span",
            "",
            item.node_count + " components" + (active ? " · Active" : ""),
          ),
        );
        button.addEventListener(
          "click",
          () => (location.hash = "#harness/" + item.workflow_revision_id),
        );
        history.append(button);
      });
      picker.disabled = false;
      picker.value = chosen;
      currentWorkflowId = chosen;
      const compare = $("[data-compare-revision]"),
        previous = compare.value;
      compare.replaceChildren(new Option("No comparison", ""));
      harnessWorkflows
        .filter((item) => item.workflow_revision_id !== chosen)
        .forEach((item) =>
          compare.append(
            new Option(
              item.source_commit?.slice(0, 12) ||
                item.workflow_revision_id.slice(0, 12),
              item.workflow_revision_id,
            ),
          ),
        );
      compare.disabled = compare.options.length === 1;
      if ([...compare.options].some((option) => option.value === previous))
        compare.value = previous;
      const workflow =
        linkedWorkflow ||
        (await request("/api/harness-workflows/" + encodeURIComponent(chosen)));
      if (sequence !== harnessSequence) return;
      if (workflow.task_family && workflow.task_family !== family) {
        $("[data-harness-family]").value = workflow.task_family;
        return loadHarness(chosen);
      }
      set(
        "[data-harness-workflow-version]",
        workflow.source_commit?.slice(0, 18) || chosen,
      );
      renderWorkflow(
        workflow.graph,
        $("[data-harness-workflow]"),
        $("[data-harness-workflow-list]"),
        $("[data-harness-inspector]"),
      );
      loadWorkflowComparison();
    } catch (error) {
      if (sequence !== harnessSequence) return;
      loaded(history);
      currentWorkflowId = null;
      $("[data-compare-revision]").disabled = true;
      history.replaceChildren();
      picker.replaceChildren(new Option("Revisions unavailable", ""));
      picker.disabled = true;
      set("[data-harness-workflow-version]", "—");
      renderWorkflow(
        null,
        $("[data-harness-workflow]"),
        $("[data-harness-workflow-list]"),
        $("[data-harness-inspector]"),
      );
      stateMessage(
        $("[data-harness-error]"),
        "Could not load harness",
        error.message,
        () => loadHarness(revisionId),
      );
    }
  }
  function renderWorkflowDiff(root, data) {
    root.replaceChildren(el("h2", "", "Workflow changes"));
    const list = el("ul");
    ["added", "removed", "changed"].forEach((kind) =>
      (data.nodes?.[kind] || []).forEach((node) =>
        list.append(
          el(
            "li",
            "",
            kind.charAt(0).toUpperCase() +
              kind.slice(1) +
              ": " +
              (node.label || node.after?.label || node.id),
          ),
        ),
      ),
    );
    const added = data.edges?.added?.length || 0,
      removed = data.edges?.removed?.length || 0;
    if (added || removed)
      list.append(
        el("li", "", `${added} connections added · ${removed} removed`),
      );
    if (list.children.length) root.append(list);
    else
      root.append(
        el("p", "muted", "No structural changes between these revisions."),
      );
    root.append(disclosure("Full structural comparison", data));
    root.hidden = false;
  }
  async function loadWorkflowComparison() {
    const base = $("[data-compare-revision]").value,
      revision = currentWorkflowId,
      root = $("[data-workflow-diff]"),
      sequence = ++comparisonSequence;
    if (!base || !revision) {
      root.hidden = true;
      return;
    }
    root.hidden = false;
    loading(root, "Comparing revisions");
    try {
      const data = await request(
        "/api/harness-workflows/" +
          encodeURIComponent(revision) +
          "/diff?base=" +
          encodeURIComponent(base),
      );
      if (
        sequence !== comparisonSequence ||
        currentWorkflowId !== revision ||
        !location.hash.startsWith("#harness")
      )
        return;
      loaded(root);
      renderWorkflowDiff(root, data);
    } catch (error) {
      if (
        sequence !== comparisonSequence ||
        !location.hash.startsWith("#harness")
      )
        return;
      loaded(root);
      stateMessage(
        root,
        "Could not compare revisions",
        error.message,
        loadWorkflowComparison,
      );
    }
  }
  function showPage(page) {
    if (shownPage === "ask") {
      chatScrollTop = $("[data-chat-scroll]").scrollTop;
      saveChat();
    }
    const [raw, id] = String(page || "").split("/");
    const selected = [
      "ask",
      "runs",
      "run-details",
      "evaluations",
      "versions",
      "evolve",
      "harness",
    ].includes(raw)
      ? raw
      : "ask";
    shownPage = selected;
    $("#ask").hidden = selected !== "ask";
    if (selected === "ask") {
      const position = chatScrollTop;
      requestAnimationFrame(() => {
        if (!$("#ask").hidden) $("[data-chat-scroll]").scrollTop = position;
      });
    }
    $(".workspace").classList.toggle("chat-page", selected === "ask");
    syncContext(selected);
    $("#run-details").hidden = selected !== "run-details" || !selectedRun;
    $("#runs").hidden = selected !== "runs";
    $("#evaluations").hidden = selected !== "evaluations";
    $("#versions").hidden = selected !== "versions";
    $("#evolve").hidden = selected !== "evolve";
    $("#harness").hidden = selected !== "harness";
    $("[data-workflow-diff]").hidden =
      selected !== "harness" || !$("[data-compare-revision]").value;
    document.querySelectorAll("[data-nav]").forEach((link) => {
      const active =
        link.dataset.nav === (selected === "run-details" ? "runs" : selected);
      link.classList.toggle("selected", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    if (selected === "evaluations") loadEvaluations();
    if (selected === "versions") loadVersions();
    if (selected === "evolve") loadEvolve(id);
    if (selected === "harness") loadHarness(id);
    if (selected !== "evolve") {
      clearTimeout(evolutionTimer);
      evolutionSequence++;
    }
    if (selected !== "harness") {
      harnessSequence++;
      comparisonSequence++;
    }
    const sequence = ++routeSequence;
    if (
      selected === "run-details" &&
      id &&
      selectedRun?.run_id !== decodeURIComponent(id)
    ) {
      $("#run-details").hidden = false;
      loading($("[data-run-message]"), "Loading run");
      set("[data-run-question]", "Loading run…");
      set("#run-details [data-outcome]", "Loading");
      $("#run-details [data-outcome]").className = "outcome-badge";
      $(".run-facts").hidden = true;
      $(".run-columns").hidden = true;
      request("/api/runs/" + id)
        .then((run) => {
          if (sequence === routeSequence) {
            $(".run-columns").hidden = false;
            renderRun(run);
          }
        })
        .catch((error) => {
          if (sequence === routeSequence)
            stateMessage(
              $("[data-run-message]"),
              "Could not load run",
              error.message,
              () => showPage(page),
            );
        });
    }
    if (selected === "run-details" && !id && !selectedRun) {
      $("#run-details").hidden = false;
      set("[data-run-question]", "Select a run");
      stateMessage(
        $("[data-run-message]"),
        "No run selected",
        "Open a recorded analysis from Runs.",
        () => (location.hash = "#runs"),
        "empty",
      );
      $(".run-columns").hidden = true;
    }
    window.scrollTo({ top: 0, behavior: "instant" });
  }
  function recordPager(root, total, page, update) {
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE)),
      pager = el("div", "pager");
    const prev = el("button", "", "Previous"),
      next = el("button", "", "Next");
    prev.type = next.type = "button";
    prev.disabled = page === 0;
    next.disabled = page >= pages - 1;
    prev.addEventListener("click", () => update(page - 1));
    next.addEventListener("click", () => update(page + 1));
    pager.append(
      prev,
      el("span", "", `${page + 1} of ${pages} · ${total} recorded (latest 50)`),
      next,
    );
    root.append(pager);
  }
  function recordTable(root, headings) {
    root.replaceChildren();
    const wrap = el("div", "table-wrap"),
      table = el("table", "record-table"),
      head = el("thead"),
      row = el("tr"),
      body = el("tbody");
    headings.forEach((label) => {
      const th = el("th", "", label);
      th.scope = "col";
      row.append(th);
    });
    head.append(row);
    table.append(head, body);
    wrap.append(table);
    root.append(wrap);
    return body;
  }
  function renderEvaluations() {
    const root = $("[data-evaluations-list]"),
      filter = $("[data-evaluation-filter]").value;
    const items = evaluations.filter(
      (item) =>
        !filter ||
        (filter === "passed" ? item.passed === true : item.passed === false),
    );
    if (!items.length) {
      stateMessage(
        root,
        "No evaluations",
        filter
          ? "No recorded evaluations match this filter."
          : "Protected evaluation results will appear here when recorded.",
        null,
        "empty",
      );
      return;
    }
    const body = recordTable(root, [
      "Case",
      "Result",
      "Role",
      "Resources",
      "Recorded",
    ]);
    evaluationPage = Math.min(
      evaluationPage,
      Math.ceil(items.length / PAGE_SIZE) - 1,
    );
    items
      .slice(evaluationPage * PAGE_SIZE, (evaluationPage + 1) * PAGE_SIZE)
      .forEach((item) => {
        const row = el("tr"),
          cell = el("td"),
          details = el("details", "record-detail");
        details.append(
          el("summary", "", item.case_id || item.evaluation_id || "Evaluation"),
        );
        if (item.violation) details.append(el("p", "", item.violation));
        if (item.trace_id)
          details.append(el("p", "mono", "Trace: " + item.trace_id));
        if (item.run_id) {
          const link = el("a", "text-button", "Open run →");
          link.href = "#run-details/" + encodeURIComponent(item.run_id);
          details.append(link);
        }
        cell.append(details);
        const result = el("td"),
          passed = item.passed === true;
        result.append(
          el(
            "span",
            "status-badge " +
              (passed ? "pass" : item.passed === false ? "fail" : ""),
            passed ? "Passed" : item.passed === false ? "Failed" : "Pending",
          ),
        );
        row.append(
          cell,
          result,
          el("td", "", item.role || "Protected evaluation"),
          el("td", "numeric", formatResource(item.resources)),
          el("td", "", date(item.created_at)),
        );
        body.append(row);
      });
    recordPager(root, items.length, evaluationPage, (page) => {
      evaluationPage = page;
      renderEvaluations();
    });
  }
  async function loadEvaluations() {
    const root = $("[data-evaluations-list]");
    loading(root, "Loading evaluations");
    try {
      evaluations =
        (await request("/api/evaluations?limit=50")).evaluations || [];
      loaded(root);
      renderEvaluations();
    } catch (error) {
      loaded(root);
      stateMessage(
        root,
        "Could not load evaluations",
        error.message,
        loadEvaluations,
      );
    }
  }
  function renderVersions() {
    const root = $("[data-versions-list]"),
      filter = $("[data-version-filter]").value;
    const items = versions.filter(
      (item) => !filter || item.commit === versionData?.active_commit,
    );
    if (!items.length) {
      stateMessage(
        root,
        "No versions",
        filter
          ? "No retained version matches this filter."
          : "Candidate versions will appear here when recorded. The base harness remains available.",
        null,
        "empty",
      );
      return;
    }
    const body = recordTable(root, ["Commit", "Status", "Parent", "Created"]);
    versionPage = Math.min(
      versionPage,
      Math.ceil(items.length / PAGE_SIZE) - 1,
    );
    items
      .slice(versionPage * PAGE_SIZE, (versionPage + 1) * PAGE_SIZE)
      .forEach((item) => {
        const row = el("tr"),
          status = el("td"),
          active = item.commit === versionData.active_commit;
        status.append(
          el(
            "span",
            "status-badge " + (active ? "pass" : ""),
            active ? "Active" : item.status || "Recorded",
          ),
        );
        row.append(
          el("td", "mono", item.commit || item.version_id),
          status,
          el("td", "mono", item.parent_commit?.slice(0, 12) || "Base"),
          el("td", "", date(item.created_at)),
        );
        body.append(row);
      });
    recordPager(root, items.length, versionPage, (page) => {
      versionPage = page;
      renderVersions();
    });
  }
  async function loadVersions() {
    const root = $("[data-versions-list]"),
      family = $("[data-version-family]").value;
    syncContext("versions");
    loading(root, "Loading versions");
    try {
      const data = await request(
        "/api/versions?limit=50&task_family=" + encodeURIComponent(family),
      );
      if (family !== $("[data-version-family]").value) return;
      versionData = data;
      versions = data.versions || [];
      loaded(root);
      set(
        "[data-version-description]",
        data.active_commit
          ? "Active commit: " + data.active_commit
          : "Active base version: " + data.base_version,
      );
      renderVersions();
    } catch (error) {
      if (family !== $("[data-version-family]").value) return;
      loaded(root);
      stateMessage(
        root,
        "Could not load versions",
        error.message,
        loadVersions,
      );
    }
  }
  document
    .querySelectorAll("[data-tab]")
    .forEach((button) =>
      button.addEventListener("click", () => showTab(button.dataset.tab)),
    );
  window.addEventListener("hashchange", () => showPage(location.hash.slice(1)));
  function runMessage(run) {
    return (
      run.message ||
      (run.outcome === "unsupported"
        ? run.limitation_reason ||
          "The analyst cannot answer this question with its current capabilities."
        : run.outcome === "answered" && run.answer != null
          ? JSON.stringify(run.answer)
          : run.error || "No answer was stored.")
    );
  }
  function renderRun(run) {
    selectedRun = run;
    closeSuggestions();
    $("#run-details").hidden = false;
    $(".run-columns").hidden = false;
    $(".run-facts").hidden = false;
    $("#run-details").className = "run-view " + (run.outcome || "");
    set("[data-run-question]", run.question || "Run " + run.run_id);
    set("[data-run-short-id]", run.run_id ? run.run_id.slice(0, 10) : "Run");
    const badge = $("#run-details [data-outcome]");
    badge.className = "outcome-badge " + run.outcome;
    badge.textContent = outcomeName(run.outcome);
    set("[data-run-message]", runMessage(run));
    loaded($("[data-run-message]"));
    set("[data-run-id]", run.run_id ? run.run_id.slice(0, 8) : "—");
    $("[data-run-id]").title = run.run_id || "";
    set(
      "[data-version]",
      run.version && run.version.length > 22
        ? run.version.slice(0, 12) + "…"
        : run.version || "—",
    );
    $("[data-version]").title = run.version || "";
    set("[data-run-started]", date(run.created_at));
    set(
      "[data-duration]",
      run.resources && run.resources.elapsed_seconds !== undefined
        ? run.resources.elapsed_seconds + " s"
        : "—",
    );
    const resources = run.resources || {},
      toolCount = resources.tool_calls ?? 0;
    set(
      "[data-resource-summary]",
      (resources.model_calls ?? 0) +
        " model · " +
        toolCount +
        " " +
        (toolCount === 1 ? "tool" : "tools") +
        " · " +
        (resources.table_pages ?? 0) +
        " " +
        ((resources.table_pages ?? 0) === 1 ? "page" : "pages") +
        " · " +
        (resources.total_tokens ?? 0) +
        " tokens",
    );
    renderSQLite(run);
    renderTools(run);
    renderTrace(run);
    renderGap(run);
    renderTimeline(run);
    showTab("sqlite");
    $("[data-sqlite-content]").append(
      disclosure("SQLite run record", {
        run_id: run.run_id,
        question: run.question,
        outcome: run.outcome,
        limitation_kind: run.limitation_kind || null,
        dataset: run.dataset && run.dataset.id,
        sqlite_rows_read:
          (run.evidence &&
            run.evidence.sqlite &&
            run.evidence.sqlite.row_count) ||
          0,
        tool_calls: (run.resources && run.resources.tool_calls) || 0,
        trace_id: (run.trace && run.trace.id) || null,
        version: run.version,
      }),
    );
    const check = $("#candidate-check");
    if (run.outcome === "unsupported") $(".run-secondary").prepend(check);
    else $(".evidence-grid").append(check);

    announce(outcomeName(run.outcome) + " run loaded");
    showPage("run-details");
  }
  function historyRow(run) {
    const row = el("tr"),
      questionCell = el("td"),
      link = el("a", "row-link", run.question || run.run_id);
    link.href = "#run-details/" + encodeURIComponent(run.run_id);
    questionCell.append(link);
    const outcome = el("td", "table-status");
    outcome.append(
      el(
        "span",
        "outcome-badge " + (run.outcome || ""),
        outcomeName(run.outcome),
      ),
    );
    const version = el("td", "mono", run.version?.slice(0, 12) || "—");
    version.title = run.version || "";
    row.append(
      questionCell,
      outcome,
      version,
      el(
        "td",
        "numeric",
        run.resources?.elapsed_seconds !== undefined
          ? run.resources.elapsed_seconds + " s"
          : "—",
      ),
      el("td", "", date(run.created_at)),
    );
    return row;
  }
  function renderHistory() {
    const list = $("[data-history-list]"),
      pager = $("[data-history-pager]"),
      query = $("[data-run-search]").value.toLowerCase(),
      outcome = $("[data-run-filter]").value;
    const filtered = runs.filter(
      (run) =>
        (!outcome || run.outcome === outcome) &&
        ((run.question || "") + " " + run.run_id).toLowerCase().includes(query),
    );
    const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
    historyPage = Math.min(historyPage, pages - 1);
    list.replaceChildren();
    pager.replaceChildren();
    set(
      "[data-history-count]",
      filtered.length +
        " recorded" +
        (runs.length === 100 ? " · latest 100" : ""),
    );
    if (!filtered.length) {
      const row = el("tr"),
        cell = el("td");
      cell.colSpan = 5;
      cell.append(
        el(
          "p",
          "empty-state",
          query || outcome
            ? "No runs match these filters."
            : "No completed runs yet. Run an analysis to get started.",
        ),
      );
      row.append(cell);
      list.append(row);
      return;
    }
    filtered
      .slice(historyPage * PAGE_SIZE, (historyPage + 1) * PAGE_SIZE)
      .forEach((run) => list.append(historyRow(run)));
    const prev = el("button", "", "Previous"),
      next = el("button", "", "Next");
    prev.type = next.type = "button";
    prev.disabled = historyPage === 0;
    next.disabled = historyPage === pages - 1;
    prev.addEventListener("click", () => {
      historyPage--;
      renderHistory();
    });
    next.addEventListener("click", () => {
      historyPage++;
      renderHistory();
    });
    pager.append(prev, el("span", "", historyPage + 1 + " of " + pages), next);
  }
  async function loadHistory() {
    const list = $("[data-history-list]");
    list.replaceChildren();
    const row = el("tr"),
      cell = el("td");
    cell.colSpan = 5;
    loading(cell, "Loading runs");
    row.append(cell);
    list.append(row);
    try {
      runs = (await request("/api/runs?limit=100")).runs || [];
      renderHistory();
    } catch (error) {
      const errorRow = el("tr"),
        errorCell = el("td");
      errorCell.colSpan = 5;
      stateMessage(
        errorCell,
        "Could not load runs",
        error.message,
        loadHistory,
      );
      errorRow.append(errorCell);
      list.replaceChildren(errorRow);
      $("[data-history-pager]").replaceChildren();
      set("[data-history-count]", "Unavailable");
    }
  }
  function syncChatLayout() {
    const hasMessages = Boolean(chatMessages.children.length);
    $("#ask").classList.toggle("has-messages", hasMessages);
    $(".ask-heading").hidden = hasMessages;
    $("[data-examples]").hidden = hasMessages;
    chatMessages.hidden = !hasMessages;
  }
  function chatNearBottom() {
    const scroll = $("[data-chat-scroll]");
    return scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100;
  }
  function scrollChat() {
    const scroll = $("[data-chat-scroll]");
    scroll.scrollTo({ top: scroll.scrollHeight, behavior: "instant" });
  }
  function renderChatEntry(reply) {
    const { entry, message, status, content } = reply;
    content.textContent = entry.text;
    status.textContent = entry.status || "";
    if (entry.outcome) message.dataset.chatOutcome = entry.outcome;
    if (entry.error) message.dataset.state = "error";
    if (entry.pending) message.setAttribute("aria-busy", "true");
    else message.removeAttribute("aria-busy");
    message.querySelector(".case-links")?.remove();
    if (entry.runId) {
      const links = el("div", "case-links"),
        runLink = el("a", "text-button", "View run ↗");
      runLink.href = "#run-details/" + encodeURIComponent(entry.runId);
      links.append(runLink);
      if (entry.evolutionId) {
        const evolutionLink = el("a", "text-button", "View evolution ↗");
        evolutionLink.href = "#evolve/" + encodeURIComponent(entry.evolutionId);
        links.append(evolutionLink);
      }
      message.append(links);
    }
  }
  function appendChatMessage(role, text, source, details = {}) {
    const entry = {
      role, text,
      source: source ? { id: source.id, input_kind: source.input_kind } : null,
      ...details,
    };
    const message = el("article", "chat-message"),
      heading = el("div", "sr-only"),
      status = el("div", "chat-status"),
      content = el("p", "", text);
    message.dataset.chatRole = role;
    message.setAttribute("aria-atomic", "true");
    heading.append(el("strong", "", role === "user" ? "You" : "Analyst"));
    if (source)
      heading.append(
        el(
          "span",
          "",
          (source.input_kind === "logistics_bundle" ? "Logistics" : "Inventory") +
            " · " + source.id,
        ),
      );
    message.append(heading, status, content);
    const reply = { entry, message, status, content };
    renderChatEntry(reply);
    chatEntries.push(entry);
    chatMessages.append(message);
    syncChatLayout();
    $("[data-new-analysis]").hidden = false;
    return reply;
  }
  function restoreChat() {
    try {
      const saved = JSON.parse(localStorage.getItem(CHAT_STORAGE_KEY));
      const validSource = (source) => source === null || (
        source && typeof source.id === "string" &&
        ["inventory_table", "logistics_bundle"].includes(source.input_kind)
      );
      if (saved?.version !== 1 || !Array.isArray(saved.messages) ||
          typeof saved.draft !== "string" || saved.draft.length > 2000 ||
          !validSource(saved.source) || !saved.messages.every((entry) =>
            entry && ["user", "assistant"].includes(entry.role) &&
            typeof entry.text === "string" && validSource(entry.source) &&
            ["status", "outcome", "runId", "evolutionId"].every((key) =>
              entry[key] == null || typeof entry[key] === "string") &&
            ["pending", "error"].every((key) =>
              entry[key] === undefined || typeof entry[key] === "boolean"))) return;
      question.value = saved.draft;
      chatSource = saved.source;
      chatScrollTop = Number.isFinite(saved.scrollTop) ? Math.max(0, saved.scrollTop) : 0;
      for (const entry of saved.messages) {
        const { role, source, status, outcome, runId, evolutionId, error, pending } = entry;
        appendChatMessage(role, pending
          ? "This request was interrupted by a page reload. Check Runs for a completed result before sending it again."
          : entry.text, source, {
          status: pending ? "Request interrupted" : status,
          outcome, runId, evolutionId,
          error: pending || error,
          pending: false,
        });
      }
      saveDraft();
    } catch {}
  }
  $("#analysis-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    closeSuggestions();
    if (runButton.disabled) return;
    const prompt = question.value.trim(),
      source = selectedSource;
    if (!prompt) {
      question.setCustomValidity("Enter a question for the analyst.");
      question.reportValidity();
      return;
    }
    appendChatMessage("user", prompt, source);
    const reply = appendChatMessage("assistant", "Running analysis…", null, { pending: true });
    const sequence = ++chatSequence;
    question.value = "";
    saveDraft();
    autosize();
    submitting = true;
    const original = runButton.innerHTML;
    updateRunButton();
    runButton.setAttribute("aria-busy", "true");
    runButton.innerHTML = '<span class="send-progress" aria-hidden="true"></span>';
    runButton.setAttribute("aria-label", "Analysis in progress");
    announce("Analysis in progress");
    question.focus({ preventScroll: true });
    closeSuggestions();
    scrollChat();
    try {
      const run = await request("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: prompt,
          input_kind: source.input_kind,
          dataset_id: source.id,
        }),
      });
      if (sequence !== chatSequence) return;
      const followReply = chatNearBottom();
      reply.entry.text = runMessage(run);
      reply.entry.outcome = run.outcome;
      reply.entry.pending = false;
      const seconds = run.resources?.elapsed_seconds;
      reply.entry.status = seconds == null
        ? outcomeName(run.outcome)
        : outcomeName(run.outcome) + " · " + seconds + " s";
      reply.entry.runId = run.run_id;
      reply.entry.evolutionId = run.evolution_job_id;
      renderChatEntry(reply);
      if (followReply) scrollChat();
      announce("Analyst replied");
      loadHistory();
    } catch (error) {
      if (sequence !== chatSequence) return;
      const followReply = chatNearBottom();
      reply.entry.error = true;
      reply.entry.pending = false;
      reply.entry.status = "Analysis failed";
      reply.entry.text = "Analysis could not complete. " + error.message;
      renderChatEntry(reply);
      announce("Analysis could not complete");
      if (!question.value) {
        question.value = prompt;
        saveDraft();
        autosize();
      }
      if (followReply) scrollChat();
    } finally {
      if (sequence !== chatSequence) return;
      saveChat();
      submitting = false;
      updateRunButton();
      runButton.removeAttribute("aria-busy");
      runButton.innerHTML = original;
      runButton.setAttribute("aria-label", "Send question");
    }
  });
  $("[data-copy-run-id]").addEventListener("click", async () => {
    if (!selectedRun) return;
    try {
      await navigator.clipboard.writeText(selectedRun.run_id);
      announce("Run ID copied");
    } catch {
      announce("Could not copy run ID");
    }
  });
  $("[data-harness-family]").addEventListener("change", () => loadHarness());
  async function loadHealth() {
    const connection = $("[data-connection]");
    connection.dataset.state = "loading";
    set("[data-sqlite-status]", "Checking SQLite");
    try {
      const data = await request("/api/health");
      healthData = data;
      serviceReady =
        data.sqlite === "connected" && Boolean(data.logistics_active_version);
      $("[data-retry-connection]").hidden = serviceReady;
      connection.dataset.state = serviceReady
        ? "ready"
        : data.sqlite === "connected"
          ? "warning"
          : "error";
      set(
        "[data-sqlite-status]",
        serviceReady
          ? "SQLite connected"
          : data.sqlite === "connected"
            ? "Restart required"
            : "SQLite unavailable",
      );
      syncContext(location.hash.slice(1).split("/")[0] || "ask");
      if (selectedSource) setSource(selectedSource, false);
      updateRunButton();
    } catch (error) {
      serviceReady = false;
      healthData = null;
      connection.dataset.state = "error";
      set("[data-sqlite-status]", "Server unavailable");
      set("[data-active-version]", "Unavailable");
      $("[data-retry-connection]").hidden = false;
      set(
        "[data-source-note]",
        "Connect to the local server before running an analysis.",
      );
      updateRunButton();
    }
  }
  $("[data-retry-connection]").addEventListener("click", () => {
    loadHealth();
    loadDatasets();
    loadHistory();
  });
  $("[data-compare-revision]").addEventListener(
    "change",
    loadWorkflowComparison,
  );
  $("[data-new-analysis]").addEventListener("click", () => {
    chatSequence++;
    clearTimeout(chatSaveTimer);
    submitting = false;
    runButton.innerHTML = sendButtonContent;
    runButton.removeAttribute("aria-busy");
    runButton.setAttribute("aria-label", "Send question");
    updateRunButton();
    closeSuggestions();
    chatMessages.replaceChildren();
    chatEntries.length = 0;
    chatScrollTop = 0;
    $("[data-chat-scroll]").scrollTop = 0;
    syncChatLayout();
    question.value = "";
    question.setCustomValidity("");
    saveDraft();
    autosize();
    location.hash = "#ask";
    announce("Chat reset");
    setTimeout(() => question.focus(), 0);
  });
  $("[data-run-search]").addEventListener("input", () => {
    historyPage = 0;
    renderHistory();
  });
  $("[data-run-filter]").addEventListener("change", () => {
    historyPage = 0;
    renderHistory();
  });
  $("[data-refresh-runs]").addEventListener("click", () => {
    loadHealth();
    loadDatasets();
    loadHistory();
  });
  $("[data-evaluation-filter]").addEventListener("change", () => {
    evaluationPage = 0;
    renderEvaluations();
  });
  $("[data-version-filter]").addEventListener("change", () => {
    versionPage = 0;
    renderVersions();
  });
  $("[data-version-family]").addEventListener("change", () => {
    versionPage = 0;
    loadVersions();
  });
  $("[data-refresh-evaluations]").addEventListener("click", loadEvaluations);
  $("[data-refresh-versions]").addEventListener("click", loadVersions);
  $("[data-revision-picker]").addEventListener(
    "change",
    (event) => (location.hash = "#harness/" + event.target.value),
  );
  function showEvolutionTab(name) {
    document.querySelectorAll("[data-evolution-tab]").forEach((button) => {
      const selected = button.dataset.evolutionTab === name;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    document
      .querySelectorAll("[data-evolution-panel]")
      .forEach(
        (panel) => (panel.hidden = panel.dataset.evolutionPanel !== name),
      );
  }
  document
    .querySelectorAll("[data-evolution-tab]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        showEvolutionTab(button.dataset.evolutionTab),
      ),
    );
  // Roving keyboard focus for both tab groups.
  document.querySelectorAll('[role="tablist"]').forEach((list) =>
    list.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
        return;
      event.preventDefault();
      const buttons = [...list.querySelectorAll('[role="tab"]')],
        current = buttons.indexOf(document.activeElement);
      const index =
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? buttons.length - 1
            : (current +
                (event.key === "ArrowRight" ? 1 : -1) +
                buttons.length) %
              buttons.length;
      buttons[index].click();
      buttons[index].focus();
    }),
  );
  $("[data-chat-scroll]").addEventListener("scroll", () => {
    if ($("#ask").hidden) return;
    chatScrollTop = $("[data-chat-scroll]").scrollTop;
    clearTimeout(chatSaveTimer);
    chatSaveTimer = setTimeout(saveChat, 150);
  });
  window.addEventListener("pagehide", saveChat);
  restoreChat();
  loadHealth();
  loadDatasets().then(() => {
    if (location.hash === "#versions") loadVersions();
  });
  loadHistory();
  showPage(location.hash.slice(1) || "ask");
})();
