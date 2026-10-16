// Records the ~100 second SEAL demo video with Playwright.
//
//   node scripts/demo/record_demo.cjs [output.mp4]
//
// Starts the credential-free demo console (scripts/demo/demo_server.py),
// drives the real operator UI through ask -> capability gap -> evolution ->
// similar question, and writes an MP4 (needs ffmpeg) next to the raw WebM.
// Run from a clean commit: evolution refuses to replay a dirty source tree.

const { spawn, execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "..", "..");
const PORT = Number(process.env.SEAL_DEMO_PORT || 4191);
const BASE = `http://127.0.0.1:${PORT}`;
const OUTPUT = path.resolve(process.argv[2] || path.join(ROOT, "artifacts", "seal-demo.mp4"));
const VIEWPORT = { width: 1440, height: 990 };

const QUESTIONS = {
  answerable: "How many units of SKU-00042 are available?",
  gap: "What are the available units in each warehouse?",
  similar: "What are the on-hand units in each category?",
};

const STAGE_PHASES = [
  [["incident_classified", "workflow_snapshot_ready", "diagnosing", "trace_read"],
    "Diagnose: read the failed run's trace and classify the incident",
    "A capability limit, not a provider or data error, so a harness change may fix it"],
  [["cases_frozen", "baseline_reproduced"],
    "Reproduce: freeze the original case plus a fresh generated one, and re-fail both on the baseline",
    "The protected oracle grades every case; the proposer never sees the answers"],
  [["proposal_received", "candidate_ready", "proposal_rejected"],
    "Propose: the proposer writes a reusable harness change as a pinned Git commit",
    "The diff is screened: only harness/ files, no answers, budgets, or oracle edits"],
  [["evaluating", "selection_decided"],
    "Evaluate: baseline and candidate run the same frozen cases in isolated runtimes",
    "Original failure, generated reproduction, regressions, private variations, and a refusal"],
  [["activated", "rerun_completed"],
    "Activated: every gate passed, so the tested commit became the live harness",
    "SEAL then re-ran the original question on it automatically"],
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForServer() {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(BASE + "/api/health");
      if (response.ok) return;
    } catch {}
    await sleep(500);
  }
  throw new Error("Demo console did not start");
}

// Captions and a visible cursor, drawn inside the page so the recording shows them.
function overlayScript() {
  const style = document.createElement("style");
  style.textContent = `
    body { padding-bottom: 92px; }
    .workspace.chat-page { height: calc(100dvh - 92px) !important; }
    .toast { bottom: 108px !important; }
    #demo-caption { position: fixed; left: 0; right: 0; bottom: 0; height: 92px; z-index: 99999;
      pointer-events: none; display: flex; flex-direction: column; justify-content: center; align-items: center;
      background: #111; color: #fff; font: 500 21px/1.3 Geist, system-ui, sans-serif; text-align: center; }
    #demo-caption span { transition: opacity .3s ease; }
    #demo-caption small { display: block; margin-top: 5px; font-size: 15px; font-weight: 400; color: #bdbdbd; }
    #demo-card { position: fixed; inset: 0; z-index: 100000; display: flex; flex-direction: column;
      align-items: center; justify-content: center; gap: 14px; background: #0f0f10; color: #fff;
      font: 600 54px/1.1 Geist, system-ui, sans-serif; opacity: 0; transition: opacity .5s ease; pointer-events: none; }
    #demo-card p { margin: 0; font-size: 22px; font-weight: 400; color: #bdbdbd; max-width: 900px; text-align: center; line-height: 1.45; }
    #demo-cursor { position: fixed; z-index: 99998; width: 18px; height: 18px; margin: -9px 0 0 -9px;
      border-radius: 50%; background: rgba(20,20,20,.55); border: 2px solid #fff; pointer-events: none;
      box-shadow: 0 0 0 1px rgba(0,0,0,.3); transition: transform .12s ease; left: -40px; top: -40px; }
    #demo-cursor.down { transform: scale(.7); }`;
  const mount = () => {
    if (document.getElementById("demo-caption")) return;
    document.head.append(style);
    const caption = Object.assign(document.createElement("div"), { id: "demo-caption" });
    const card = Object.assign(document.createElement("div"), { id: "demo-card" });
    const cursor = Object.assign(document.createElement("div"), { id: "demo-cursor" });
    document.body.append(caption, card, cursor);
    addEventListener("mousemove", (event) => {
      cursor.style.left = event.clientX + "px";
      cursor.style.top = event.clientY + "px";
    }, true);
    addEventListener("mousedown", () => cursor.classList.add("down"), true);
    addEventListener("mouseup", () => cursor.classList.remove("down"), true);
  };
  if (document.body) mount();
  else addEventListener("DOMContentLoaded", mount);
}

async function main() {
  fs.mkdirSync(path.dirname(OUTPUT), { recursive: true });
  const server = spawn(
    "uv",
    ["run", "--frozen", "python", "scripts/demo/demo_server.py", "--port", String(PORT),
      "--agent-latency", "0.6", "--proposer-latency", "4"],
    { cwd: ROOT, stdio: ["ignore", "inherit", "inherit"] },
  );
  const videoDir = fs.mkdtempSync(path.join(path.dirname(OUTPUT), ".video-"));
  let browser;
  try {
    await waitForServer();
    browser = await chromium.launch();
    const context = await browser.newContext({
      viewport: VIEWPORT,
      deviceScaleFactor: 1,
      recordVideo: { dir: videoDir, size: VIEWPORT },
    });
    await context.addInitScript(overlayScript);
    const page = await context.newPage();
    const started = Date.now();
    const mark = (label) => console.log(`[${((Date.now() - started) / 1000).toFixed(1)}s] ${label}`);

    const caption = (text, detail = "") =>
      page.evaluate(([text, detail]) => {
        const node = document.getElementById("demo-caption");
        node.innerHTML = text ? `<span>${text}${detail ? `<small>${detail}</small>` : ""}</span>` : "";
      }, [text, detail]);
    const card = (title, body) =>
      page.evaluate(([title, body]) => {
        const node = document.getElementById("demo-card");
        node.innerHTML = title ? `<div>${title}</div><p>${body}</p>` : "";
        node.style.opacity = title ? "1" : "0";
      }, [title, body]);
    const pointAt = async (locator, { click = true } = {}) => {
      await locator.scrollIntoViewIfNeeded();
      const box = await locator.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 25 });
      await sleep(250);
      if (click) await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    };
    const ask = async (question) => {
      const input = page.locator("#question");
      await pointAt(input);
      await input.pressSequentially(question, { delay: 38 });
      await sleep(500);
      await page.keyboard.press("Enter");
    };
    const lastReply = () => page.locator("article.chat-message").last();
    const waitForReply = async () => {
      await page.waitForFunction(() => {
        const messages = document.querySelectorAll("article.chat-message");
        const last = messages[messages.length - 1];
        return last && !last.hasAttribute("aria-busy") && last.querySelector(".case-links");
      }, null, { timeout: 60_000 });
    };

    await page.goto(BASE + "/#ask");
    await page.waitForSelector("#question");
    await page.waitForTimeout(800);

    // Opening card.
    mark("intro");
    await card("SEAL: an agent that repairs itself",
      "Ask a question. When the agent hits a capability gap, it writes a fix, " +
      "proves it against baseline in isolated runs, and promotes it only if every gate passes.");
    await sleep(5000);
    await card("", "");
    await sleep(600);

    // 1. An answerable question.
    mark("answerable question");
    await caption("1 · Ask the analyst agent about a 512-row inventory table",
      "Every answer is read from SQLite through bounded harness tools");
    await sleep(2500);
    await ask(QUESTIONS.answerable);
    await waitForReply();
    await caption("Answered: the agent filtered to one SKU and read a single page");
    await sleep(4500);

    // 2. A question beyond the current harness.
    mark("capability gap");
    await caption("2 · Now a question the current harness can't handle",
      "A whole-table grouped total needs 128 pages, but the agent may only make 8 model calls");
    await sleep(3000);
    await ask(QUESTIONS.gap);
    await waitForReply();
    await caption("It fails honestly instead of guessing, and SEAL opens an evolution job automatically");
    await sleep(4500);

    // 3. Watch the evolution.
    mark("evolution");
    const evolutionLink = lastReply().getByRole("link", { name: /View evolution/ });
    const jobId = decodeURIComponent((await evolutionLink.getAttribute("href")).split("/").pop());
    await pointAt(evolutionLink);
    await page.waitForSelector("#evolve:not([hidden])");
    await caption("3 · Evolution starts from the recorded incident",
      "Diagnose → reproduce → propose → screen → evaluate → activate");
    // Follow the job's real stages, captioning every phase in order for a few seconds each.
    let shown = -1;
    let shownAt = 0;
    for (;;) {
      const job = await (await fetch(`${BASE}/api/evolution-jobs/${jobId}`)).json();
      const record = job.job || job;
      if (["rejected", "blocked", "operational_error", "needs_contract"].includes(record.status))
        throw new Error("Evolution did not activate: " + (record.reason || record.status));
      const reached = STAGE_PHASES.findIndex(([stages]) => stages.includes(record.stage));
      const dwelled = Date.now() - shownAt > 3200;
      if (reached > shown && dwelled) {
        shown += 1;
        shownAt = Date.now();
        mark("phase " + STAGE_PHASES[shown][0][0]);
        await caption(STAGE_PHASES[shown][1], STAGE_PHASES[shown][2]);
      } else if (record.status === "activated" && shown === STAGE_PHASES.length - 1 && dwelled) {
        break;
      }
      await sleep(300);
    }
    mark("activated");
    await sleep(1500);

    const changesTab = page.locator("#evolution-tab-changes");
    await pointAt(changesTab);
    await caption("The change: a new aggregate_metric tool that scans every page inside one call",
      "Only harness/tools.py and harness/context.py changed. No answers, limits, or oracle edits");
    await sleep(1200);
    await page.mouse.wheel(0, 380);
    await sleep(6500);
    await page.mouse.wheel(0, -380);

    const evaluationsTab = page.locator("#evolution-tab-evaluations");
    await pointAt(evaluationsTab);
    await caption("Paired selection: baseline vs. candidate on the same frozen cases",
      "Original failure, a generated reproduction, regressions, private variations, and a refusal");
    await sleep(1200);
    await page.mouse.wheel(0, 380);
    await sleep(6500);
    await page.mouse.wheel(0, -380);

    // 4. A similar question, now answered.
    mark("similar question");
    await pointAt(page.locator('a.nav-link[data-nav="ask"]'));
    await page.waitForSelector("#ask:not([hidden])");
    await caption("4 · Ask a similar question it couldn't answer before",
      "Different metric and grouping, same whole-table shape");
    await sleep(2500);
    await ask(QUESTIONS.similar);
    await waitForReply();
    await caption("Answered with the evolved harness: one tool call scanned all 128 pages");
    await sleep(5000);
    await pointAt(lastReply().getByRole("link", { name: /View run/ }));
    await page.waitForSelector("#run-details:not([hidden])");
    await caption("The run record shows the new tool call and the exact commit that answered it", "");
    await page.locator("#tab-tools").click().catch(() => {});
    await sleep(6000);

    mark("outro");
    await caption("");
    await card("Fail → evolve → answer",
      "Real SEAL UI, SQLite history, harness, evolution controller, and promotion gates. " +
      "Model, Daytona, and Phoenix calls were local stand-ins for this recording.");
    await sleep(5000);
    mark("done");
    await context.close();
    const [video] = fs.readdirSync(videoDir).filter((name) => name.endsWith(".webm"));
    const webm = OUTPUT.replace(/\.mp4$/, "") + ".webm";
    fs.renameSync(path.join(videoDir, video), webm);
    execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-i", webm, "-c:v", "libx264", "-preset", "slow",
      "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", OUTPUT], { stdio: "inherit" });
    console.log("Wrote " + OUTPUT);
  } finally {
    if (browser) await browser.close();
    server.kill("SIGINT");
    fs.rmSync(videoDir, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
