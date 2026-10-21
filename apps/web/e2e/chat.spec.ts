// The Chat tab against a real `indus dashboard` and daemon (P18-30): the real TUI in xterm over
// `/api/pty`, and the browser half of the M6 demo — the browser chat and a classic CLI attached to
// the same daemon session.

import type { WebSocket } from "@playwright/test";
import { expect, type Stack, type Turn, test } from "./fixtures.ts";

const READY = "Ask anything";

const APPROVAL_TURN: Turn = [
  { text: ["Streaming ", "from the daemon. "] },
  { tool_call: { name: "terminal", arguments: { command: "find victim -delete" }, id: "call_1" } },
  { finish: "tool_calls" },
];

async function ptyChildren(stack: Stack): Promise<number[]> {
  return (await stack.ask("pty_children")).pids as number[];
}

async function userRows(stack: Stack, text: string): Promise<number> {
  const rows = await stack.rows(
    `SELECT count(*) FROM messages WHERE role = 'user' AND content = '${text.replaceAll("'", "''")}'`,
  );
  return Number(rows[0]?.[0] ?? 0);
}

test("chat: a prompt and its approval in the terminal; Logs and back keeps the PTY; reload reattaches; a dead child offers a new session", async ({
  launch,
  page,
  term,
}) => {
  const stack = await launch({
    turns: [
      APPROVAL_TURN,
      [{ text: ["Cleaned ", "up."] }],
      [{ text: "Still the same chat." }],
      [{ text: "A brand new chat." }],
    ],
  });
  const sockets: WebSocket[] = [];
  page.on("websocket", (socket) => {
    if (new URL(socket.url()).pathname === "/api/pty") sockets.push(socket);
  });

  await page.goto(`${stack.url}/chat`);
  await term.shows(READY);
  expect(sockets).toHaveLength(1);

  // A prompt typed in xterm reaches the TUI, the daemon and the model; its approval card is
  // answered in the terminal (Enter: the first choice, allow once).
  await term.submit("please clean up");
  await term.shows("Streaming from the daemon.");
  await term.shows("Needs your approval");
  await term.shows("find victim -delete");
  await term.press("Enter");
  await term.shows("Cleaned up.");
  await expect.poll(async () => (await stack.ask("model_turns")).turns).toBe(2);
  await expect.poll(() => term.text()).not.toContain("Needs your approval");
  expect(await userRows(stack, "please clean up")).toBe(1);

  // Another page and back: the same terminal and socket, no new dial.
  const [child] = await ptyChildren(stack);
  expect(child).toBeGreaterThan(0);
  await page.getByRole("link", { name: "Logs" }).click();
  await expect(page.getByRole("heading", { name: "Logs" })).toBeVisible();
  await expect(page.getByTestId("chat-terminal")).toBeHidden();
  await page.getByRole("link", { name: "Chat" }).click();
  await term.shows("Cleaned up.");
  expect(sockets).toHaveLength(1);
  expect(sockets[0]?.isClosed()).toBe(false);
  expect(await ptyChildren(stack)).toEqual([child]);

  // A full reload: the tab's attach token reattaches the same PTY child; the ring replays and the
  // TUI redraws, so the transcript is back; the chat goes on in the same session.
  await page.reload();
  await term.shows("Cleaned up.");
  expect(sockets).toHaveLength(2);
  expect(await ptyChildren(stack)).toEqual([child]);
  await term.submit("are you still there");
  await term.shows("Still the same chat.");
  const sessions = await stack.rows(
    "SELECT DISTINCT session_id FROM messages WHERE role = 'user' AND content IN ('please clean up', 'are you still there')",
  );
  expect(sessions).toHaveLength(1);

  // The PTY child dies: the host says so and offers a new session, which starts a new child.
  await stack.ask("kill", { pid: child });
  const restart = page.getByRole("button", { name: "Start new session" });
  await expect(restart).toBeVisible();
  await restart.click();
  await term.shows(READY);
  await expect.poll(() => ptyChildren(stack)).not.toEqual([child]);
  await expect.poll(async () => (await ptyChildren(stack)).length).toBe(1);
  await term.submit("hello again");
  await term.shows("A brand new chat.");
  const fresh = await stack.rows(
    "SELECT DISTINCT session_id FROM messages WHERE role = 'user' AND content = 'hello again'",
  );
  expect(fresh).toHaveLength(1);
  expect(fresh[0]?.[0]).not.toBe(sessions[0]?.[0]);
});

test("M6: the browser chat and a classic CLI share one daemon session, each sees the other's turns, and the CLI's approval is answered in the browser", async ({
  launch,
  page,
  term,
}) => {
  const stack = await launch({
    turns: [
      [{ text: "Hello first." }],
      [{ text: ["Browser ", "turn ", "answered."] }],
      APPROVAL_TURN,
      [{ text: ["Cleaned ", "up."] }],
    ],
  });
  const opened = await stack.ask("indus", { args: ["chat", "-q", "hello"] });
  expect(opened.code, String(opened.stderr)).toBe(0);
  const sid = /session_id: (\S+)/.exec(String(opened.stderr))?.[1] ?? "";
  expect(sid).not.toBe("");

  // The browser chat on that session, and the classic REPL attached to the same daemon session.
  await page.goto(`${stack.url}/chat?resume=${encodeURIComponent(sid)}`);
  await term.shows(READY);
  await stack.ask("cli.open", { session: sid });

  // 1. A prompt typed in the browser: its turn streams live in the CLI.
  await term.submit("from the browser");
  await term.shows("Browser turn answered.");
  await stack.cliExpect("Browser turn answered");

  // 2. A prompt typed in the CLI streams in the browser; the approval it raises is answered from
  //    the dashboard, and the CLI's card closes.
  await stack.ask("cli.send", { text: "please clean up" });
  await stack.cliExpect("Streaming from the daemon");
  await term.shows("Streaming from the daemon.");
  await stack.cliExpect("Approval needed");
  await term.shows("Needs your approval");
  await term.press("Enter");
  await stack.cliExpect("question answered in another window");
  await stack.cliExpect("Cleaned up");
  await term.shows("Cleaned up.");
  await expect.poll(() => term.text()).not.toContain("Needs your approval");

  // One session, one user row per prompt whichever surface typed it.
  expect(await userRows(stack, "from the browser")).toBe(1);
  expect(await userRows(stack, "please clean up")).toBe(1);
  const sessions = await stack.rows(
    "SELECT DISTINCT session_id FROM messages WHERE role = 'user' AND content IN ('hello', 'from the browser', 'please clean up')",
  );
  expect(sessions).toEqual([[sid]]);
  expect((await stack.ask("model_turns")).turns).toBe(4);
  await stack.ask("cli.close");
});
