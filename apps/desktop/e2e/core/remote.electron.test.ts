// P24-29 on the real runtime (D-16.691): the desktop chats with a remote Indus over the WebSocket.
//
// A real `indus serve` in token mode (its own port, the stack's profile home and daemon, only the
// model scripted) stands in for the remote machine. The app's connections registry names it as the
// primary connection with a saved token (the file store: no keychain in this box). The app then:
// dials it (main mints a single-use ticket per dial with the token and offers only that ticket to
// the window), shows it connected, and a prompt streams the scripted reply from the remote daemon.
// The token itself never reaches the window (page text, storage, the bridge's view of it).

import { type ChildProcess, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { cleanupFakes } from "../../testing/fakeIndus.ts";
import {
  cleanEnv,
  freePort,
  launchApp,
  processesWithHome,
  REAL_INDUS,
  type RunningApp,
  realSkip,
  type Stack,
  sendPrompt,
  sleep,
  startStack,
  stopUnder,
  tempDir,
  waitConnected,
} from "../fixtures.ts";

let work = "";
let stack: Stack | null = null;
let serve: ChildProcess | null = null;
let app: RunningApp | null = null;
let url = "";
const token = randomBytes(32).toString("base64url");

function writePrivate(file: string, text: string): void {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  fs.writeFileSync(file, text, { mode: 0o600 });
}

async function serveAnswers(base: string, timeoutMs = 60_000): Promise<void> {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    const status = await fetch(`${base}/api/config`, {
      headers: { "X-Indus-Session-Token": token },
    })
      .then((answer) => answer.status)
      .catch(() => 0);
    if (status === 200) return;
    await sleep(300);
  }
  throw new Error(`indus serve did not answer on ${base}`);
}

beforeAll(async () => {
  if (realSkip !== "") return;
  work = tempDir("indus-e2e-remote-");
  stack = await startStack(work, {
    daemon: true,
    turns: [[{ text: ["Hello from ", "the remote serve."] }]],
  });
  const port = await freePort();
  url = `http://127.0.0.1:${port}`;
  const log = fs.openSync(path.join(work, "serve.log"), "w");
  serve = spawn(REAL_INDUS, ["serve", "--host", "127.0.0.1", "--port", String(port)], {
    cwd: stack.workDir,
    detached: true,
    stdio: ["ignore", log, log],
    env: cleanEnv({
      ...stack.env,
      INDUS_HOME: stack.home,
      INDUS_DASHBOARD_SESSION_TOKEN: token,
    }),
  });
  await serveAnswers(url);
}, 240_000);

afterAll(async () => {
  await app?.stop();
  if (serve?.pid !== undefined && serve.exitCode === null) {
    try {
      process.kill(-serve.pid, "SIGTERM");
    } catch {
      // gone
    }
    await sleep(1_000);
  }
  const result = await stack?.quit();
  if (result !== undefined) expect(result.stray).toEqual([]);
  if (stack !== null) await stopUnder(stack.root);
  if (stack !== null) expect(processesWithHome(stack.home)).toEqual([]);
  cleanupFakes();
});

describe.skipIf(realSkip !== "")(
  `remote chat over a ticketed WebSocket${realSkip ? ` (skipped: ${realSkip})` : ""}`,
  () => {
    it("dials the remote serve with a per-dial ticket and streams its daemon's reply", async () => {
      const current = stack as Stack;
      const userData = path.join(work, "user-data");
      writePrivate(
        path.join(userData, "connections.json"),
        `${JSON.stringify({
          v: 1,
          connections: [
            { id: "local", kind: "local", label: "This computer" },
            { id: "lab", kind: "remote", label: "Lab", url, authMode: "token" },
          ],
          primaryId: "lab",
          launchMode: "primary",
          quarantined: [],
        })}\n`,
      );
      writePrivate(
        path.join(userData, "secrets", "connection-lab-token.json"),
        `${JSON.stringify({ v: 1, store: "file", data: token })}\n`,
      );
      // the app's own home has no Indus: the window must not fall back to this computer
      const ownHome = path.join(work, "own-home");
      fs.mkdirSync(ownHome, { recursive: true });
      app = await launchApp({
        work,
        home: ownHome,
        indus: REAL_INDUS,
        userData,
        env: { ...current.env, HOME: path.join(work, "user-home") },
      });
      await waitConnected(app.cdp, 120_000);
      const mode = await app.cdp.eval<string>(
        `document.querySelector('.connection-badge')?.dataset.mode ?? ''`,
      );
      if (mode !== "") expect(mode).toBe("remote");

      await sendPrompt(app.cdp, "Say hello");
      await app.cdp.waitFor(
        `document.querySelector('.transcript')?.innerText.includes('Hello from the remote serve.')`,
        "the remote daemon's reply",
        60_000,
      );
      const asked = (await current.sql<[string]>(
        "SELECT coalesce(content, '') FROM messages WHERE role = 'user' ORDER BY id",
      )) as Array<[string]>;
      expect(asked.map(([text]) => text)).toContain("Say hello");

      // the token stays in main: nothing the window holds or shows carries it
      const leaked = await app.cdp.eval<boolean>(
        `(() => {
          const t = ${JSON.stringify(token)};
          const stores = [localStorage, sessionStorage];
          const inStorage = stores.some((s) => {
            for (let i = 0; i < s.length; i += 1) {
              const key = s.key(i) ?? "";
              if (key.includes(t) || (s.getItem(key) ?? "").includes(t)) return true;
            }
            return false;
          });
          return inStorage || document.documentElement.outerHTML.includes(t);
        })()`,
      );
      expect(leaked).toBe(false);
      expect(fs.readFileSync(path.join(work, "serve.log"), "utf8")).not.toContain(token);
    }, 240_000);
  },
);
