import { chromium, type BrowserContext, type Page } from "@playwright/test";
import { readFile, writeFile, mkdir } from "node:fs/promises";

type Job = {
  url: string;
  mode?: "task" | "explore";
  task?: string;
  max_steps?: number;
  viewport?: { width: number; height: number };
  storage_state?: string;
  output_dir?: string;
};

type Finding = {
  type: "bug" | "ux-friction" | "accessibility" | "browser-error";
  title: string;
  severity: "low" | "medium" | "high" | "critical";
  confidence: number;
  evidence: string[];
  expected?: string;
  actual?: string;
};

type Action =
  | { action: "click"; selector: string; reason?: string }
  | { action: "fill"; selector: string; value: string; reason?: string }
  | { action: "press"; selector: string; value: string; reason?: string }
  | { action: "goto"; value: string; reason?: string }
  | { action: "done"; reason?: string };

const apiBase = process.env.OPENAI_BASE_URL || (process.env.TOKENROUTER_API_KEY ? "https://api.tokenrouter.com/v1" : "https://api.openai.com/v1");
const model = process.env.ADVERSARY_MODEL || (process.env.TOKENROUTER_API_KEY ? "z-ai/glm-5.3-free" : "gpt-5-mini");

async function ask(system: string, user: string): Promise<string> {
  const key = process.env.OPENAI_API_KEY || process.env.TOKENROUTER_API_KEY;
  if (!key) throw new Error("OPENAI_API_KEY or TOKENROUTER_API_KEY is required");
  const response = await fetch(`${apiBase}/chat/completions`, {
    method: "POST",
    headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
    body: JSON.stringify({ model, temperature: 0.1, response_format: { type: "json_object" }, messages: [
      { role: "system", content: system }, { role: "user", content: user }
    ] })
  });
  if (!response.ok) throw new Error(`model request failed: ${response.status} ${await response.text()}`);
  const data = await response.json() as { choices?: [{ message?: { content?: string } }] };
  return data.choices?.[0]?.message?.content || "{}";
}

async function snapshot(page: Page): Promise<string> {
  return page.locator("body").innerText({ timeout: 3000 }).catch(() => "").then(text => text.slice(0, 12000));
}

async function chooseAction(job: Job, page: Page): Promise<Action> {
  const links = await page.locator("a,button,[role=button],input,textarea,select").evaluateAll(elements => elements.slice(0, 80).map((element, index) => ({
    index, tag: element.tagName, text: (element.textContent || (element as HTMLInputElement).value || "").trim().slice(0, 120),
    aria: element.getAttribute("aria-label"), disabled: (element as HTMLButtonElement).disabled
  })));
  const raw = await ask(
    "You are a cautious black-box UI adversary. Choose exactly one safe browser action. Never delete data, submit payment, send invitations, change account/security settings, or invent selectors. Prefer visible controls. Return JSON only: {action, selector, value?, reason?}. Use action=done when the task is complete or no safe action remains.",
    JSON.stringify({ url: page.url(), task: job.task || "Explore the interface for confusing or broken user flows.", visible_text: await snapshot(page), controls: links })
  );
  try { return JSON.parse(raw) as Action; } catch { return { action: "done", reason: "model returned invalid JSON" }; }
}

async function apply(page: Page, action: Action): Promise<void> {
  if (action.action === "done") return;
  if (action.action === "goto") {
    if (!action.value.startsWith(new URL(page.url()).origin)) return;
    await page.goto(action.value, { waitUntil: "domcontentloaded", timeout: 15000 });
    return;
  }
  const target = page.locator(action.selector).first();
  if (action.action === "click") await target.click({ timeout: 5000 });
  if (action.action === "fill") await target.fill(action.value, { timeout: 5000 });
  if (action.action === "press") await target.press(action.value, { timeout: 5000 });
  await page.waitForTimeout(500);
}

export async function runJob(job: Job): Promise<object> {
  if (!job.url || !/^https?:\/\//.test(job.url)) throw new Error("job.url must be an http(s) URL");
  const output = job.output_dir || process.env.OUTPUT_DIR || "/reports";
  await mkdir(output, { recursive: true });
  const findings: Finding[] = [];
  const consoleErrors: string[] = [];
  const browser = await chromium.launch({ headless: true });
  const context: BrowserContext = await browser.newContext({
    viewport: job.viewport || { width: 1280, height: 800 },
    storageState: job.storage_state
  });
  const page = await context.newPage();
  page.on("console", message => { if (message.type() === "error") consoleErrors.push(message.text()); });
  page.on("pageerror", error => consoleErrors.push(error.message));
  page.on("requestfailed", request => consoleErrors.push(`${request.method()} ${request.url()} — ${request.failure()?.errorText || "failed"}`));
  await page.goto(job.url, { waitUntil: "domcontentloaded", timeout: 30000 });
  const maxSteps = Math.min(30, Math.max(1, job.max_steps || 10));
  const steps: string[] = [];
  try {
    for (let i = 0; i < maxSteps; i++) {
      const action = await chooseAction(job, page);
      steps.push(`${i + 1}. ${action.action}${"selector" in action ? ` ${action.selector}` : ""}${action.reason ? ` — ${action.reason}` : ""}`);
      if (action.action === "done") break;
      try { await apply(page, action); } catch (error) {
        findings.push({ type: "bug", title: `UI action failed: ${action.action}`, severity: "medium", confidence: 0.8, evidence: [String(error)], actual: String(error) });
      }
      await page.screenshot({ path: `${output}/step-${String(i + 1).padStart(2, "0")}.png`, fullPage: true }).catch(() => undefined);
    }
    if (consoleErrors.length) findings.push({ type: "browser-error", title: "Browser errors occurred during the flow", severity: "medium", confidence: 0.9, evidence: consoleErrors.slice(0, 20) });
    const review = await ask(
      "You are reviewing a black-box UI test. Return JSON only: {findings:[{type,title,severity,confidence,expected,actual,evidence:string[]}]}. Report only concrete, reproducible problems or substantial UX friction. Do not report personal taste. Use an empty array if there is no issue.",
      JSON.stringify({ url: page.url(), mode: job.mode || "task", task: job.task, steps, visible_text: await snapshot(page), console_errors: consoleErrors })
    );
    try { findings.push(...((JSON.parse(review) as { findings?: Finding[] }).findings || [])); } catch { /* report still contains raw evidence */ }
  } finally {
    await page.screenshot({ path: `${output}/final.png`, fullPage: true }).catch(() => undefined);
    await browser.close();
  }
  const report = { url: job.url, mode: job.mode || "task", task: job.task, steps, findings, generated_at: new Date().toISOString() };
  await writeFile(`${output}/report.json`, JSON.stringify(report, null, 2));
  await writeFile(`${output}/report.md`, `# Browser adversary report\n\n- URL: ${job.url}\n- Mode: ${job.mode || "task"}\n- Findings: ${findings.length}\n\n${findings.map((f, i) => `## ${i + 1}. ${f.title}\n\n- Type: ${f.type}\n- Severity: ${f.severity}\n- Confidence: ${f.confidence}\n- Actual: ${f.actual || "See evidence"}\n- Evidence: ${(f.evidence || []).join("; ")}\n`).join("\n")}`);
  return report;
}

const jobPath = process.argv[2];
if (jobPath) readFile(jobPath, "utf8").then(raw => runJob(JSON.parse(raw))).catch(error => { console.error(error); process.exitCode = 1; });
