// The wired app (wave-34 integration of P20-08..24, ahead of P20-40's full suite): the built
// renderer and main process in a real Electron window, driven over the DevTools protocol.
//
//   fake backend   boot → connect (the desktop announces itself as a `gui` viewer) → one prompt →
//                  a streamed reply → one approval card answered: exactly one answer on the wire;
//   real runtime   the same against a real `indus` daemon and the scripted fake model
//                  (`tests/fakes/web_stack.py`), whose next turn answers after the approval.
//
// The app runs from a copy outside the checkout, so the resolver uses the `indus` named by
// INDUS_DESKTOP_INDUS (the dev-checkout rung would pick this checkout's own runtime). Skips with
// the reason without an Electron binary or a display; the real case also without `.venv`.

import { type ChildProcess, spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";
import { build as viteBuild } from "vite";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
// @ts-expect-error -- plain JS module without types
import { buildElectron } from "../../scripts/build-electron.mjs";
// @ts-expect-error -- plain JS module without types
import { controlQuery } from "../fakeBackend/control.mjs";
import { cleanupFakes, tempDir, writeFakeIndus } from "../fakeIndus.ts";
import { displayWrapper, electronBinary, rootSandboxArgs } from "./binary.ts";
import { appPage, Cdp, hasButton, TRANSCRIPT_TEXT } from "./cdp.ts";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP_ROOT = path.resolve(HERE, "..", "..");
const REPO = path.resolve(APP_ROOT, "..", "..");
const PYTHON = path.join(REPO, ".venv", "bin", "python");
const electron = electronBinary();
const display = displayWrapper();
const skipReason =
  process.platform === "win32"
    ? "POSIX only (the fake daemon's control socket)"
    : electron.path === null
      ? electron.reason
      : display === null
        ? "no display and no xvfb-run"
        : "";
const realSkip =
  skipReason !== ""
    ? skipReason
    : fs.existsSync(PYTHON) && fs.existsSync(path.join(REPO, "tests", "fakes", "web_stack.py"))
      ? ""
      : "no .venv in this checkout";

const SESSION = "s-fake-1";
const SCENARIO = {
  description: "The wired-app test: one chat turn and one approval through the fake daemon.",
  version: "0.20.0",
  rpc: {
    readyOnConnect: true,
    methods: {
      "client.capabilities": { result: { features: ["request.decline"] } },
      "session.create": { result: { session_id: SESSION } },
      "session.history": { result: { messages: [] } },
      "session.subscribe": { result: { session_id: SESSION, replay_epoch: "e1", last_seq: 0 } },
      "prompt.submit": { result: { turn_id: "t-1", generation: 1, status: "started" } },
      "config.get": { result: { key: "x", value: null } },
      "commands.catalog": { result: { commands: [] } },
    },
  },
  rest: { routes: { "GET /api/sessions": { status: 200, body: { sessions: [] } } } },
};

let work = "";
let appDir = "";

function freePort(): Promise<number> {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as net.AddressInfo;
      server.close(() => resolve(port));
    });
  });
}

interface RunningApp {
  child: ChildProcess;
  cdp: Cdp;
  stop(): Promise<void>;
}

async function launch(env: Record<string, string>): Promise<RunningApp> {
  const port = await freePort();
  const [command, ...args] = [
    ...(display?.command ?? []),
    electron.path as string,
    ...rootSandboxArgs(),
    "--disable-gpu",
    `--remote-debugging-port=${port}`,
    appDir,
  ];
  const child = spawn(command as string, args, {
    cwd: work,
    detached: true,
    stdio: "ignore",
    env: {
      ...process.env,
      INDUS_DESKTOP_USER_DATA_DIR: path.join(work, `user-data-${port}`),
      INDUS_DESKTOP_SKIP_QUIT_PROMPT: "1",
      ...env,
    },
  });
  const stop = async () => {
    if (child.exitCode !== null || child.pid === undefined) return;
    const gone = new Promise<void>((resolve) => child.once("exit", () => resolve()));
    try {
      process.kill(-child.pid, "SIGTERM"); // the group this test started (xvfb-run, Electron)
    } catch {
      return;
    }
    await Promise.race([gone, new Promise((resolve) => setTimeout(resolve, 5_000))]);
  };
  try {
    const cdp = await Cdp.open(await appPage(port));
    await cdp.send("Runtime.enable");
    return { child, cdp, stop };
  } catch (error) {
    await stop();
    throw error;
  }
}

/** Boot, connect and send one prompt; resolves once the person's own row is on screen. */
async function sendPrompt(cdp: Cdp, prompt: string): Promise<void> {
  await cdp.waitFor(
    `document.querySelector('.connection-badge')?.dataset.phase === 'open'`,
    "the connection",
    60_000,
  );
  await cdp.waitFor(`!!document.querySelector('textarea')`, "the composer");
  await cdp.type("textarea", prompt);
  await cdp.pressEnter();
  await cdp.waitFor(`location.hash.length > 2`, "the new session's route", 20_000);
  await cdp.waitFor(`${TRANSCRIPT_TEXT}.includes(${JSON.stringify(prompt)})`, "the prompt's row");
}

/** The first message of a fresh chat is never offered back as "not confirmed" while it runs. */
async function expectNoUncertainStrip(cdp: Cdp): Promise<void> {
  expect(await cdp.eval<boolean>(`document.body.innerText.includes("Not confirmed")`)).toBe(false);
}

async function answerApproval(cdp: Cdp): Promise<void> {
  await cdp.waitFor(hasButton("Allow once"), "the approval card", 60_000);
  await expectNoUncertainStrip(cdp);
  await new Promise((resolve) => setTimeout(resolve, 400)); // the card's arming delay
  await cdp.click("Allow once");
  await cdp.waitFor(`!(${hasButton("Allow once")})`, "the card to close", 20_000);
}

beforeAll(async () => {
  if (skipReason !== "") return;
  work = tempDir("indus-wired-");
  appDir = path.join(work, "app");
  fs.mkdirSync(appDir);
  fs.copyFileSync(path.join(APP_ROOT, "package.json"), path.join(appDir, "package.json"));
  await viteBuild({
    configFile: path.join(APP_ROOT, "vite.config.ts"),
    root: APP_ROOT,
    logLevel: "silent",
    build: { outDir: path.join(appDir, "dist"), emptyOutDir: true },
  });
  await (buildElectron as (dir: string) => Promise<string>)(path.join(appDir, "dist-electron"));
}, 180_000);

afterAll(() => {
  cleanupFakes();
});

describe.skipIf(skipReason !== "")(
  `the wired app${skipReason ? ` (skipped: ${skipReason})` : ""}`,
  () => {
    it("fake backend: boot, connect, prompt, streamed reply, one approval answered once", async () => {
      const home = path.join(work, "home");
      fs.mkdirSync(home, { mode: 0o700 });
      const scenario = path.join(work, "scenario.json");
      fs.writeFileSync(scenario, JSON.stringify(SCENARIO));
      const indus = writeFakeIndus(path.join(work, "bin", "indus"), scenario);
      const app = await launch({ INDUS_HOME: home, INDUS_DESKTOP_INDUS: indus });
      try {
        const { cdp } = app;
        await sendPrompt(cdp, "Hello fake");
        const emit = (event: Record<string, unknown>) =>
          controlQuery(home, "fake-emit", { event: { session_id: SESSION, ...event } });
        const received = async () =>
          (
            (await controlQuery(home, "fake-received", {})) as {
              result: { frames: Array<{ method?: string; params?: Record<string, unknown> }> };
            }
          ).result.frames;

        const announce = (await received()).find((frame) => frame.method === "client.capabilities");
        expect(announce?.params).toMatchObject({ surface: "gui" });

        await emit({ type: "turn.started", turn_id: "t-1", generation: 1 });
        await emit({ type: "message.delta", turn_id: "t-1", text: "Hello " });
        await emit({ type: "message.delta", turn_id: "t-1", text: "from the fake daemon." });
        await cdp.waitFor(
          `${TRANSCRIPT_TEXT}.includes('Hello from the fake daemon.')`,
          "the streamed reply",
        );
        await emit({
          type: "server_request",
          kind: "approval",
          request_id: "r-1",
          turn_id: "t-1",
          command: "find victim -delete",
          description: "Deletes files",
          choices: ["once", "session", "deny"],
        });
        await answerApproval(cdp);
        const answers = (await received()).filter(
          (frame) => frame.method === "server_request.answer",
        );
        expect(answers.map((frame) => frame.params)).toEqual([
          {
            session_id: SESSION,
            request_id: "r-1",
            answer: { kind: "approval", choice: "once" },
          },
        ]);
        cdp.close();
      } finally {
        await app.stop();
      }
    }, 120_000);

    it.skipIf(realSkip !== "")(
      `real runtime: the same against a real daemon and the scripted model${realSkip ? ` (skipped: ${realSkip})` : ""}`,
      async () => {
        const spec = path.join(work, "stack.json");
        fs.writeFileSync(
          spec,
          JSON.stringify({
            root: path.join(work, "stack"),
            turns: [
              [
                { text: ["Streaming ", "from the daemon. "] },
                {
                  tool_call: {
                    name: "terminal",
                    arguments: { command: "find victim -delete" },
                    id: "call_1",
                  },
                },
                { finish: "tool_calls" },
              ],
              [{ text: ["Cleaned ", "up."] }],
            ],
            daemon: true,
            dashboard: null,
            config_extra: "",
            basic_auth: null,
          }),
        );
        const stack = spawn(PYTHON, ["-m", "tests.fakes.web_stack", spec], {
          cwd: REPO,
          env: { ...process.env, PYTHONPATH: REPO },
          stdio: ["pipe", "pipe", "pipe"],
        });
        let stderr = "";
        stack.stderr?.on("data", (chunk: Buffer) => {
          stderr = (stderr + chunk.toString("utf8")).slice(-4000);
        });
        const lines = readline.createInterface({ input: stack.stdout as NodeJS.ReadableStream });
        const queue: string[] = [];
        const waiting: Array<(line: string | null) => void> = [];
        lines.on("line", (line) => (waiting.shift() ?? ((l: string) => queue.push(l)))(line));
        lines.on("close", () => {
          for (const waiter of waiting.splice(0)) waiter(null);
        });
        const next = () =>
          queue.length > 0
            ? Promise.resolve(queue.shift() ?? null)
            : new Promise<string | null>((resolve) => waiting.push(resolve));
        const ready = JSON.parse((await next()) ?? "{}") as { ready?: boolean; home?: string };
        expect(ready.ready, stderr).toBe(true);
        let app: RunningApp | null = null;
        try {
          app = await launch({
            INDUS_HOME: String(ready.home),
            INDUS_DESKTOP_INDUS: path.join(REPO, ".venv", "bin", "indus"),
          });
          const { cdp } = app;
          await sendPrompt(cdp, "Please clean up");
          await cdp.waitFor(
            `${TRANSCRIPT_TEXT}.includes('Streaming from the daemon.')`,
            "the streamed reply",
            60_000,
          );
          await answerApproval(cdp);
          await cdp.waitFor(
            `${TRANSCRIPT_TEXT}.includes('Cleaned up.')`,
            "the reply after the approval",
            60_000,
          );
          cdp.close();
        } finally {
          await app?.stop();
          stack.stdin?.write(`${JSON.stringify({ op: "quit" })}\n`);
          let stray: unknown = null;
          for (;;) {
            const line = await next();
            if (line === null) break;
            const reply = JSON.parse(line) as { stopped?: boolean; stray?: unknown };
            if (reply.stopped) {
              stray = reply.stray;
              break;
            }
          }
          expect(stray).toEqual([]);
        }
      },
      180_000,
    );
  },
);
