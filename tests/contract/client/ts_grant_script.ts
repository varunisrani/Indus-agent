// `@indus/client` attached through a grant from `indus gateway grant --json` to a real daemon
// process (driven by `tests/integration/cli/test_attach_daemon.py`). One JSON line arrives on
// stdin — `{grant, text}`; the ticket is only inside `grant.protocols`, never in argv, env or a
// file. The script attaches, creates a session, submits `text`, waits for the turn to end and
// prints one JSON line with what it saw.

import { createInterface } from "node:readline";
import { IndusClient, WsTransport } from "../../../packages/client/src/index.ts";

interface Grant {
  url: string;
  protocols: string[];
  profile_id: string;
  instance_id: string;
}

const lines = createInterface({ input: process.stdin });
const first = await new Promise<Record<string, unknown>>((done) => {
  lines.once("line", (line) => done(JSON.parse(line) as Record<string, unknown>));
});
lines.close();
const grant = first.grant as Grant;
const text = String(first.text ?? "hello");

async function until(check: () => boolean, ms: number, what: string): Promise<void> {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((done) => setTimeout(done, 10));
  }
}

const client = new IndusClient({
  connect: (hooks) =>
    new WsTransport({
      url: grant.url,
      protocols: grant.protocols,
      logs: hooks.logs,
      onEvent: hooks.onEvent,
    }),
});
const events: Array<Record<string, unknown>> = [];
let ready: Record<string, unknown> | null = null;
client.subscribe((event) => {
  if (event.type === "gateway.ready") ready = event as unknown as Record<string, unknown>;
  else if ("seq" in event) events.push(event as unknown as Record<string, unknown>);
});

client.start();
await until(() => ready !== null, 15_000, "gateway.ready");
await client.request("client.capabilities", { features: ["server_requests"], surface: "tui" });
const created = await client.request("session.create", {});
const sid = created.session_id;
const submitted = await client.request("prompt.submit", { session_id: sid, text });
await until(
  () => events.some((e) => e.type === "turn.done" && e.session_id === sid),
  30_000,
  "turn.done",
);
const reply = events
  .filter((e) => e.type === "message.delta" && e.session_id === sid)
  .map((e) => String(e.text ?? ""))
  .join("");
const seen = ready as Record<string, unknown> | null;
process.stdout.write(
  `${JSON.stringify({
    step: "done",
    session_id: sid,
    turn_id: submitted.turn_id,
    reply,
    profile_id: seen?.profile_id ?? null,
    instance_id: seen?.instance_id ?? null,
    logs: client.logs.tail(200),
  })}\n`,
);
await client.close();
process.exit(0);
