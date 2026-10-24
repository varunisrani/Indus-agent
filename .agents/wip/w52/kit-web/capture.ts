// Side-by-side capture of the Indus web dashboard: every page and notable component, light and dark,
// 1440x900, into $OUT as <slug>--light.png / <slug>--dark.png plus manifest.json (compare brief).
// Built on the repo's web_stack harness (stack.ts): only the model is faked.
//
//   WT=<worktree> OUT=<png dir> PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node capture.ts

import fs from "node:fs";
import path from "node:path";
import type { Page } from "@playwright/test";
import { CONFIG_EXTRA, chromium, OUT, seed, sleep, Stack, TURNS } from "./stack.ts";

const FAIL = path.join(path.dirname(new URL(import.meta.url).pathname), "..", "fail");
fs.mkdirSync(FAIL, { recursive: true });
const VIEW = { width: 1440, height: 900 };
const THEME_KEY = "indus-theme";
type Theme = "light" | "dark";
interface Entry {
  slug: string;
  title: string;
  group: string;
  note: string;
  light: string;
  dark: string;
}
const manifest: Entry[] = [];
const failures: string[] = [];
const MISSING: Record<string, string> = {
  docs: "Documentation is an external link (GitHub docs folder) in the sidebar, not a dashboard page.",
  "theme-menu": "No theme menu: a single Light/Dark toggle button in the sidebar footer (visible in every shot).",
  "language-menu": "The web dashboard has no language/locale picker.",
};

for (const f of fs.readdirSync(OUT)) if (f.endsWith(".png") || f === "manifest.json") fs.rmSync(path.join(OUT, f));

const file = (slug: string, theme: Theme) => `${slug}--${theme}.png`;

/** Flip the dashboard's own theme toggle (a DOM click, so an open dialog or menu stays open). */
async function toggleTheme(page: Page, want: Theme): Promise<void> {
  const label = want === "dark" ? "Switch to dark theme" : "Switch to light theme";
  const flipped = await page.evaluate((l) => {
    const b = document.querySelector<HTMLButtonElement>(`button[aria-label="${l}"]`);
    if (!b) return false;
    b.click();
    return true;
  }, label);
  if (!flipped) throw new Error(`no "${label}" toggle on this page`);
  await page.waitForFunction((t) => document.documentElement.dataset.theme === t, `indus-${want}`);
}

/** Light then dark of the current state, via the in-page toggle; leaves the page in light. */
async function pair(page: Page, slug: string, title: string, group: string, note: string, settle = 800) {
  await sleep(settle);
  const theme = await page.evaluate(() => document.documentElement.dataset.theme);
  if (theme !== "indus-light") await toggleTheme(page, "light");
  await sleep(250);
  await page.screenshot({ path: path.join(OUT, file(slug, "light")) });
  await toggleTheme(page, "dark");
  await sleep(500);
  await page.screenshot({ path: path.join(OUT, file(slug, "dark")) });
  await toggleTheme(page, "light");
  manifest.push({ slug, title, group, note, light: file(slug, "light"), dark: file(slug, "dark") });
  console.log(`shot ${slug}`);
}

/** One capture step; a failure is recorded and the run goes on. */
async function step(page: Page, name: string, fn: () => Promise<void>) {
  try {
    await fn();
  } catch (e) {
    const msg = `${name}: ${String(e).split("\n").slice(0, 3).join(" | ")}`;
    failures.push(msg);
    console.log(`FAIL ${msg}`);
    await page.screenshot({ path: path.join(FAIL, `fail-${name}.png`) }).catch(() => undefined);
    await page.keyboard.press("Escape").catch(() => undefined);
  }
}

async function scrollMain(page: Page, to: number | "bottom" | string) {
  await page.evaluate((t) => {
    const main = document.querySelector("main");
    if (!main) return;
    if (t === "bottom") main.scrollTop = main.scrollHeight;
    else if (typeof t === "number") main.scrollTop = t;
    else {
      const h = Array.from(main.querySelectorAll("h2,h3")).find((e) => e.textContent?.trim() === t);
      if (h) main.scrollTop = (h as HTMLElement).getBoundingClientRect().top - main.getBoundingClientRect().top + main.scrollTop - 16;
    }
  }, to);
}

async function visit(page: Page, url: string) {
  await page.goto(url);
  await page.waitForLoadState("networkidle").catch(() => undefined);
}

const browser = await chromium.launch({ headless: true });
const stacks: Stack[] = [];
let seedNotes: string[] = [];

try {
  // -- 1. sign-in page: the dashboard bound beyond loopback with a username/password ---------------
  {
    const password = ["shots", "pass", String(Date.now())].join("-");
    const stack = await Stack.start({
      turns: [[{ text: "Hello." }]],
      dashboard: { host: "127.0.0.2" },
      basic_auth: { username: "admin", password },
    });
    stacks.push(stack);
    for (const theme of ["light", "dark"] as const) {
      const context = await browser.newContext({ viewport: VIEW, colorScheme: theme });
      await context.addInitScript(
        ([k, v]) => {
          try {
            localStorage.setItem(k, v);
          } catch {}
        },
        [THEME_KEY, `indus-${theme}`],
      );
      const page = await context.newPage();
      await page.goto(`${stack.url}/sessions`);
      await page.getByLabel("Username").waitFor();
      await page.getByLabel("Username").fill("admin");
      await sleep(700);
      await page.screenshot({ path: path.join(OUT, file("login", theme)) });
      await context.close();
    }
    manifest.push({
      slug: "login",
      title: "Sign in",
      group: "Auth",
      note: "Username/password sign-in shown when the dashboard is bound beyond loopback (127.0.0.2 here). The page has no theme toggle; the theme comes from the stored choice / system preference.",
      light: file("login", "light"),
      dark: file("login", "dark"),
    });
    console.log("shot login");
    await stack.close();
    stacks.pop();
  }

  // -- 2. the loopback dashboard, seeded -----------------------------------------------------------
  const stack = await Stack.start({ turns: TURNS, config_extra: CONFIG_EXTRA, dashboard: { args: ["--console"] } });
  stacks.push(stack);
  seedNotes = await seed(stack);
  // a few files in the Files page's folder (the profile user's home)
  const userHome = path.join(stack.dir, "stack", "user-home");
  fs.mkdirSync(path.join(userHome, "demo-app", "src"), { recursive: true });
  fs.writeFileSync(
    path.join(userHome, "NOTES.md"),
    "# Notes\n\n- The dashboard serves the web UI on 127.0.0.1:9119.\n- Cron jobs live under the profile home.\n- Run `indus doctor` after an upgrade.\n",
  );
  fs.writeFileSync(path.join(userHome, "todo.txt"), "1. Review the nightly build\n2. Answer the pairing request\n");
  fs.writeFileSync(path.join(userHome, "demo-app", "README.md"), "# demo-app\n\nA small example project.\n");
  fs.writeFileSync(path.join(userHome, "demo-app", "src", "main.py"), 'print("hello from demo-app")\n');

  const context = await browser.newContext({ viewport: VIEW, colorScheme: "light" });
  await context.addInitScript((k) => {
    try {
      if (!localStorage.getItem(k)) localStorage.setItem(k, "indus-light");
    } catch {}
  }, THEME_KEY);
  const page = await context.newPage();
  const U = stack.url;

  // Sessions
  await step(page, "sessions", async () => {
    await visit(page, `${U}/sessions`);
    await page.getByRole("list", { name: "Sessions" }).getByRole("listitem").first().waitFor();
    await pair(page, "sessions", "Sessions", "Sessions", "Landing page: stats, full-text search, Chats/Automation/All filter, source filter, three CLI sessions with Resume/Rename/Export/Delete.", 1200);
  });
  await step(page, "session-detail", async () => {
    const row = page.getByRole("list", { name: "Sessions" }).getByRole("listitem").filter({ hasText: /haiku/i }).first();
    await row.locator("button[aria-expanded]").first().click();
    await pair(page, "session-detail", "Session transcript", "Sessions", "One session expanded inline to its transcript.", 1500);
    await row.locator("button[aria-expanded]").first().click();
  });
  await step(page, "session-rename", async () => {
    const row = page.getByRole("list", { name: "Sessions" }).getByRole("listitem").filter({ hasText: /branch/i }).first();
    await row.getByRole("button", { name: "Rename" }).click();
    await page.getByRole("textbox", { name: "Session title" }).waitFor();
    await pair(page, "session-rename", "Rename a session", "Sessions", "Inline title editor on a session row (Save/Cancel).");
    await page.getByRole("button", { name: "Cancel" }).click();
  });
  await step(page, "session-delete", async () => {
    const row = page.getByRole("list", { name: "Sessions" }).getByRole("listitem").filter({ hasText: /repository/i }).first();
    await row.getByRole("button", { name: "Delete" }).click();
    await page.getByRole("dialog", { name: "Delete this session?" }).waitFor();
    await pair(page, "session-delete", "Delete session confirm", "Sessions", "Confirm dialog before deleting a session.");
    await page.keyboard.press("Escape");
  });
  await step(page, "sessions-prune", async () => {
    await page.getByRole("button", { name: /Prune/ }).click();
    await page.getByRole("dialog", { name: "Prune old sessions" }).waitFor();
    await pair(page, "sessions-prune", "Prune old sessions", "Sessions", "Header action: prune dialog.");
    await page.keyboard.press("Escape");
  });
  await step(page, "sessions-search", async () => {
    await page.getByRole("textbox", { name: "Search sessions" }).fill("branch");
    await page.getByRole("button", { name: "Search" }).click();
    await page.getByRole("list", { name: "Search results" }).getByRole("listitem").first().waitFor();
    await pair(page, "sessions-search", "Search messages", "Sessions", "Full-text search across messages with highlighted snippet.", 1000);
  });

  // Shell pieces
  await step(page, "sidebar-collapsed", async () => {
    await visit(page, `${U}/sessions`);
    await page.getByRole("button", { name: "Collapse sidebar" }).click();
    await pair(page, "sidebar-collapsed", "Collapsed sidebar", "Shell", "Sidebar collapsed to its icon rail (wide screens).");
    await page.getByRole("button", { name: "Expand sidebar" }).click();
  });
  await step(page, "empty-state", async () => {
    await visit(page, `${U}/sessions`);
    await page.locator("nav select").first().selectOption({ label: "research" }).catch(async () => {
      await page.locator("nav select").first().selectOption("research");
    });
    await sleep(1500);
    await pair(page, "empty-state", "Other profile, no sessions", "Shell", "Sidebar profile switcher set to the second profile 'research': the profile-scope banner and the Sessions page's empty state.", 1200);
    await page.locator("nav select").first().selectOption({ index: 0 });
    await sleep(800);
  });

  // Chat: the real TUI in xterm over /api/pty
  await step(page, "chat", async () => {
    await visit(page, `${U}/chat`);
    const rows = page.locator('[data-testid="chat-terminal"] .xterm-rows');
    const screen = () => rows.evaluate((r) => Array.from(r.children, (c) => c.textContent ?? "").join("\n"));
    const shows = async (needle: string, timeout = 60_000) => {
      const until = Date.now() + timeout;
      while (Date.now() < until) {
        if ((await screen().catch(() => "")).includes(needle)) return;
        await sleep(250);
      }
      throw new Error(`terminal never showed ${needle}`);
    };
    await shows("Ask anything");
    await pair(page, "chat", "Chat", "Chat", "Chat page: the real TUI in xterm.js over /api/pty; right rail with model, sessions and workspace.", 1000);
    await page.locator('[data-testid="chat-terminal"] .xterm-helper-textarea').focus();
    await page.keyboard.type("What is in this folder?", { delay: 15 });
    await page.keyboard.press("Enter");
    await shows("it is empty apart from");
    await pair(page, "chat-exchange", "Chat exchange", "Chat", "A short exchange with a terminal tool row (ls) and the answer.", 1500);
  });

  // Files
  await step(page, "files", async () => {
    await visit(page, `${U}/files`);
    await page.getByRole("table", { name: "Files" }).waitFor();
    await pair(page, "files", "Files", "Pages", "File browser of the profile user's folder: Up, new folder, upload, table with download/delete.");
    await page.getByRole("table", { name: "Files" }).getByRole("button", { name: "NOTES.md" }).first().click();
    await page.getByRole("dialog").waitFor();
    await pair(page, "files-preview", "File preview", "Pages", "Text preview dialog for NOTES.md with Download/Close.", 1000);
    await page.keyboard.press("Escape");
  });

  // Analytics
  await step(page, "analytics", async () => {
    await visit(page, `${U}/analytics`);
    await pair(page, "analytics", "Analytics", "Pages", "Token analytics (listed only with dashboard.show_token_analytics: true): range, totals, tokens/cost per day.", 1500);
  });

  // Models
  await step(page, "models", async () => {
    await visit(page, `${U}/models`);
    await pair(page, "models", "Models", "Pages", "Main model (fake-model on a custom endpoint) and the provider catalogue.", 1500);
    await scrollMain(page, "Side tasks");
    await pair(page, "models-side-tasks", "Models: side tasks", "Pages", "Lower part of Models: per-side-task provider/model overrides.");
  });

  // Logs
  await step(page, "logs", async () => {
    await visit(page, `${U}/logs`);
    await page.getByRole("group", { name: "File" }).getByRole("button", { name: "gateway" }).click();
    await page.getByRole("heading", { name: "gateway.log" }).waitFor();
    await page.getByRole("list", { name: "Log lines" }).getByRole("listitem").first().waitFor();
    await pair(page, "logs", "Logs", "Pages", "gateway.log tail with file/level/component/lines filters.", 1200);
  });

  // Cron
  await step(page, "cron", async () => {
    await visit(page, `${U}/cron`);
    await page.getByRole("list", { name: "Scheduled jobs" }).waitFor();
    await pair(page, "cron", "Cron", "Pages", "Two scheduled jobs (one paused) with Pause/Resume, Run now, Edit, Delete.");
    await page.getByRole("button", { name: "New job" }).click();
    await page.getByRole("dialog").waitFor();
    await pair(page, "cron-new-job", "New scheduled job", "Pages", "New job dialog: name, prompt, schedule, deliver to.");
    await page.keyboard.press("Escape");
  });

  // Skills
  await step(page, "skills", async () => {
    await visit(page, `${U}/skills`);
    await pair(page, "skills", "Skills", "Pages", "Installed skills view.", 1200);
    await page.getByRole("button", { name: "Toolsets" }).click();
    await pair(page, "skills-toolsets", "Skills: toolsets", "Pages", "Toolsets view (enable/disable toolsets).", 1200);
    await page.getByRole("button", { name: "Hub" }).click();
    await pair(page, "skills-hub", "Skills: hub", "Pages", "Hub search view (no search run: it would query the public hub).");
  });

  // Plugins
  await step(page, "plugins", async () => {
    await visit(page, `${U}/plugins`);
    await pair(page, "plugins", "Plugins", "Pages", "Installed plugins and install-from-git form.", 1200);
  });

  // MCP
  await step(page, "mcp", async () => {
    await visit(page, `${U}/mcp`);
    await page.getByRole("list", { name: "MCP servers" }).waitFor();
    await pair(page, "mcp", "MCP", "Pages", "Two servers: demo-tools (the repo's fake stdio MCP server, on) and docs-search (http, off), and the Add a server form.", 1000);
    const servers = page.getByRole("list", { name: "MCP servers" });
    await servers.getByRole("listitem").filter({ hasText: "demo-tools" }).getByRole("button", { name: "Test" }).click();
    await page.getByRole("list", { name: "Server tools" }).waitFor({ timeout: 30_000 });
    await pair(page, "mcp-test", "MCP: test a server", "Pages", "Test on demo-tools: connected, its tools listed.", 1000);
  });
  await step(page, "error-state", async () => {
    const servers = page.getByRole("list", { name: "MCP servers" });
    await servers.getByRole("listitem").filter({ hasText: "docs-search" }).getByRole("button", { name: "Test" }).click();
    await sleep(6000);
    await pair(page, "error-state", "MCP test failure", "Pages", "Test on docs-search (nothing listens at 127.0.0.1:9): the connection error as the page shows it.", 1000);
  });

  // Channels
  await step(page, "channels", async () => {
    await visit(page, `${U}/channels`);
    await page.getByRole("list", { name: "Messaging platforms" }).waitFor();
    await pair(page, "channels", "Channels", "Pages", "Messaging platforms grid (Telegram, Discord, Slack, ...), none configured.", 1200);
  });

  // Webhooks
  await step(page, "webhooks", async () => {
    await visit(page, `${U}/webhooks`);
    await page.getByRole("list", { name: "Webhook routes" }).waitFor();
    await pair(page, "webhooks", "Webhooks", "Pages", "Receiver switch, one subscribed route (github-pushes), Add a route form.", 1000);
  });

  // Pairing
  await step(page, "pairing", async () => {
    await visit(page, `${U}/pairing`);
    await pair(page, "pairing", "Pairing", "Pages", "Pending requests and approved users (both empty), approve by code.", 1000);
  });

  // Profiles + builder
  await step(page, "profiles", async () => {
    await visit(page, `${U}/profiles`);
    await page.getByRole("list", { name: "Profiles" }).waitFor();
    await pair(page, "profiles", "Profiles", "Pages", "Profile cards (default, research) with Edit/Rename/Export/Delete and import.", 1000);
    await page.getByRole("list", { name: "Profiles" }).getByRole("listitem").filter({ hasText: "research" }).getByRole("button", { name: "Edit" }).click();
    await page.getByRole("dialog").waitFor();
    await pair(page, "profile-edit", "Edit profile", "Pages", "Edit dialog: model, description, persona.", 1000);
    await page.keyboard.press("Escape");
  });
  await step(page, "profile-builder", async () => {
    await visit(page, `${U}/profiles/new`);
    await pair(page, "profile-builder", "New profile wizard", "Pages", "Profile builder, step 1 (Name / Start from).", 1000);
    await page.getByRole("textbox", { name: "Profile name" }).fill("writer");
    await page.getByRole("button", { name: "Next" }).click();
    await sleep(300);
    await page.getByRole("button", { name: "Next" }).click();
    await pair(page, "profile-builder-model", "New profile: model", "Pages", "Profile builder, step 3 (Model).", 800);
    for (let i = 0; i < 3; i++) {
      await page.getByRole("button", { name: "Next" }).click();
      await sleep(300);
    }
    await pair(page, "profile-builder-review", "New profile: review", "Pages", "Profile builder, step 6 (Review) before Create.", 800);
  });

  // Config
  await step(page, "config", async () => {
    await visit(page, `${U}/config`);
    await page.getByRole("spinbutton", { name: "agent.max_turns" }).waitFor();
    await pair(page, "config", "Config", "Pages", "Schema-driven settings form (Settings view).", 1200);
    await page.getByRole("button", { name: "Effective" }).click();
    await page.getByRole("table", { name: "Effective configuration" }).waitFor();
    await pair(page, "config-effective", "Config: effective", "Pages", "Effective configuration table view.", 1000);
  });
  await step(page, "toast", async () => {
    await page.getByRole("button", { name: "Settings" }).click();
    const field = page.getByRole("spinbutton", { name: "agent.max_turns" });
    await field.waitFor();
    await field.fill("60");
    await page.getByRole("button", { name: "Save" }).first().click();
    await sleep(700);
    await pair(page, "toast", "Saved toast", "Shell", "Config saved: the toast notification (agent.max_turns changed to 60).", 100);
  });

  // Keys (env)
  await step(page, "env", async () => {
    await visit(page, `${U}/env`);
    await pair(page, "env", "Keys", "Pages", "Provider keys (.env), all unset: password fields with Save; no real or fake key entered.", 1200);
    await scrollMain(page, "custom");
    await pair(page, "env-custom", "Keys: custom", "Pages", "Lower part: custom variables (the webhook route's generated secret, masked, with Replace/Delete).");
  });

  // System
  await step(page, "system", async () => {
    await visit(page, `${U}/system`);
    await pair(page, "system", "System", "Pages", "This machine, updates, daemon controls, memory provider.", 1500);
    await scrollMain(page, "Credential pool");
    await pair(page, "system-operations", "System: pool and operations", "Pages", "Credential pool, operations (doctor, backup, import), checkpoints.");
    await scrollMain(page, "bottom");
    await pair(page, "system-hooks", "System: shell hooks", "Pages", "Checkpoint prune and shell hooks form.");
    await scrollMain(page, 0);
    await page.getByRole("button", { name: "Stop" }).first().click();
    await page.getByRole("dialog", { name: "Stop the daemon?" }).waitFor();
    await pair(page, "system-stop-confirm", "Stop daemon confirm", "Pages", "Confirm dialog before stopping the gateway daemon.");
    await page.keyboard.press("Escape");
  });
  await step(page, "system-doctor", async () => {
    await scrollMain(page, "Operations");
    await page.getByRole("button", { name: "Run doctor" }).click();
    await sleep(8000);
    await scrollMain(page, "Operations");
    await pair(page, "system-doctor", "System: run doctor", "Pages", "Operations > Run doctor: the action log with the doctor's output.", 1000);
  });

  // Admin console (indus dashboard --console)
  await step(page, "console", async () => {
    await visit(page, `${U}/sessions`);
    await page.getByRole("button", { name: "Admin console" }).click();
    const dialog = page.getByRole("dialog", { name: "Admin console" });
    await dialog.waitFor();
    await sleep(1500);
    await dialog.getByRole("textbox").last().fill("help");
    await dialog.getByRole("button", { name: "Run" }).click();
    await pair(page, "console", "Admin console", "Shell", "Sidebar 'Admin console' (dashboard --console): curated indus commands in a modal, after typing help.", 2500);
    await page.keyboard.press("Escape");
  });

  // Daemon down: status strip and banners
  await step(page, "daemon-stopped", async () => {
    await visit(page, `${U}/system`);
    await page.getByRole("button", { name: "Stop" }).first().click();
    const dialog = page.getByRole("dialog", { name: "Stop the daemon?" });
    await dialog.getByRole("button", { name: /Stop/ }).click();
    await sleep(6000);
    await visit(page, `${U}/sessions`);
    await pair(page, "daemon-stopped", "Daemon stopped", "Shell", "After System > Stop: the sidebar status strip and Sessions with the gateway daemon down.", 3000);
  });

  await context.close();
} finally {
  await browser.close().catch(() => undefined);
  for (const s of stacks.reverse()) await s.close().catch((e) => console.error(`close: ${e}`));
}

const CANON = [
  "login", "sessions", "session-detail", "sessions-search", "chat", "chat-exchange", "analytics", "channels", "config",
  "cron", "docs", "env", "files", "logs", "mcp", "models", "pairing", "plugins", "profile-builder", "profiles", "skills",
  "system", "webhooks", "theme-menu", "language-menu", "empty-state", "error-state",
];
const have = new Set(manifest.map((m) => m.slug));
const missing = CANON.filter((s) => !have.has(s));
fs.writeFileSync(
  path.join(OUT, "manifest.json"),
  `${JSON.stringify(
    {
      app: "Indus web dashboard",
      viewport: "1440x900@1",
      shots: manifest,
      missing,
      missing_reasons: Object.fromEntries(missing.map((s) => [s, MISSING[s] ?? "capture failed (see failures)"])),
      failures,
      seed_notes: seedNotes,
    },
    null,
    1,
  )}\n`,
);
console.log(`pairs ${manifest.length}; missing ${missing.join(",")}; failures ${failures.length}`);
for (const f of failures) console.log(`failure ${f}`);
process.exit(0);
