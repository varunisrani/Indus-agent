// Desktop (Electron) screenshots, built on the repo's own E2E harness (apps/desktop/e2e/fixtures.ts):
// the fake backend for the onboarding overlay, and the real runtime (tests/fakes/web_stack.py with
// the scripted fake model) for the chat, tool row, approval card, panes and Settings.
//
//   WT=<worktree> OUT=<png dir> xvfb-run -a -s "-screen 0 1920x1080x24" node desktop.ts
//
// Nothing is written into the worktree except the build that fixtures.buildApp() puts in a temp dir.

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

async function size(app: App): Promise<void> {
  await app.cdp.send("Emulation.setDeviceMetricsOverride", {
    width: W,
    height: H,
    deviceScaleFactor: 1,
    mobile: false,
  });
}

async function shot(app: App, name: string, settle = 800): Promise<void> {
  await sleep(settle);
  const file = path.join(OUT, name);
  await app.cdp.screenshot(file);
  if (!fs.existsSync(file)) throw new Error(`no screenshot for ${name}`);
  console.log(`shot ${name}`);
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
}

/** A fixed, short folder per scene so on-screen paths read well (removed afterwards). */
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

/**
 * Wait for the composer to show the turn as ended. Seen on this build: after a finished turn the
 * composer can stay in its running state (Stop/Steer) indefinitely although the model was not
 * called again; a reload shows the true state. Logged so the report can mention it.
 */
async function settle(app: App, stack: { command(op: Record<string, unknown>): Promise<Record<string, unknown>> }): Promise<void> {
  const t0 = Date.now();
  if (await app.cdp.waitFor(IDLE, "the turn to end", 20_000).then(() => true, () => false)) {
    console.log(`composer idle ${Date.now() - t0} ms after the reply (no reload)`);
    return;
  }
  const used = await stack.command({ op: "model_turns" }).catch(() => ({}));
  console.log(`note: composer still running 20 s after the reply (model turns used ${JSON.stringify(used)}); reloading`);
  await app.cdp.eval("location.reload()").catch(() => undefined);
  await sleep(1500);
  await app.cdp.waitFor(`document.readyState === "complete"`, "the reload", 30_000);
  await size(app);
  await fx.waitConnected(app.cdp);
  await app.cdp.waitFor(IDLE, "the composer after the reload", 30_000).catch(() => console.log("note: still running after the reload"));
}

const want = (key: string) => ONLY.length === 0 || ONLY.includes(key);
const cleanup: Array<() => Promise<unknown>> = [];

try {
  // -- 1. first-run onboarding overlay (fake backend, no provider set up) -------------------------
  if (want("onboarding")) {
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
    await size(app);
    await fx.waitConnected(app.cdp);
    await app.cdp.waitFor(
      `[...document.querySelectorAll('button')].some((b) => b.textContent.trim().startsWith('Paste an API key'))`,
      "the onboarding overlay",
      30_000,
    );
    await shot(app, "desktop-01-onboarding.png", 1200);
    await fx.clickButtonStarting(app.cdp, "Paste an API key", 30_000);
    await app.cdp.waitFor(
      `!!document.querySelector('.onboarding-form input[type="password"]')`,
      "the key form",
    );
    await shot(app, "desktop-02-onboarding-api-key.png");
    await app.stop();
    await fx.stopUnder(work); // the fake daemon and serve the app started for this home
  }

  // -- 2. the real runtime: chat, tool row, approval, panes, settings ----------------------------
  if (want("chat")) {
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
        [
          { text: ["I'll draft ", "the release notes ", "in `NOTES.md`. "] },
          {
            tool_call: {
              name: "write_file",
              arguments: { path: "NOTES.md", content: notes },
              id: "call_w",
            },
          },
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
        [
          { text: ["Removing ", "the stale build output. "] },
          { tool_call: { name: "terminal", arguments: { command: "find dist -delete" }, id: "c_t" } },
          { finish: "tool_calls" },
        ],
        [{ text: ["Removed `dist/`. ", "The working tree now only adds `NOTES.md`."] }],
        // spare turns for any background model call after the conversation
        [{ text: "Noted." }],
        [{ text: "Noted." }],
        [{ text: "Noted." }],
      ],
    });
    // the demo repository is the chat's folder (Settings › Workspace default folder)
    const repo = fx.gitRepo(path.join(work, "demo-app"));
    fs.mkdirSync(path.join(repo, "src"));
    fs.writeFileSync(path.join(repo, "src", "index.ts"), 'export const greet = (name: string) => `Hello, ${name}!`;\n');
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
    await size(app);
    await fx.waitConnected(app.cdp);
    await sleep(3_000);

    // default folder for new chats = the demo repo
    await app.cdp.eval(`location.hash = '#/settings?view=workspace&page=desktop'`);
    await app.cdp.waitFor(
      `!!document.querySelector('[data-row="default-directory"] input')`,
      "the workspace page",
      30_000,
    );
    await fx.fillField(app.cdp, '[data-row="default-directory"] input', repo);
    await app.cdp.eval(`location.hash = '#/'`);
    await app.cdp.waitFor(`!document.querySelector('[data-row="default-directory"]')`, "Settings to close");

    await fx.clickButton(app.cdp, "New chat");
    await sleep(800);
    await shot(app, "desktop-03-new-chat.png");

    await fx.sendPrompt(app.cdp, "Draft release notes for 0.2.0 in NOTES.md");
    await answerIfAsked(app);
    await app.cdp.waitFor(
      `document.querySelector('.transcript')?.innerText.includes('changelog link')`,
      "the reply after the tool",
      60_000,
    );
    await settle(app, stack);
    await shot(app, "desktop-04-chat-tool-row.png", 1200);

    // expand the tool row if it has a toggle
    const expanded = await app.cdp.eval(`(() => {
      const row = document.querySelector('.tool-row');
      if (!row) return 'no row';
      const t = row.querySelector('button, summary, [role="button"]');
      if (!t) return 'no toggle';
      t.click();
      return 'clicked';
    })()`);
    console.log(`tool row toggle: ${expanded}`);
    if (expanded === "clicked") await shot(app, "desktop-05-tool-row-expanded.png", 1000);
    if (expanded === "clicked") {
      // collapse it again so the next turn's approval card fits on screen
      await app.cdp.eval(`document.querySelector('.tool-row button, .tool-row summary, .tool-row [role="button"]')?.click()`);
      await sleep(400);
    }

    await fx.sendPrompt(app.cdp, "Clean out the old build folder");
    const asked = await app.cdp
      .waitFor(hasButton("Allow once"), "the approval card", 30_000)
      .then(() => true)
      .catch(() => false);
    if (asked) {
      await sleep(600);
      await app.cdp.eval(`(() => {
        const allow = [...document.querySelectorAll('button')].find((x) => x.textContent.trim() === 'Allow once');
        const card = allow?.parentElement?.parentElement ?? allow;
        card?.scrollIntoView({ block: 'end' });
      })()`);
      await shot(app, "desktop-06-approval-card.png", 800);
      await answerIfAsked(app, 10_000);
    }
    await app.cdp.waitFor(
      `document.querySelector('.transcript')?.innerText.includes('only adds')`,
      "the reply after the approval",
      60_000,
    );
    await settle(app, stack);
    await shot(app, "desktop-07-chat-conversation.png", 1200);

    // reload: the rows are rebuilt from the stored history
    await app.cdp.eval("location.reload()").catch(() => undefined);
    await sleep(1500);
    await app.cdp.waitFor(`document.readyState === "complete"`, "the reload", 30_000);
    await size(app);
    await fx.waitConnected(app.cdp);
    await app.cdp.waitFor(
      `document.querySelector('.transcript')?.innerText.includes('only adds')`,
      "the transcript after the reload",
      30_000,
    );
    await app.cdp.eval(`(() => {
      const row = document.querySelector('.tool-row');
      const t = row && row.querySelector('button, summary, [role="button"]');
      if (t) t.click();
    })()`);
    await shot(app, "desktop-13-tool-rows-after-reload.png", 1200);

    await showPane(app, "review", "Review");
    await app.cdp.waitFor(
      `document.getElementById('pane-body-review')?.innerText.includes('NOTES.md')`,
      "the review pane to list NOTES.md",
      30_000,
    );
    // open the file's diff if the row is clickable
    await app.cdp.eval(`(() => {
      const body = document.getElementById('pane-body-review');
      const hit = [...body.querySelectorAll('button[aria-pressed]')].find((e) => e.textContent.trim() === 'NOTES.md');
      if (hit) hit.click();
    })()`);
    await shot(app, "desktop-08-review-pane.png", 1200);

    await showPane(app, "files", "Files");
    await app.cdp.waitFor(
      `document.getElementById('pane-body-files')?.innerText.includes('NOTES.md')`,
      "the files pane to list NOTES.md",
      30_000,
    );
    await shot(app, "desktop-09-files-pane.png", 1000);

    for (const [view, page, name] of [
      ["appearance", "theme", "desktop-10-settings-appearance.png"],
      ["about", "version", "desktop-11-settings-about.png"],
      ["about", "licences", "desktop-14-settings-licences.png"],
    ] as const) {
      await app.cdp.eval(`location.hash = '#/settings?view=${view}&page=${page}'`);
      await sleep(500);
      await app.cdp.waitFor(
        `!!document.querySelector('input[aria-label="Search settings"]')`,
        `the ${view} settings page`,
        20_000,
      );
      await shot(app, name, 1200);
    }
    await app.cdp.eval(`location.hash = '#/'`);
    await sleep(800);

    // the slash menu in the composer
    await fx.clickButton(app.cdp, "New chat");
    await app.cdp.waitFor(`!!document.querySelector('textarea')`, "the composer");
    await app.cdp.type("textarea", "/");
    await shot(app, "desktop-12-slash-menu.png", 1200);
    await app.cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await app.cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await app.cdp.eval(`(() => { const t = document.querySelector('textarea'); if (t) { Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(t, ''); t.dispatchEvent(new Event('input', { bubbles: true })); t.blur(); } })()`);
    await sleep(500);

    // the command palette: Ctrl+Shift+P
    const ctrlShift = 2 | 8;
    await app.cdp.send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "P", code: "KeyP", windowsVirtualKeyCode: 80, modifiers: ctrlShift });
    await app.cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "P", code: "KeyP", windowsVirtualKeyCode: 80, modifiers: ctrlShift });
    const palette = await app.cdp
      .waitFor(`!!document.querySelector('.command-palette input')`, "the command palette", 10_000)
      .then(() => true, (e) => { console.log(`note: no palette: ${String(e).slice(0, 200)}`); return false; });
    if (palette) await shot(app, "desktop-15-command-palette.png", 1000);
    await app.cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await app.cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await sleep(500);

    // the layout toolbar (Files / Review toggles), as a 2x crop of the top bar
    const bar = await app.cdp.eval(`(() => { const b = document.querySelector('.pane-toolbar'); if (!b) return null; const r = b.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; })()`);
    if (bar) {
      const crop = (await app.cdp.send("Page.captureScreenshot", {
        format: "png",
        clip: { x: Math.max(0, bar.x - 4), y: Math.max(0, bar.y - 4), width: Math.min(W, bar.width + 8), height: bar.height + 8, scale: 2 },
      })) as { data: string };
      fs.writeFileSync(path.join(OUT, "desktop-16-layout-toolbar.png"), Buffer.from(crop.data, "base64"));
      console.log("shot desktop-16-layout-toolbar.png");
    } else console.log("note: no .pane-toolbar");
  }
} finally {
  for (const step of cleanup.reverse()) {
    try {
      await step();
    } catch (error) {
      console.error(`cleanup: ${error}`);
    }
  }
  cleanupFakes();
  fs.rmSync(path.join(os.tmpdir(), "indus-shots"), { recursive: true, force: true });
}
console.log("desktop done");
process.exit(0);
