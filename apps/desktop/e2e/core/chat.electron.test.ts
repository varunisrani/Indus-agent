// P20-40 chat specs on the real runtime (Demo 4's core): a real `indus` daemon and serve in a temp
// profile home, only the model scripted (`tests/fakes/web_stack.py`). In order:
//
//   onboarding     the overlay covers the chat until a provider is set up: a custom
//                  OpenAI-compatible endpoint, tested by the backend's real
//                  `/api/model/test-endpoint`, then "Start chatting";
//   deep link      a second launch hands `indus-dev://open/settings?view=workspace&page=desktop`
//                  over; Settings opens there and the default folder for new chats is set;
//   chat           a new chat in that folder: streamed reply, a tool row (the agent writes a file),
//                  an approval card answered once, the reply after it;
//   panes          git review lists the agent's edit, files shows it, the preview shows a local
//                  page the test serves (as if the agent had started it);
//   oracle         after a reload every persisted message is shown exactly once and in order;
//   sidebar        a second chat, then the first again from the sidebar.

import fs from "node:fs";
import http from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { cleanupFakes } from "../../testing/fakeIndus.ts";
import { checkTranscriptOnce, persistedMessages } from "../../testing/oracle.ts";
import {
  bridge,
  clickAt,
  clickButton,
  clickButtonStarting,
  DEV_SCHEME,
  fillField,
  gitRepo,
  hasButton,
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
let repo = "";
let stack: Stack | null = null;
let app: RunningApp | null = null;
let site: http.Server | null = null;
let siteUrl = "";

/** Answer an approval card if one comes up (the scripted tools may or may not need one). */
async function answerIfAsked(app: RunningApp, waitMs = 3_000): Promise<boolean> {
  const until = Date.now() + waitMs;
  while (Date.now() < until) {
    if (await app.cdp.eval<boolean>(hasButton("Run"))) {
      await sleep(400); // the card's arming delay
      await app.cdp.click("Run");
      await app.cdp.waitFor(`!(${hasButton("Run")})`, "the card to close", 20_000);
      return true;
    }
    await sleep(200);
  }
  return false;
}

async function showPane(app: RunningApp, id: string, title: string): Promise<void> {
  const opened = await app.cdp.eval<boolean>(`!!document.getElementById('pane-tab-${id}')`);
  if (!opened) {
    // the window starts on the chat alone (W51-b): Files and Review have their own band toggle,
    // the other panes an "Open …" button
    const open = (await app.cdp.eval<boolean>(hasButton(`Open ${title}`)))
      ? `Open ${title}`
      : title;
    await clickButton(app.cdp, open);
  }
  await clickAt(app.cdp, `#pane-tab-${id}`);
}

async function storedMessages(): Promise<Array<{ role: "user" | "assistant"; text: string }>> {
  const rows = (await stack?.sql<[string, string, string]>(
    "SELECT session_id, role, coalesce(content, '') FROM messages WHERE coalesce(active, 1) = 1 ORDER BY id",
  )) as Array<[string, string, string]>;
  return rows.map(([, role, text]) => ({ role: role as "user" | "assistant", text }));
}

async function sessionRows(sessionId: string) {
  const rows = (await stack?.sql<[string, string]>(
    `SELECT role, coalesce(content, '') FROM messages WHERE session_id = '${sessionId.replaceAll("'", "''")}' AND coalesce(active, 1) = 1 ORDER BY id`,
  )) as Array<[string, string]>;
  return persistedMessages(rows.map(([role, text]) => ({ role: role as "user", text })));
}

beforeAll(async () => {
  if (realSkip !== "") return;
  work = tempDir("indus-e2e-chat-");
  repo = gitRepo(path.join(work, "repo"));
  site = http.createServer((_request, response) => {
    response
      .writeHead(200, { "content-type": "text/html" })
      .end(
        "<!doctype html><html><head><title>Demo page</title></head><body><h1>Served by the agent</h1></body></html>",
      );
  });
  await new Promise<void>((resolve) => site?.listen(0, "127.0.0.1", resolve));
  siteUrl = `http://127.0.0.1:${(site.address() as AddressInfo).port}/`;
  stack = await startStack(work, {
    daemon: true,
    turns: [
      [
        { text: ["Writing ", "the notes. "] },
        {
          tool_call: {
            name: "write_file",
            arguments: { path: path.join(repo, "notes.md"), content: "hello from the agent\n" },
            id: "call_w",
          },
        },
        { finish: "tool_calls" },
      ],
      [{ text: ["The notes ", "are written."] }],
      [
        { text: ["Cleaning ", "up. "] },
        {
          tool_call: { name: "terminal", arguments: { command: "find victim -delete" }, id: "c_t" },
        },
        { finish: "tool_calls" },
      ],
      [{ text: ["Cleaned ", "up."] }],
      [{ text: ["Hello ", "again."] }],
    ],
  });
}, 240_000);

afterAll(async () => {
  await app?.stop();
  site?.close();
  const result = await stack?.quit();
  if (result !== undefined) expect(result.stray).toEqual([]);
  // the "work" profile made by the rail spec runs its own daemon and serve under the stack's home
  if (stack !== null) await stopUnder(stack.root);
  if (stack !== null) expect(processesWithHome(stack.home)).toEqual([]);
  cleanupFakes();
});

describe.skipIf(realSkip !== "")(
  `Demo 4 core on the real runtime${realSkip ? ` (skipped: ${realSkip})` : ""}`,
  () => {
    it("boots and attaches; a chosen custom endpoint needs no onboarding overlay", async () => {
      const current = stack as Stack;
      app = await launchApp({
        work,
        home: current.home,
        indus: REAL_INDUS,
        env: { ...current.env, HOME: path.join(work, "user-home") },
      });
      await waitConnected(app.cdp);
      await sleep(3_000); // the overlay's own provider check runs first
      expect(await app.cdp.eval<boolean>(hasButton("Choose later"))).toBe(false);
    }, 180_000);

    it("Settings › Providers re-runs onboarding: a custom endpoint tested by the real route", async () => {
      const running = app as RunningApp;
      const current = stack as Stack;
      await running.secondLaunch(`${DEV_SCHEME}://open/settings?view=providers&page=accounts`);
      await clickButton(running.cdp, "Set up a provider", 30_000);
      // W52-m: the custom endpoint row sits behind the "Other providers" disclosure
      await clickButtonStarting(running.cdp, "Other providers", 30_000);
      await clickButtonStarting(running.cdp, "Use a custom OpenAI-compatible endpoint", 30_000);
      // the base URL field (the endpoint's optional key field is a password input after it)
      await fillField(
        running.cdp,
        '.onboarding-form input[data-ui="onboarding-endpoint-url"]',
        `http://127.0.0.1:${current.modelPort}/v1`,
      );
      await clickButton(running.cdp, "Test and continue");
      await running.cdp.waitFor(hasButton("Start chatting"), "the model step", 30_000);
      const model = await running.cdp.eval<string>(
        `document.querySelector('.onboarding-form input').value`,
      );
      expect(model.startsWith("fake-model")).toBe(true);
      await clickButton(running.cdp, "Start chatting");
      await running.cdp.waitFor(
        `!(${hasButton("Start chatting")})`,
        "the overlay to close",
        30_000,
      );
      const config = (await current.command({ op: "read", path: "config.yaml" })) as {
        text?: string;
      };
      expect(config.text).toContain(model);
      expect(config.text).toContain(`127.0.0.1:${current.modelPort}`);
    }, 120_000);

    it("a deep link opens Settings where the default folder for new chats is set", async () => {
      const running = app as RunningApp;
      await running.secondLaunch(`${DEV_SCHEME}://open/settings?view=workspace&page=desktop`);
      await running.cdp.waitFor(
        `!!document.querySelector('[data-row="default-directory"] input')`,
        "the workspace page",
        30_000,
      );
      await fillField(running.cdp, '[data-row="default-directory"] input', repo);
      await running.cdp.eval(`location.hash = '#/'`);
      await running.cdp.waitFor(
        `!document.querySelector('[data-row="default-directory"]')`,
        "Settings to close",
      );
    }, 90_000);

    it('settings search over the real schema: "proxy" lands on the owning page, field outlined', async () => {
      const running = app as RunningApp;
      await running.secondLaunch(`${DEV_SCHEME}://open/settings`);
      await fillField(running.cdp, 'input[aria-label="Search settings"]', "proxy");
      await running.cdp.waitFor(
        `document.querySelectorAll('.settings-hit').length > 0`,
        "search hits from the backend's schema",
        20_000,
      );
      await running.cdp.eval(`document.querySelector('.settings-hit').click()`);
      await running.cdp.waitFor(
        `!!document.querySelector('[data-field][data-highlighted="true"]')`,
        "the owning page with the field outlined",
        20_000,
      );
      expect(await running.cdp.eval<string>(`location.hash`)).toMatch(/field=/);
      await running.cdp.eval(`location.hash = '#/'`);
    }, 90_000);

    it("chat: streamed reply, tool row, approval card, the reply after it", async () => {
      const running = app as RunningApp;
      await clickButton(running.cdp, "New session");
      await sendPrompt(running.cdp, "Please write the notes");
      await answerIfAsked(running);
      await running.cdp.waitFor(
        `document.querySelector('.transcript')?.innerText.includes('The notes are written.')`,
        "the reply after the tool",
        60_000,
      );
      expect(await running.cdp.eval<number>(`document.querySelectorAll('.tool-row').length`)).toBe(
        1,
      );
      expect(fs.readFileSync(path.join(repo, "notes.md"), "utf8")).toBe("hello from the agent\n");

      await sendPrompt(running.cdp, "Please clean up");
      await running.cdp.waitFor(hasButton("Run"), "the approval card", 60_000);
      expect(await answerIfAsked(running, 10_000)).toBe(true);
      await running.cdp.waitFor(
        `document.querySelector('.transcript')?.innerText.includes('Cleaned up.')`,
        "the reply after the approval",
        60_000,
      );
    }, 180_000);

    it("panes: git review lists the edit, files shows it, the preview shows a local page", async () => {
      const running = app as RunningApp;
      await showPane(running, "review", "Review");
      await running.cdp.waitFor(
        `document.getElementById('pane-body-review')?.innerText.includes('notes.md')`,
        "the review pane to list notes.md",
        30_000,
      );
      await showPane(running, "files", "Files");
      await running.cdp.waitFor(
        `document.getElementById('pane-body-files')?.innerText.includes('notes.md')`,
        "the files pane to list notes.md",
        30_000,
      );
      await showPane(running, "preview", "Preview");
      await fillField(running.cdp, 'input[aria-label="Address"]', siteUrl);
      await running.cdp.eval(
        `document.querySelector('input[aria-label="Address"]').form.requestSubmit()`,
      );
      const guest = await running.attach(
        (target) => target.type === "webview" && target.url.startsWith(siteUrl),
        "the preview guest",
      );
      await guest.waitFor(`document.title === 'Demo page'`, "the local page");
      expect(await guest.eval<string>(`typeof window.indusDesktop`)).toBe("undefined");
      guest.close();
    }, 120_000);

    it("transcript-once: after a reload every persisted message shows exactly once, in order", async () => {
      const running = app as RunningApp;
      const sessionId = decodeURIComponent(
        (await running.cdp.eval<string>(`location.hash`)).replace(/^#\//, ""),
      );
      await running.cdp.eval(`location.reload()`);
      await waitConnected(running.cdp);
      await running.cdp.waitFor(
        `document.querySelector('.transcript')?.innerText.includes('Cleaned up.')`,
        "the history after the reload",
        30_000,
      );
      const persisted = await sessionRows(sessionId);
      expect(persisted.length).toBeGreaterThanOrEqual(4);
      expect(checkTranscriptOnce(persisted, await shownMessages(running.cdp))).toEqual([]);
    }, 120_000);

    it("sidebar: a second chat, then back to the first from the list", async () => {
      const running = app as RunningApp;
      const first = (await running.cdp.eval<string>(`location.hash`)).replace(/^#\//, "");
      await clickButton(running.cdp, "New session");
      await sendPrompt(running.cdp, "Second chat");
      await running.cdp.waitFor(
        `document.querySelector('.transcript')?.innerText.includes('Hello again.')`,
        "the second chat's reply",
        60_000,
      );
      const second = (await running.cdp.eval<string>(`location.hash`)).replace(/^#\//, "");
      expect(second).not.toBe(first);
      await running.cdp.waitFor(
        `[...document.querySelectorAll('.session-open')].length >= 2`,
        "both chats in the sidebar",
        30_000,
      );
      await running.cdp.eval(`(() => {
        const rows = [...document.querySelectorAll('.session-open')];
        rows.find((r) => r.innerText.includes('Please write the'))?.click();
      })()`);
      await running.cdp.waitFor(
        `location.hash === '#/' + ${JSON.stringify(first)}`,
        "the first chat's route",
      );
      await running.cdp.waitFor(
        `document.querySelector('.transcript')?.innerText.includes('Cleaned up.')`,
        "the first chat's history",
        30_000,
      );
      expect(
        checkTranscriptOnce(
          await sessionRows(decodeURIComponent(first)),
          await shownMessages(running.cdp),
        ),
      ).toEqual([]);
      const all = await storedMessages();
      expect(all.filter((m) => m.role === "user").length).toBe(3);
      expect(await bridge(running.cdp, `b.connection.get()`)).toMatchObject({ ok: true });
    }, 120_000);

    it("profile rail: switch to another profile (its own daemon, empty list) and back", async () => {
      const running = app as RunningApp;
      const current = stack as Stack;
      const made = (await current.command({
        op: "indus",
        args: ["profile", "create", "work", "--clone", "--no-alias", "--no-skills"],
      })) as { code?: number; stderr?: string };
      expect(made.code, made.stderr).toBe(0);
      await running.cdp.eval(`location.hash = '#/'`);
      await running.cdp.eval(`location.reload()`);
      await waitConnected(running.cdp);
      const work = `button[aria-label^="Profile work"]`;
      const home = `button[aria-label^="Profile default"]`;
      await running.cdp.waitFor(`!!document.querySelector('${work}')`, "the profile rail", 30_000);
      await clickAt(running.cdp, work);
      await running.cdp.waitFor(
        `document.querySelector('${work}')?.getAttribute('aria-pressed') === 'true'`,
        "the work profile in front",
        60_000,
      );
      await waitConnected(running.cdp, 120_000);
      await running.cdp.waitFor(
        `document.body.innerText.includes('No sessions yet')`,
        "the work profile's own (empty) list",
        30_000,
      );
      await clickAt(running.cdp, home);
      await running.cdp.waitFor(
        `document.querySelector('${home}')?.getAttribute('aria-pressed') === 'true'`,
        "the default profile back in front",
        60_000,
      );
      await waitConnected(running.cdp, 120_000);
      await running.cdp.waitFor(
        `document.body.innerText.includes('Please write the')`,
        "the default profile's chats again",
        30_000,
      );
    }, 300_000);
  },
);
