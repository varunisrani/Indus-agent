// TUI screenshots: the real Ink TUI (`indus --tui`, apps/tui/dist) in a real pseudo-terminal
// (node-pty from the workspace), rendered by xterm.js (the workspace's @xterm/xterm) in headless
// Chromium. The profile is a web_stack home with the scripted fake model and a real daemon (no
// dashboard). Keys typed into xterm go back to the PTY, as in any terminal.
//
//   WT=<worktree> OUT=<png dir> PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node tui.ts

import { type ChildProcess, spawn } from "node:child_process";
import fs from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";

const WT = process.env.WT ?? "";
const OUT = process.env.OUT ?? "";
if (WT === "" || OUT === "") throw new Error("set WT and OUT");
fs.mkdirSync(OUT, { recursive: true });
if (!fs.existsSync(path.join(WT, "apps/tui/dist/entry.js"))) throw new Error("build apps/tui first");

const webRequire = createRequire(path.join(WT, "apps/web/package.json"));
const tuiRequire = createRequire(path.join(WT, "apps/tui/package.json"));
const { chromium } = webRequire("@playwright/test") as typeof import("@playwright/test");
const pty = tuiRequire("node-pty") as typeof import("node-pty");
const xtermDir = path.dirname(path.dirname(webRequire.resolve("@xterm/xterm")));
const fitJs = webRequire.resolve("@xterm/addon-fit");

const PYTHON = path.join(WT, ".venv/bin/python");
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// -- the profile: scripted model + daemon (tests/fakes/web_stack.py, no dashboard) ----------------
// a fixed, short folder so the paths on screen read well (removed afterwards)
const dir = path.join(os.tmpdir(), "indus-shots", "tui");
fs.rmSync(dir, { recursive: true, force: true });
fs.mkdirSync(dir, { recursive: true });
const root = path.join(dir, "demo");
const NOTES = "# Release notes\n\n- Faster start-up\n- New `--shout` flag\n";
fs.writeFileSync(
  path.join(os.tmpdir(), "indus-shots", "tui-spec.json"),
  JSON.stringify({
    root,
    daemon: true,
    dashboard: null,
    config_extra: "",
    basic_auth: null,
    turns: [
      [
        {
          text: [
            "Here is the plan:\n\n",
            "1. **Read** the open issues tagged `release`.\n",
            "2. **Group** them into features and fixes.\n",
            "3. **Draft** the notes in `NOTES.md` for your review.\n\n",
            "Shall I start?",
          ],
        },
      ],
      [
        { text: ["Drafting ", "the notes now. "] },
        {
          tool_call: {
            name: "write_file",
            arguments: { path: "NOTES.md", content: NOTES },
            id: "call_w",
          },
        },
        { finish: "tool_calls" },
      ],
      [
        { text: ["Cleaning up ", "the scratch files. "] },
        { tool_call: { name: "terminal", arguments: { command: "find scratch -delete" }, id: "call_t" } },
        { finish: "tool_calls" },
      ],
      [{ text: ["Done. `NOTES.md` is ready ", "and the scratch folder is gone."] }],
    ],
  }),
);
const stackProc: ChildProcess = spawn(PYTHON, ["-m", "tests.fakes.web_stack", path.join(os.tmpdir(), "indus-shots", "tui-spec.json")], {
  cwd: WT,
  env: { ...process.env, PYTHONPATH: WT },
  stdio: ["pipe", "pipe", "pipe"],
});
let stackErr = "";
stackProc.stderr?.on("data", (c: Buffer) => {
  stackErr = (stackErr + c.toString()).slice(-4000);
});
const stackLines = createInterface({ input: stackProc.stdout as NodeJS.ReadableStream });
const readyLine = await new Promise<string | null>((r) => {
  stackLines.once("line", r);
  stackLines.once("close", () => r(null));
});
const ready = readyLine ? JSON.parse(readyLine) : null;
let child: import("node-pty").IPty | null = null;
const browser = await chromium.launch({ headless: true });

async function stopStack() {
  if (stackProc.exitCode === null) {
    stackProc.stdin?.write(`${JSON.stringify({ op: "quit" })}\n`);
    await new Promise<void>((r) => {
      const t = setTimeout(() => {
        stackProc.kill("SIGTERM");
        r();
      }, 60_000);
      stackProc.once("exit", () => {
        clearTimeout(t);
        r();
      });
    });
  }
}

try {
  if (!ready?.ready) throw new Error(`stack did not start: ${readyLine}\n${stackErr}`);
  const work = String(ready.work);
  fs.mkdirSync(path.join(work, "scratch"));
  fs.writeFileSync(path.join(work, "scratch", "draft.tmp"), "x\n");

  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.setContent(`<!doctype html><html><head><style>
    html,body{margin:0;height:100%;background:#16181d}
    #t{position:absolute;inset:14px 16px}
  </style></head><body><div id="t"></div></body></html>`);
  await page.addStyleTag({ path: path.join(xtermDir, "css/xterm.css") });
  await page.addScriptTag({ path: path.join(xtermDir, "lib/xterm.js") });
  await page.addScriptTag({ path: fitJs });
  const dims = (await page.evaluate(() => {
    const w = window as unknown as Record<string, any>;
    const term = new w.Terminal({
      fontFamily: "'DejaVu Sans Mono', monospace",
      fontSize: 15,
      lineHeight: 1.15,
      cursorBlink: false,
      allowProposedApi: true,
      theme: {
        background: "#16181d",
        foreground: "#d8dee9",
        cursor: "#d8dee9",
        black: "#20242c", red: "#e06c75", green: "#98c379", yellow: "#e5c07b",
        blue: "#61afef", magenta: "#c678dd", cyan: "#56b6c2", white: "#d8dee9",
        brightBlack: "#5c6370", brightRed: "#ef7a85", brightGreen: "#a9d48a", brightYellow: "#f0cf8c",
        brightBlue: "#7cc0f5", brightMagenta: "#d48ee6", brightCyan: "#6fcad6", brightWhite: "#ffffff",
      },
    });
    const fit = new w.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(document.getElementById("t"));
    fit.fit();
    w.term = term;
    term.onData((d: string) => w.toPty(d));
    term.focus();
    return { cols: term.cols, rows: term.rows };
  })) as { cols: number; rows: number };
  console.log(`terminal ${dims.cols}x${dims.rows}`);

  // the PTY: what `indus --tui` runs, in the profile's environment (tests/fakes/daemon.py ProfileHome.env)
  const env: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) if (v !== undefined && !k.startsWith("INDUS_")) env[k] = v;
  Object.assign(env, {
    INDUS_HOME: String(ready.home),
    HOME: path.join(root, "user-home"),
    XDG_CONFIG_HOME: path.join(root, "config"),
    INDUS_GATEWAY_LOCK_DIR: path.join(root, "token-locks"),
    NO_PROXY: "127.0.0.1,localhost",
    no_proxy: "127.0.0.1,localhost",
    PYTHONPATH: WT,
    PATH: `${path.dirname(PYTHON)}:${path.dirname(process.execPath)}:/usr/bin:/bin`,
    TERM: "xterm-256color",
    COLORTERM: "truecolor",
  });
  let queue = Promise.resolve();
  await page.exposeFunction("toPty", (d: string) => child?.write(d));
  const launch = async (args: string[]) => {
    if (child !== null) {
      const old = child;
      child = null;
      await new Promise<void>((r) => {
        const t = setTimeout(r, 10_000);
        old.onExit(() => {
          clearTimeout(t);
          r();
        });
        old.kill("SIGTERM");
      });
      await queue;
    }
    await page.evaluate(() => (window as any).term.reset());
    const mine = pty.spawn(PYTHON, ["-m", "indus", "--tui", ...args], {
      name: "xterm-256color",
      cols: dims.cols,
      rows: dims.rows,
      cwd: work,
      env,
    });
    child = mine;
    mine.onData((d) => {
      if (child !== mine) return;
      queue = queue.then(() => page.evaluate((s) => (window as any).term.write(s), d)).catch(() => undefined);
    });
  };

  const screen = () =>
    page.evaluate(() => {
      const b = (window as any).term.buffer.active;
      const out: string[] = [];
      for (let i = 0; i < b.length; i++) out.push(b.getLine(i)?.translateToString(true) ?? "");
      return out.join("\n");
    });
  const shows = async (needle: string | RegExp, timeout = 60_000) => {
    const until = Date.now() + timeout;
    for (;;) {
      const s = await screen();
      if (typeof needle === "string" ? s.includes(needle) : needle.test(s)) return;
      if (Date.now() > until) throw new Error(`terminal never showed ${needle}:\n${s.slice(-3000)}`);
      await sleep(200);
    }
  };
  const gone = async (needle: string, timeout = 20_000) => {
    const until = Date.now() + timeout;
    while ((await screen()).includes(needle)) {
      if (Date.now() > until) throw new Error(`terminal still shows ${needle}`);
      await sleep(200);
    }
  };
  const shot = async (name: string, settle = 900) => {
    await sleep(settle);
    await queue;
    await page.screenshot({ path: path.join(OUT, name) });
    console.log(`shot ${name}`);
  };
  const type = async (text: string) => {
    await page.keyboard.type(text, { delay: 20 });
  };

  const isReady = /─ ready/;

  // the conversation: attached to the profile's daemon (the default)
  await launch([]);
  await shows("Ask anything");
  await shows(isReady);
  await shot("tui-01-welcome.png", 1500);

  await type("Plan the release notes for 0.2.0");
  await page.keyboard.press("Enter");
  await shows("Shall I start?");
  await shot("tui-02-reply.png", 1200);

  await type("Yes, draft them");
  await page.keyboard.press("Enter");
  // the write may or may not ask for approval; answer it if it does
  const until = Date.now() + 30_000;
  for (;;) {
    const s = await screen();
    if (s.includes("Cleaning up the scratch files")) break;
    if (s.includes("Needs your approval")) {
      await page.keyboard.press("Enter");
      await gone("Needs your approval");
    }
    if (Date.now() > until) break;
    await sleep(300);
  }
  await shows("Needs your approval");
  await shows("find scratch -delete");
  await shot("tui-03-approval.png", 1200);
  await page.keyboard.press("Enter");
  await shows("the scratch folder is gone");
  await shows(isReady);
  await shot("tui-04-exchange.png", 1500);

  // slash menu, model picker and sessions, still attached to the daemon (default mode)
  await type("/");
  await shows(/\/[a-z]+ +\S/, 15_000).catch(() => console.log("note: no slash entries listed"));
  await shot("tui-06-slash-menu.png", 1200);
  await page.keyboard.press("Backspace");
  await sleep(600);
  await page.keyboard.press("Control+o");
  await shows("Choose a provider", 15_000).catch(() => console.log("note: no provider picker"));
  await shot("tui-07-model-picker.png", 1000);
  await page.keyboard.press("Escape");
  await sleep(800);
  await page.keyboard.press("Control+x");
  await shows("Sessions", 15_000).catch(() => console.log("note: no sessions picker"));
  await shot("tui-08-sessions.png", 1500);
  await page.keyboard.press("Escape");
} finally {
  try {
    child?.kill();
  } catch {
    // gone
  }
  await browser.close().catch(() => undefined);
  await stopStack();
  fs.rmSync(path.join(os.tmpdir(), "indus-shots"), { recursive: true, force: true });
}
console.log("tui done");
process.exit(0);
