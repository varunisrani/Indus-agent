// Every desktop page and notable component, in light and dark (compare brief 2026-10-24).
// Built on the repo's E2E harness (apps/desktop/e2e/fixtures.ts) like desktop.ts: the fake backend
// for the onboarding overlay, the real runtime (tests/fakes/web_stack.py + the scripted fake model)
// for everything else. Theme: the app's default "system" mode follows prefers-color-scheme, which
// is switched over DevTools (Emulation.setEmulatedMedia) so open menus/dialogs stay open.
//
//   WT=<worktree> OUT=<png dir> xvfb-run -a -s "-screen 0 1920x1080x24" node desktop-all.ts

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const WT = process.env.WT ?? "";
const OUT = process.env.OUT ?? "";
if (WT === "" || OUT === "") throw new Error("set WT and OUT");
const ONLY = (process.env.ONLY ?? "").split(",").filter((s) => s !== "");
fs.mkdirSync(OUT, { recursive: true });

const fx = await import(path.join(WT, "apps/desktop/e2e/fixtures.ts"));
const { cleanupFakes } = await import(path.join(WT, "apps/desktop/testing/fakeIndus.ts"));
const { sleep, hasButton } = fx;

const IDLE = hasButton("Send");
const W = 1440;
const H = 900;

type App = Awaited<ReturnType<typeof fx.launchApp>>;
type Cdp = App["cdp"];

interface Entry {
  slug: string;
  title: string;
  group: string;
  note: string;
  light: string;
  dark: string;
}
const manifest: Entry[] = [];
const problems: string[] = [];
const MANIFEST = path.join(OUT, "manifest.json");

function saveManifest(missing: string[] = []): void {
  fs.writeFileSync(MANIFEST, `${JSON.stringify({ entries: manifest, missing, problems }, null, 2)}\n`);
}

async function size(cdp: Cdp, w = W, h = H): Promise<void> {
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: w,
    height: h,
    deviceScaleFactor: 1,
    mobile: false,
  });
}

async function theme(cdp: Cdp, mode: "light" | "dark"): Promise<void> {
  await cdp.send("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-color-scheme", value: mode }],
  });
}

const key = (name: string) => ({
  key: name,
  code: name.length === 1 ? `Key${name.toUpperCase()}` : name,
  windowsVirtualKeyCode:
    name === "Escape" ? 27 : name === "Enter" ? 13 : name === "ArrowDown" ? 40 : name.toUpperCase().charCodeAt(0),
});
async function press(cdp: Cdp, name: string, modifiers = 0): Promise<void> {
  await cdp.send("Input.dispatchKeyEvent", { type: "rawKeyDown", modifiers, ...key(name) });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", modifiers, ...key(name) });
}

/** One pair: the same state in light then dark (theme flipped via emulated media). */
async function pair(
  cdp: Cdp,
  slug: string,
  title: string,
  group: string,
  note = "",
  settle = 900,
): Promise<void> {
  try {
    await sleep(settle);
    const files: Record<string, string> = {};
    for (const mode of ["light", "dark"] as const) {
      await theme(cdp, mode);
      await sleep(mode === "light" ? 350 : 650);
      const file = `${slug}--${mode}.png`;
      await cdp.screenshot(path.join(OUT, file));
      files[mode] = file;
    }
    await theme(cdp, "light");
    manifest.push({ slug, title, group, note, light: files.light ?? "", dark: files.dark ?? "" });
    saveManifest();
    console.log(`pair ${slug}`);
  } catch (error) {
    problems.push(`${slug}: ${String(error).slice(0, 300)}`);
    console.log(`FAILED ${slug}: ${String(error).slice(0, 300)}`);
  }
}

/** Run a step; on failure log it and go on. */
async function step(name: string, body: () => Promise<void>): Promise<void> {
  try {
    await body();
  } catch (error) {
    problems.push(`${name}: ${String(error).slice(0, 400)}`);
    console.log(`STEP FAILED ${name}: ${String(error).slice(0, 400)}`);
  }
}

async function go(cdp: Cdp, hash: string, settle = 900): Promise<void> {
  await cdp.eval(`location.hash = ${JSON.stringify(hash)}`);
  await sleep(settle);
}

async function escape(cdp: Cdp): Promise<void> {
  await press(cdp, "Escape");
  await sleep(300);
}

async function clearComposer(cdp: Cdp): Promise<void> {
  await cdp.eval(`(() => { const t = document.querySelector('textarea'); if (t) { Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(t, ''); t.dispatchEvent(new Event('input', { bubbles: true })); t.blur(); } })()`);
  await sleep(300);
}

async function answerIfAsked(app: App, waitMs = 3_000): Promise<boolean> {
  const until = Date.now() + waitMs;
  while (Date.now() < until) {
    if (await app.cdp.eval(hasButton("Allow once"))) {
      await sleep(400);
      await app.cdp.click("Allow once");
      await app.cdp.waitFor(`!(${hasButton("Allow once")})`, "the card to close", 20_000);
      return true;
    }
    await sleep(200);
  }
  return false;
}

async function showPane(app: App, id: string, title: string): Promise<void> {
  const toggle = `[data-ui="pane-toggle-${id}"]`;
  if (await app.cdp.eval(`!!document.querySelector('${toggle}')`)) {
    if (!(await app.cdp.eval(`document.querySelector('${toggle}').getAttribute('aria-pressed') === 'true'`))) {
      await fx.clickAt(app.cdp, toggle);
    }
    await app.cdp.waitFor(`!!document.getElementById('pane-body-${id}')`, `the ${title} pane`, 20_000);
    return;
  }
  const opened = await app.cdp.eval(`!!document.getElementById('pane-tab-${id}')`);
  if (!opened) await fx.clickButton(app.cdp, `Open ${title}`);
  await fx.clickAt(app.cdp, `#pane-tab-${id}`);
  await app.cdp.waitFor(`!!document.getElementById('pane-body-${id}')`, `the ${title} pane`, 20_000);
}

async function hidePanes(cdp: Cdp): Promise<void> {
  await cdp.eval(`(() => {
    for (const id of ['files', 'review']) {
      const t = document.querySelector('[data-ui="pane-toggle-' + id + '"]');
      if (t && t.getAttribute('aria-pressed') === 'true') t.click();
    }
    for (const b of [...document.querySelectorAll('button[aria-label^="Close "]')]) {
      if (b.closest('.pane-zone, .pane-header, .pane-tabs, [data-pane]')) b.click();
    }
  })()`);
  await sleep(600);
}

function sceneDir(name: string): string {
  const dir = path.join(os.tmpdir(), "indus-shots", name);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function git(dir: string, ...args: string[]): void {
  const ran = spawnSync("git", args, {
    cwd: dir,
    encoding: "utf8",
    env: {
      ...process.env,
      GIT_CONFIG_GLOBAL: "/dev/null",
      GIT_CONFIG_NOSYSTEM: "1",
      GIT_AUTHOR_NAME: "Demo",
      GIT_AUTHOR_EMAIL: "demo@example.invalid",
      GIT_COMMITTER_NAME: "Demo",
      GIT_COMMITTER_EMAIL: "demo@example.invalid",
    },
  });
  if (ran.status !== 0) throw new Error(`git ${args.join(" ")}: ${ran.stderr}`);
}

async function settleTurn(app: App, stack: { command(op: Record<string, unknown>): Promise<Record<string, unknown>> }): Promise<void> {
  if (await app.cdp.waitFor(IDLE, "the turn to end", 20_000).then(() => true, () => false)) return;
  const used = await stack.command({ op: "model_turns" }).catch(() => ({}));
  problems.push(`note: composer still running 20 s after a reply (model turns ${JSON.stringify(used)}); reloaded`);
  await app.cdp.eval("location.reload()").catch(() => undefined);
  await sleep(1500);
  await app.cdp.waitFor(`document.readyState === "complete"`, "the reload", 30_000);
  await fx.waitConnected(app.cdp);
  await app.cdp.waitFor(IDLE, "the composer after the reload", 30_000).catch(() => undefined);
}

/** Walk a page's tab list (role=tab), one pair per further tab. */
async function walkTabs(cdp: Cdp, slug: string, title: string, group: string, skipFirst = true): Promise<void> {
  const tabs = await cdp.eval<string[]>(`[...document.querySelectorAll('main [role="tab"], .page [role="tab"], [role="tablist"] [role="tab"]')].filter((b) => !b.closest('.sidebar')).map((b) => b.textContent.trim())`);
  const unique = [...new Set(tabs)];
  for (const [i, label] of unique.entries()) {
    if (skipFirst && i === 0) continue;
    const kebab = label.toLowerCase().replace(/\(.*?\)|\d+/g, "").trim().replace(/[^a-z]+/g, "-").replace(/^-|-$/g, "");
    await cdp.eval(`(() => { const b = [...document.querySelectorAll('[role="tab"]')].find((x) => x.textContent.trim() === ${JSON.stringify(label)} && !x.closest('.sidebar')); if (b) b.click(); })()`);
    await pair(cdp, `${slug}-${kebab}`, `${title} › ${label}`, group, `${label} tab`, 1500);
  }
  if (unique.length > 0) await cdp.eval(`(() => { const b = [...document.querySelectorAll('[role="tab"]')].find((x) => x.textContent.trim() === ${JSON.stringify(unique[0])} && !x.closest('.sidebar')); if (b) b.click(); })()`);
}

const want = (k: string) => ONLY.length === 0 || ONLY.includes(k);
const cleanup: Array<() => Promise<unknown>> = [];

try {
  // == A. onboarding (fake backend, no provider set up) ==========================================
  if (want("onboarding")) {
    await step("onboarding", async () => {
      const work = sceneDir("onb");
      cleanup.push(async () => fs.rmSync(work, { recursive: true, force: true }));
      const scenario = structuredClone(fx.HEALTHY);
      scenario.rest.routes["GET /api/model/options"] = {
        status: 200,
        body: {
          providers: [
            { name: "openrouter", label: "OpenRouter", auth_type: "api_key", configured: false, device_login: false },
            { name: "anthropic", label: "Anthropic", auth_type: "api_key", configured: false, device_login: false },
            { name: "openai", label: "OpenAI", auth_type: "api_key", configured: false, device_login: false },
          ],
          current: { provider: null, model: null },
        },
      };
      const fake = fx.fakeBackend(work, scenario);
      const app = await fx.launchApp({ work, home: fake.home, indus: fake.indus });
      cleanup.push(() => app.stop());
      await size(app.cdp);
      await fx.waitConnected(app.cdp);
      await app.cdp.waitFor(
        `[...document.querySelectorAll('button')].some((b) => b.textContent.trim().startsWith('Paste an API key'))`,
        "the onboarding overlay",
        30_000,
      );
      await pair(app.cdp, "onboarding", "Onboarding: choose how Indus reaches a model", "Onboarding", "First run, no provider set up (fake backend)", 1200);
      await fx.clickButtonStarting(app.cdp, "Paste an API key", 30_000);
      await app.cdp.waitFor(`!!document.querySelector('.onboarding-form input[type="password"]')`, "the key form");
      await pair(app.cdp, "onboarding-api-key", "Onboarding: paste an API key", "Onboarding", "Provider picker and empty key field");
      await app.stop();
      await fx.stopUnder(work);
    });
  }

  // == B. the real runtime ========================================================================
  if (want("main")) {
    const work = sceneDir("chat");
    cleanup.push(async () => fs.rmSync(work, { recursive: true, force: true }));
    const notes = [
      "# Release notes",
      "",
      "## 0.2.0",
      "",
      "- `greet()` now trims the name before greeting.",
      "- Added a `--shout` flag that upper-cases the greeting.",
      "",
    ].join("\n");
    const stack = await fx.startStack(work, {
      daemon: true,
      turns: [
        // chat 1, turn 1: a tool row
        [
          { text: ["I'll draft ", "the release notes ", "in `NOTES.md`. "] },
          { tool_call: { name: "write_file", arguments: { path: "NOTES.md", content: notes }, id: "call_w" } },
          { finish: "tool_calls" },
        ],
        [
          {
            text: [
              "Done — `NOTES.md` now lists the two changes in **0.2.0**:\n\n",
              "1. `greet()` trims the name first.\n",
              "2. A new `--shout` flag upper-cases the greeting.\n\n",
              "Want me to add a changelog link to the README as well?",
            ],
          },
        ],
        // chat 1, turn 2: an approval
        [
          { text: ["Removing ", "the stale build output. "] },
          { tool_call: { name: "terminal", arguments: { command: "find dist -delete" }, id: "c_t" } },
          { finish: "tool_calls" },
        ],
        [{ text: ["Removed `dist/`. ", "The working tree now only adds `NOTES.md`."] }],
        // chat 2: a reply caught mid-stream
        [
          { text: ["`greet(name)` builds ", "a greeting string:\n\n", "- it takes a `name`\n"] },
          { stall: 25 },
          { text: ["- and returns `Hello, <name>!`\n\nThat is the whole module."] },
        ],
        // chat 3: a provider refusal (error state)
        [{ fail: { status: 400, body: '{"error": {"message": "demo: the provider refused this request"}}' } }],
        // spare turns
        [{ text: "Noted." }],
        [{ text: "Noted." }],
        [{ text: "Noted." }],
        [{ text: "Noted." }],
      ],
    });
    const repo = fx.gitRepo(path.join(work, "demo-app"));
    fs.mkdirSync(path.join(repo, "src"));
    fs.writeFileSync(path.join(repo, "src", "index.ts"), 'export const greet = (name: string) => `Hello, ${name}!`;\n');
    fs.writeFileSync(path.join(repo, "src", "shout.ts"), "export const shout = (s: string) => s.toUpperCase();\n");
    fs.writeFileSync(path.join(repo, "package.json"), '{\n  "name": "demo-app",\n  "version": "0.1.0"\n}\n');
    fs.writeFileSync(path.join(repo, ".gitignore"), "dist/\n");
    git(repo, "add", ".");
    git(repo, "commit", "-q", "-m", "greet()");
    fs.mkdirSync(path.join(repo, "dist"));
    fs.writeFileSync(path.join(repo, "dist", "index.js"), "// stale build\n");
    cleanup.push(async () => {
      await stack.quit();
      await fx.stopUnder(stack.root);
    });
    const app = await fx.launchApp({
      work,
      home: stack.home,
      indus: fx.REAL_INDUS,
      env: { ...stack.env, HOME: path.join(work, "user-home") },
    });
    cleanup.push(() => app.stop());
    const cdp = app.cdp;
    await size(cdp);
    await theme(cdp, "light");
    await fx.waitConnected(cdp);
    await sleep(3_000);

    // default folder for new chats = the demo repo
    await go(cdp, "#/settings?view=workspace&page=desktop", 300);
    await cdp.waitFor(`!!document.querySelector('[data-row="default-directory"] input')`, "the workspace page", 30_000);
    await fx.fillField(cdp, '[data-row="default-directory"] input', repo);
    await go(cdp, "#/", 300);
    await cdp.waitFor(`!document.querySelector('[data-row="default-directory"]')`, "Settings to close");

    await fx.clickButton(cdp, "New chat");
    await sleep(800);
    await pair(cdp, "new-chat", "New chat", "Chat", "Empty new chat in the demo-app folder", 800);

    await step("model-picker", async () => {
      await fx.clickAt(cdp, '[data-testid="model-picker"] > button');
      await cdp.waitFor(`!!document.querySelector('[data-testid="model-picker"] [role="dialog"], [data-testid="model-picker"] ul')`, "the model picker", 10_000);
      await pair(cdp, "model-picker", "Model picker", "Chat", "Composer model picker (fake-model on a custom endpoint)", 1200);
      await escape(cdp);
      if (await cdp.eval(`document.querySelector('[data-testid="model-picker"] > button')?.getAttribute('aria-expanded') === 'true'`)) {
        await fx.clickAt(cdp, '[data-testid="model-picker"] > button');
      }
    });

    await step("mention-menu", async () => {
      await cdp.type("textarea", "Look at @src");
      await sleep(1500);
      await pair(cdp, "mention-menu", "@-mention file menu", "Chat", "Typing @src in the composer", 600);
      await escape(cdp);
      await clearComposer(cdp);
    });

    await step("slash-menu", async () => {
      await cdp.type("textarea", "/");
      await pair(cdp, "slash-menu", "Slash-command menu", "Chat", "Typing / in the composer", 1200);
      await escape(cdp);
      await clearComposer(cdp);
    });

    // -- chat 1
    let chat1 = "";
    await step("chat1", async () => {
      await fx.sendPrompt(cdp, "Draft release notes for 0.2.0 in NOTES.md");
      await answerIfAsked(app);
      await cdp.waitFor(`document.querySelector('.transcript')?.innerText.includes('changelog link')`, "the reply after the tool", 60_000);
      chat1 = await cdp.eval<string>(`decodeURIComponent(location.hash.slice(2))`);
      await settleTurn(app, stack);
      await pair(cdp, "tool-row", "Tool row", "Chat", "write_file tool row (\"Wrote NOTES.md\") between streamed text and the answer", 1200);
      const toggled = await cdp.eval(`(() => { const row = document.querySelector('.tool-row'); const t = row && row.querySelector('button, summary, [role="button"]'); if (!t) return false; t.click(); return true; })()`);
      if (toggled) {
        await pair(cdp, "tool-row-expanded", "Tool row expanded", "Chat", "Arguments and output of the write_file call", 1000);
        await cdp.eval(`document.querySelector('.tool-row button, .tool-row summary, .tool-row [role="button"]')?.click()`);
        await sleep(400);
      }
      await fx.sendPrompt(cdp, "Clean out the old build folder");
      const asked = await cdp.waitFor(hasButton("Allow once"), "the approval card", 30_000).then(() => true, () => false);
      if (asked) {
        await sleep(600);
        await cdp.eval(`(() => { const allow = [...document.querySelectorAll('button')].find((x) => x.textContent.trim() === 'Allow once'); const card = allow?.parentElement?.parentElement ?? allow; card?.scrollIntoView({ block: 'end' }); })()`);
        await pair(cdp, "approval-card", "Approval card", "Chat", "\"Allow this action?\" for a risky command (find dist -delete)", 800);
        await answerIfAsked(app, 10_000);
      } else problems.push("approval-card: no approval card appeared");
      await cdp.waitFor(`document.querySelector('.transcript')?.innerText.includes('only adds')`, "the reply after the approval", 60_000);
      await settleTurn(app, stack);
      await pair(cdp, "chat-conversation", "Chat conversation", "Chat", "Finished two-turn conversation with two tool rows", 1200);
    });

    await step("find-bar", async () => {
      await press(cdp, "F", 2);
      await cdp.waitFor(`!!document.querySelector('.find-bar input, [role="search"] input')`, "the find bar", 5_000);
      await cdp.send("Input.insertText", { text: "NOTES" });
      await pair(cdp, "find-bar", "Find in transcript", "Chat", "Ctrl+F find bar with matches highlighted", 1000);
      await escape(cdp);
    });

    // panes
    await step("review-pane", async () => {
      await showPane(app, "review", "Review");
      await cdp.waitFor(`document.getElementById('pane-body-review')?.innerText.includes('NOTES.md')`, "the review pane to list NOTES.md", 30_000);
      await cdp.eval(`(() => { const body = document.getElementById('pane-body-review'); const hit = [...body.querySelectorAll('button[aria-pressed]')].find((e) => e.textContent.trim() === 'NOTES.md'); if (hit) hit.click(); })()`);
      await pair(cdp, "review-pane", "Review pane", "Panes", "NOTES.md diff with stage/revert and commit controls", 1200);
    });
    await step("files-pane", async () => {
      await showPane(app, "files", "Files");
      await cdp.waitFor(`document.getElementById('pane-body-files')?.innerText.includes('NOTES.md')`, "the files pane", 30_000);
      await pair(cdp, "files-pane", "Files pane", "Panes", "Files and Review panes side by side (demo-app tree)", 1000);
    });
    await step("terminal-pane", async () => {
      await hidePanes(cdp);
      await showPane(app, "terminal", "Terminal");
      await sleep(2500);
      await pair(cdp, "terminal-pane", "Terminal pane", "Panes", "Integrated terminal (xterm over node-pty) in the chat's folder", 800);
    });
    await step("preview-pane", async () => {
      await hidePanes(cdp);
      await showPane(app, "preview", "Preview");
      await pair(cdp, "preview-pane", "Preview pane", "Panes", "Browser preview pane, nothing opened yet", 1200);
    });
    await step("plugins-pane", async () => {
      await hidePanes(cdp);
      const label = await cdp.eval<string | null>(`(() => { const b = [...document.querySelectorAll('.pane-toolbar button')].find((x) => /^Open Plugins/.test(x.textContent.trim())); return b ? b.textContent.trim() : null; })()`);
      if (label === null) throw new Error("no Open Plugins button");
      await fx.clickButton(cdp, label);
      await pair(cdp, "plugins", "Plugins (tool pane)", "Panes", "Desktop plugin inventory opened as a tool pane", 1500);
      await hidePanes(cdp);
    });
    await step("layout-everything", async () => {
      const presets = await cdp.eval<string[]>(`[...document.querySelectorAll('.pane-toolbar select option')].map((o) => o.value).filter(Boolean)`);
      console.log(`presets: ${presets.join(",")}`);
      const pick = presets.find((p) => /all|every|full|ide|work/i.test(p)) ?? presets[presets.length - 1];
      if (!pick) throw new Error("no presets");
      await cdp.eval(`(() => { const s = document.querySelector('.pane-toolbar select'); Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(s, ${JSON.stringify(pick)}); s.dispatchEvent(new Event('change', { bubbles: true })); })()`);
      await pair(cdp, "right-sidebar", "Tool panes beside the chat (layout preset)", "Panes", `Layout preset "${pick}": the tool zone beside the chat`, 2500);
      await cdp.eval(`(() => { const s = document.querySelector('.pane-toolbar select'); const first = ${JSON.stringify(presets[0] ?? "")}; Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(s, first); s.dispatchEvent(new Event('change', { bubbles: true })); })()`);
      await sleep(800);
      await hidePanes(cdp);
    });

    // -- chat 2: mid-stream
    await step("chat-streaming", async () => {
      await fx.clickButton(cdp, "New chat");
      await sleep(600);
      await fx.sendPrompt(cdp, "Explain the greet function");
      await cdp.waitFor(`document.querySelector('.transcript')?.innerText.includes('it takes a')`, "the first chunks", 30_000);
      await pair(cdp, "chat-streaming", "Reply streaming", "Chat", "A reply mid-stream (model paused): Stop/Steer composer", 500);
      await cdp.waitFor(`document.querySelector('.transcript')?.innerText.includes('whole module')`, "the rest of the reply", 60_000);
      await settleTurn(app, stack);
    });

    // -- chat 3: error state
    await step("error-state", async () => {
      await fx.clickButton(cdp, "New chat");
      await sleep(600);
      await fx.sendPrompt(cdp, "Summarise the open issues");
      await cdp.waitFor(`/refused|error|failed/i.test(document.querySelector('.transcript')?.innerText ?? '')`, "the error card", 60_000);
      await sleep(1500);
      await pair(cdp, "error-state", "Error card", "Chat", "A turn the provider refused (HTTP 400 from the fake model)", 800);
    });

    // sidebar
    await step("session-sidebar", async () => {
      if (chat1 !== "") await go(cdp, `#/${encodeURIComponent(chat1)}`, 1500);
      await cdp.eval(`(() => { const el = [...document.querySelectorAll('.sidebar button[aria-label^="Pin "]')][1]; if (el) el.scrollIntoView(); })()`);
      const at = await cdp.eval<{ x: number; y: number } | null>(`(() => { const el = [...document.querySelectorAll('.sidebar button[aria-label^="Pin "]')][0]; const row = el?.parentElement; if (!row) return null; const r = row.getBoundingClientRect(); return { x: r.x + 40, y: r.y + r.height / 2 }; })()`);
      if (at) await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: at.x, y: at.y });
      await pair(cdp, "session-sidebar", "Session sidebar", "Sidebar", "Three sessions; hovered row shows pop-out / pin / archive", 800);
      if (at) await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 900, y: 500 });
    });
    await step("session-search", async () => {
      await fx.fillField(cdp, 'input[aria-label="Search conversations"], .sidebar input[type="search"], .sidebar input', "release");
      await cdp.eval(`document.querySelector('.sidebar input')?.focus()`);
      await pair(cdp, "session-switcher", "Sidebar session search", "Sidebar", "Filtering conversations by \"release\"", 1200);
      await fx.fillField(cdp, ".sidebar input", "");
    });

    await step("command-palette", async () => {
      await press(cdp, "P", 2 | 8);
      await cdp.waitFor(`!!document.querySelector('.command-palette input')`, "the command palette", 10_000);
      await pair(cdp, "command-palette", "Command palette", "Navigation", "Ctrl+Shift+P", 1000);
      await escape(cdp);
    });

    await step("context-menu", async () => {
      // the native context menu (Electron Menu.popup) is an X window: grab the whole screen
      const at = await cdp.eval<{ x: number; y: number } | null>(`(() => { const el = document.querySelector('.transcript .msg-assistant .msg-text'); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x + 30, y: r.y + 8 }; })()`);
      if (!at) throw new Error("no assistant message");
      await cdp.send("Emulation.clearDeviceMetricsOverride", {});
      await sleep(800);
      const real = await cdp.eval<{ x: number; y: number } | null>(`(() => { const el = document.querySelector('.transcript .msg-assistant .msg-text'); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x + 30, y: r.y + 8 }; })()`);
      const win = await cdp.eval<{ x: number; y: number; w: number; h: number }>(`({ x: window.screenX, y: window.screenY, w: window.outerWidth, h: window.outerHeight })`);
      const files: Record<string, string> = {};
      for (const mode of ["light", "dark"] as const) {
        await theme(cdp, mode);
        await sleep(600);
        await cdp.eval(`(() => { const el = document.querySelector('.transcript .msg-assistant .msg-text'); const r = document.createRange(); r.selectNodeContents(el); const s = getSelection(); s.removeAllRanges(); s.addRange(r); })()`);
        for (const type of ["mousePressed", "mouseReleased"]) {
          await cdp.send("Input.dispatchMouseEvent", { type, x: real?.x ?? 100, y: real?.y ?? 100, button: "right", clickCount: 1 });
        }
        await sleep(1200);
        const file = `context-menu--${mode}.png`;
        const raw = path.join(os.tmpdir(), "indus-shots", `root-${mode}.png`);
        const grab = spawnSync("import", ["-window", "root", raw], { encoding: "utf8" });
        if (grab.status !== 0) throw new Error(`import: ${grab.stderr}`);
        const crop = spawnSync("convert", [raw, "-crop", `${win.w}x${win.h}+${Math.max(0, win.x)}+${Math.max(0, win.y)}`, "+repage", "-background", "white", "-gravity", "northwest", "-extent", `${W}x${H}`, path.join(OUT, file)], { encoding: "utf8" });
        if (crop.status !== 0) throw new Error(`convert: ${crop.stderr}`);
        files[mode] = file;
        await press(cdp, "Escape");
        await sleep(600);
      }
      await theme(cdp, "light");
      await size(cdp);
      manifest.push({ slug: "context-menu", title: "Context menu", group: "Chat", note: `Native right-click menu on selected reply text (X screen grab of the ${win.w}x${win.h} window, padded to 1440x900)`, light: files.light ?? "", dark: files.dark ?? "" });
      saveManifest();
      console.log("pair context-menu");
    });
    await size(cdp).catch(() => undefined);

    await step("simple-mode", async () => {
      if (chat1 !== "") await go(cdp, `#/${encodeURIComponent(chat1)}`, 1200);
      await cdp.eval(`(() => { const b = [...document.querySelectorAll('.pane-toolbar [role="radio"]')].find((x) => x.textContent.trim() === 'Simple'); if (b) b.click(); })()`);
      await pair(cdp, "simple-mode", "Simple interface mode", "Chat", "The same conversation in Simple mode (fewer surfaces)", 1200);
      await cdp.eval(`(() => { const b = [...document.querySelectorAll('[role="radio"]')].find((x) => x.textContent.trim() === 'Advanced'); if (b) b.click(); })()`);
      await sleep(800);
    });

    // -- pages
    const pages: Array<[string, string, string, string, string]> = [
      ["#/capabilities?tab=skills", "skills", "Capabilities › Skills", "Capabilities", "Capabilities page, Skills tab"],
      ["#/capabilities?tab=toolsets", "tools", "Capabilities › Toolsets", "Capabilities", "Capabilities page, Toolsets tab"],
      ["#/capabilities?tab=connectors", "mcp", "Capabilities › Connectors (MCP)", "Capabilities", "Capabilities page, Connectors (MCP servers) tab"],
      ["#/capabilities?tab=plugins", "capabilities", "Capabilities › Plugins", "Capabilities", "Capabilities page, Plugins tab"],
      ["#/messaging", "messaging", "Messaging", "Pages", "Messaging platforms"],
      ["#/artifacts", "artifacts", "Artifacts", "Pages", ""],
      ["#/webhooks", "webhooks", "Webhooks", "Pages", ""],
      ["#/command-center", "command-center", "Command center", "Pages", "Sessions section"],
      ["#/learning-map", "starmap", "Learning map", "Pages", "Skills and memories as a force-directed graph"],
      ["#/bots", "bots", "Bots", "Pages", "Profiles as bots (bundled contribution)"],
      ["#/cron", "cron", "Cron", "Overlays", "Scheduled jobs overlay"],
      ["#/profiles", "profiles", "Profiles", "Overlays", "Profiles overlay"],
      ["#/agents", "agents", "Agents", "Overlays", "Delegation overlay (opened cold: no conversation)"],
    ];
    for (const [hash, slug, title, group, note] of pages) {
      await step(slug, async () => {
        await go(cdp, "#/", 400);
        await go(cdp, hash, 2500);
        await pair(cdp, slug, title, group, note, 500);
        if (slug === "command-center") {
          const tabs = await cdp.eval<string[]>(`[...document.querySelectorAll('.manage-tabs [role="tab"]')].map((b) => b.textContent.trim())`);
          for (const [i, label] of tabs.entries()) {
            if (i === 0) continue;
            await cdp.eval(`document.querySelectorAll('.manage-tabs [role="tab"]')[${i}].click()`);
            const s = /maint/i.test(label) ? "session-import" : `command-center-${label.toLowerCase().replace(/[^a-z]+/g, "-")}`;
            await pair(cdp, s, `Command center › ${label}`, "Pages", /maint/i.test(label) ? "Maintenance: doctor, backup, prune, and session import from Claude Code / Codex" : `${label} section`, 2000);
          }
        } else if (!slug.startsWith("capab") && !["skills", "tools", "mcp"].includes(slug)) {
          await walkTabs(cdp, slug, title, group);
        }
      });
    }

    // -- Settings: every page
    const VIEWS: Record<string, string[]> = {
      model: ["main", "fallbacks", "auxiliary"],
      chat: ["behaviour", "personality", "voice"],
      appearance: ["theme", "typography", "window"],
      workspace: ["projects", "desktop", "shell", "files"],
      safety: ["approvals", "privacy", "checkpoints"],
      memory: ["persistent", "context"],
      advanced: ["desktop", "runtime", "tools", "terminal", "profiles", "plugins"],
      providers: ["accounts", "keys", "endpoints"],
      gateways: ["connection", "connections", "storage"],
      keyboard: ["shortcuts"],
      notifications: ["alerts"],
      sessions: ["archived", "directory"],
      about: ["version", "licences"],
    };
    const CANON: Record<string, string> = {
      "chat/behaviour": "settings-general",
      "appearance/theme": "settings-appearance",
      "model/main": "settings-models",
      "providers/accounts": "settings-providers",
      "providers/keys": "settings-keys",
      "keyboard/shortcuts": "settings-shortcuts",
      "advanced/desktop": "settings-advanced",
      "about/version": "settings-about",
      "about/licences": "settings-licences",
    };
    for (const [view, pagesOf] of Object.entries(VIEWS)) {
      for (const page of pagesOf) {
        await step(`settings ${view}/${page}`, async () => {
          await go(cdp, `#/settings?view=${view}&page=${page}`, 600);
          await cdp.waitFor(`!!document.querySelector('input[aria-label="Search settings"]')`, "settings", 20_000);
          const heading = await cdp.eval<string>(`(() => { const h = document.querySelector('.settings-page h2, .settings-content h2, [role="dialog"] h2'); return h ? h.textContent.trim() : ''; })()`);
          const slug = CANON[`${view}/${page}`] ?? `settings-${view}-${page}`;
          await pair(cdp, slug, `Settings › ${view} › ${heading || page}`, "Settings", `#/settings?view=${view}&page=${page}`, 1200);
        });
      }
    }
    await step("settings-search", async () => {
      await go(cdp, "#/settings?view=model&page=main", 600);
      await fx.fillField(cdp, 'input[aria-label="Search settings"]', "theme");
      await cdp.eval(`document.querySelector('input[aria-label="Search settings"]').focus()`);
      await pair(cdp, "settings-search", "Settings search", "Settings", "Searching settings for \"theme\"", 1200);
    });
    await go(cdp, "#/", 800);

    // -- deep links: confirmation dialogs and a refusal toast
    await step("deep-links", async () => {
      await app.secondLaunch(`${fx.DEV_SCHEME}://skill/demo-skill`);
      await cdp.waitFor(hasButton("Install"), "the skill confirmation", 20_000);
      await pair(cdp, "deep-link-skill", "Deep link: install a skill", "Deep links", "indus://skill/demo-skill asks first", 800);
      await fx.clickButton(cdp, "Cancel");
      await app.secondLaunch(`${fx.DEV_SCHEME}://mcp/docs?url=https%3A%2F%2Fexample.invalid%2Fmcp`);
      await cdp.waitFor(hasButton("Add the server"), "the MCP confirmation", 20_000);
      await pair(cdp, "deep-link-mcp", "Deep link: add an MCP server", "Deep links", "indus://mcp/docs?url=… asks first", 800);
      await fx.clickButton(cdp, "Cancel");
      await app.secondLaunch(`${fx.DEV_SCHEME}://mcp/evil?command=rm&args=-rf`);
      await cdp.waitFor(`document.body.innerText.includes('A link may not add a server that runs a program')`, "the refusal", 20_000);
      await pair(cdp, "notifications-toast", "Toast", "Deep links", "Refusal toast for an MCP link that would run a program", 600);
    });

    // -- HUD (floating conversation window)
    await step("hud", async () => {
      if (chat1 === "") throw new Error("no chat 1 session");
      const opened = await fx.bridge(cdp, `b.hud.open({ sessionId: ${JSON.stringify(chat1)}, profile: "default" })`);
      if (!opened.ok) throw new Error(`hud.open: ${opened.error}`);
      const hud = await app.attach((t: { type: string; url: string }) => t.type === "page" && t.url.includes("win=hud"), "the HUD window");
      await hud.send("Runtime.enable");
      await sleep(2500);
      const dims = await hud.eval<{ w: number; h: number }>(`({ w: innerWidth, h: innerHeight })`);
      await pair(hud, "hud", "HUD (floating conversation)", "Windows", `Pop-out HUD window on chat 1 at its natural size (${dims.w}x${dims.h})`, 500);
      await fx.bridge(cdp, `b.hud.close()`);
      hud.close();
    });

    // -- quick entry: the quick window's renderer, shown in the main window (its own window opens
    // only on a global shortcut, which xvfb cannot press)
    await step("quick-entry", async () => {
      const base = await cdp.eval<string>(`location.href.split('#')[0].split('?')[0]`);
      await cdp.eval(`location.href = ${JSON.stringify(`${base}?win=quick`)}`);
      await sleep(2500);
      await size(cdp, 680, 120);
      await pair(cdp, "quick-entry", "Quick entry", "Windows", "The quick-entry window's page (?win=quick), rendered at 680x120 (its own window opens on a global shortcut)", 800);
      await size(cdp);
    });
  }
} finally {
  saveManifest();
  for (const s of cleanup.reverse()) {
    try {
      await s();
    } catch (error) {
      console.error(`cleanup: ${error}`);
    }
  }
  cleanupFakes();
  fs.rmSync(path.join(os.tmpdir(), "indus-shots"), { recursive: true, force: true });
}
console.log(`desktop-all done: ${manifest.length} pairs, ${problems.length} problems`);
for (const p of problems) console.log(`problem: ${p}`);
process.exit(0);
