// The TypeScript side of the P17-20 cross-language contract: `@indus/client` over its WebSocket
// transport against a real daemon endpoint. Driven line by line by
// `test_ts_client_against_daemon.py`: grants (url + subprotocols) arrive on stdin — never in argv,
// env or a file — and progress goes out on stdout as one JSON object per line.

import { createInterface } from "node:readline";
import { IndusClient, WsTransport } from "../../../packages/client/src/index.ts";

interface Grant {
  url: string;
  protocols: string[];
}

const say = (value: object): void => {
  process.stdout.write(`${JSON.stringify(value)}\n`);
};

const inbox: Array<Record<string, unknown>> = [];
let wake: (() => void) | null = null;
createInterface({ input: process.stdin }).on("line", (line) => {
  inbox.push(JSON.parse(line) as Record<string, unknown>);
  wake?.();
});

async function next(): Promise<Record<string, unknown>> {
  while (inbox.length === 0) await new Promise<void>((done) => (wake = done));
  wake = null;
  return inbox.shift() as Record<string, unknown>;
}

async function until(check: () => boolean, ms: number, what: string): Promise<void> {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((done) => setTimeout(done, 10));
  }
}

let grant = (await next()).grant as Grant;
let transport: WsTransport | null = null;
const client = new IndusClient({
  connect: (hooks) => {
    transport = new WsTransport({ ...grant, logs: hooks.logs, onEvent: hooks.onEvent });
    return transport;
  },
  replay: { resubscribe: true },
});

const events: Array<Record<string, unknown>> = [];
let readies = 0;
client.subscribe((event) => {
  if (event.type === "gateway.ready") readies += 1;
  else if ("seq" in event) events.push(event as unknown as Record<string, unknown>);
});
let asked = 0;
client.handle({
  serverRequests: {
    approval: (request, meta) => {
      asked += 1;
      say({ step: "approval", request_id: request.request_id, generation: meta.generation });
      return { kind: "approval", choice: "once" };
    },
  },
});

client.start();
await until(() => readies === 1, 10_000, "the first ready");
await client.request("client.capabilities", { features: ["server_requests"], surface: "tui" });
const created = await client.request("session.create", {});
const sid = created.session_id;
const submitted = await client.request("prompt.submit", { session_id: sid, text: "clean up" });
say({ step: "submitted", session_id: sid, turn_id: submitted.turn_id });

const cut = await next(); // the turn is parked in a tool: drop the socket
if (cut.cut !== true) throw new Error("expected the cut command");
const before = client.replay?.cursor(sid)?.seq ?? 0;
await (transport as WsTransport | null)?.close();
await until(() => !client.connected, 5_000, "the close");
say({ step: "cut", cursor: before });

grant = (await next()).grant as Grant; // a fresh single-use ticket for the reconnect
client.start();
await until(
  () => events.some((e) => e.type === "turn.done" && e.session_id === sid),
  20_000,
  "turn.done after the reconnect",
);
const seqs = events.filter((e) => e.session_id === sid).map((e) => Number(e.seq));
const text = events
  .filter((e) => e.type === "message.delta" && e.session_id === sid)
  .map((e) => String(e.text ?? ""))
  .join("");
say({
  step: "done",
  seqs,
  text,
  asked,
  readies,
  before,
  last_cursor: client.replay?.cursor(sid)?.seq ?? null,
  logs: client.logs.tail(200),
});
await client.close();
process.exit(0);
