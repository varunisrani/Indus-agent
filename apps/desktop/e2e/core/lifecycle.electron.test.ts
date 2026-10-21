// P20-40 lifecycle specs on the real runtime, with no daemon running first: the app starts one
// through `indus gateway ensure` (as on a person's computer), supervises it, and recovers after
// the daemon is killed (`kill -9`): one "stopped working in the background" notice, the
// conversation back without a duplicate, the next prompt answered. Quitting stops the app's own
// processes (the daemon is shared and outlives the app by design, R08.24); the same home is
// launched three more times and attaches each time; at the end the stack stops everything and no
// process naming the home is left.

import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { cleanupFakes } from "../../testing/fakeIndus.ts";
import {
  checkFrames,
  checkTranscriptOnce,
  persistedMessages,
  type ShownMessage,
} from "../../testing/oracle.ts";
import {
  launchApp,
  processesWithHome,
  REAL_INDUS,
  type RunningApp,
  realSkip,
  type Stack,
  sendPrompt,
  shownMessages,
  sleep,
  startStack,
  stopUnder,
  tempDir,
  waitConnected,
} from "../fixtures.ts";

let work = "";
let stack: Stack | null = null;
let app: RunningApp | null = null;
let userData = "";
const RESTARTED = "Indus stopped working in the background and was restarted.";

function daemonPids(home: string): number[] {
  return processesWithHome(home)
    .filter((p) => /\bgateway\b/.test(p.argv) && /\brun\b/.test(p.argv))
    .map((p) => p.pid);
}

async function sessionShownOnce(running: RunningApp): Promise<string[]> {
  const sessionId = decodeURIComponent(
    (await running.cdp.eval<string>(`location.hash`)).replace(/^#\//, ""),
  );
  const rows = (await stack?.sql<[string, string]>(
    `SELECT role, coalesce(content, '') FROM messages WHERE session_id = '${sessionId.replaceAll("'", "''")}' AND coalesce(active, 1) = 1 ORDER BY id`,
  )) as Array<[string, string]>;
  const persisted = persistedMessages(rows.map(([role, text]) => ({ role: role as "user", text })));
  return checkTranscriptOnce(persisted, await shownMessages(running.cdp));
}

function launch(): Promise<RunningApp> {
  const current = stack as Stack;
  return launchApp({
    work,
    home: current.home,
    indus: REAL_INDUS,
    userData,
    env: { ...current.env, HOME: path.join(work, "user-home") },
  });
}

beforeAll(async () => {
  if (realSkip !== "") return;
  work = tempDir("indus-e2e-life-");
  userData = path.join(work, "user-data");
  stack = await startStack(work, {
    daemon: false,
    turns: [[{ text: ["First ", "answer."] }], [{ text: ["Second ", "answer."] }]],
  });
}, 240_000);

afterAll(async () => {
  await app?.stop();
  const result = await stack?.quit();
  if (result !== undefined) expect(result.stray).toEqual([]);
  if (stack !== null) expect(processesWithHome(stack.home)).toEqual([]);
  if (stack !== null) expect(await stopUnder(stack.root)).toEqual([]);
  cleanupFakes();
});

describe.skipIf(realSkip !== "")(
  `lifecycle on the real runtime${realSkip ? ` (skipped: ${realSkip})` : ""}`,
  () => {
    it("starts its own daemon on a home with none, and chats", async () => {
      const current = stack as Stack;
      expect(daemonPids(current.home)).toEqual([]);
      app = await launch();
      await waitConnected(app.cdp, 120_000);
      expect(daemonPids(current.home).length).toBe(1);
      await sendPrompt(app.cdp, "Hello there");
      await app.cdp.waitFor(
        `document.querySelector('.transcript')?.innerText.includes('First answer.')`,
        "the first reply",
        60_000,
      );
    }, 180_000);

    it("kill -9 of the daemon: one notice, recovered without duplicates, next prompt answered", async () => {
      const running = app as RunningApp;
      const current = stack as Stack;
      const [pid] = daemonPids(current.home);
      expect(pid).toBeDefined();
      // every frame from the kill until the recovered conversation: nothing shown twice, ever
      const frames: ShownMessage[][] = [];
      let sampling = true;
      const sampler = (async () => {
        while (sampling) {
          frames.push(await shownMessages(running.cdp).catch(() => []));
          await sleep(40);
        }
      })();
      process.kill(pid as number, "SIGKILL");
      await running.cdp.waitFor(
        `document.body.innerText.includes(${JSON.stringify(RESTARTED)})`,
        "the restart notice",
        120_000,
      );
      const count = `document.body.innerText.split(${JSON.stringify(RESTARTED)}).length - 1`;
      expect(await running.cdp.eval<number>(count)).toBe(1);
      await waitConnected(running.cdp, 120_000);
      // one daemon: the supervisor's respawn and the reconnect's ticket mint share one ensure
      const fresh = daemonPids(current.home);
      expect(fresh.length).toBe(1);
      expect(fresh[0]).not.toBe(pid);
      await running.cdp.waitFor(
        `document.querySelector('.transcript')?.innerText.includes('First answer.')`,
        "the conversation after the restart",
        30_000,
      );
      await sleep(1_000);
      sampling = false;
      await sampler;
      expect(frames.length).toBeGreaterThan(10);
      expect(checkFrames(frames)).toEqual([]);
      expect(await sessionShownOnce(running)).toEqual([]);
      await running.cdp.type("textarea", "Still there?");
      await running.cdp.pressEnter();
      await running.cdp.waitFor(
        `document.querySelector('.transcript')?.innerText.includes('Second answer.')`,
        "the reply after the restart",
        60_000,
      );
      await sleep(500);
      expect(await sessionShownOnce(running)).toEqual([]);
      // never a second notice for the one restart (the toast closes by itself after 15 s)
      expect(await running.cdp.eval<number>(count)).toBeLessThanOrEqual(1);
    }, 300_000);

    it("quit stops the app's own processes; the shared daemon keeps running", async () => {
      const running = app as RunningApp;
      const current = stack as Stack;
      expect(await running.stop()).toBe(true);
      app = null;
      await sleep(1_000);
      const left = processesWithHome(current.home);
      const notDaemon = left.filter((p) => !daemonPids(current.home).includes(p.pid));
      expect(notDaemon.filter((p) => !/\bserve\b/.test(p.argv))).toEqual([]);
      expect(daemonPids(current.home).length).toBe(1);
    }, 60_000);

    it("relaunching the same home three times attaches every time", async () => {
      const current = stack as Stack;
      for (let round = 0; round < 3; round += 1) {
        const again = await launch();
        try {
          await waitConnected(again.cdp, 120_000);
          await again.cdp.waitFor(
            `document.body.innerText.includes('Hello there')`,
            "the earlier chat in the sidebar",
            30_000,
          );
          expect(daemonPids(current.home).length).toBe(1);
        } finally {
          expect(await again.stop()).toBe(true);
        }
      }
    }, 400_000);
  },
);
