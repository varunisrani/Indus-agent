// Test-only driver for the shared-backend demo (P17-27, `tests/e2e/service/test_shared_backend.py`):
// the real TUI — `App` in Ink's test renderer over the real `AppRuntime`, `@indus/client` and the
// attach transport — attached to the profile's daemon exactly as the entry attaches by default
// (`indus gateway grant --json`, single-use ticket in the subprotocol). The Python test drives it:
// one JSON command per stdin line, one JSON answer per stdout line.
//
//   {"op": "state"}                     → what the screen shows (session, busy, card, messages…)
//   {"op": "type", "text": "hi"}        → keys typed into the composer, then Enter
//   {"op": "press", "keys": ["\r"]}     → raw keys (a card's choice)
//   {"op": "quit"}                      → unmount, close the link and exit 0
//
// Built with `vite build --ssr` into `apps/tui/node_modules/.shared-backend/` (Node resolves the
// app's dependencies from there). The profile comes from the environment the test sets (temp
// `INDUS_HOME` and `HOME`); `INDUS_E2E_INDUS` names the `indus` program for the grant helper.

import { createInterface } from "node:readline";
import { IndusClient } from "@indus/client";
import { App } from "../app/App.tsx";
import { AppRuntime, LinkWatch } from "../app/appRuntime.ts";
import { mount } from "../cards/inkHarness.ts";
import { overlayStore } from "../state/overlayStore.ts";
import { transcriptStore } from "../state/transcriptStore.ts";
import { uiStore } from "../state/uiStore.ts";
import { attachConnector, grantRunner, helperEnv } from "../transport/attach.ts";

const SIZE = { columns: 110, rows: 34 };
const indus = process.env.INDUS_E2E_INDUS ?? "indus";
const grantArgs = ["gateway", "grant", "--json"];
const env = Object.fromEntries(
  Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined),
);

const links = new LinkWatch();
const connect = attachConnector({
  plan: { mode: "daemon", command: indus, args: grantArgs },
  runGrant: grantRunner({ command: indus, args: grantArgs, env: helperEnv(env) }),
  local: null,
});
const client = new IndusClient({ connect: links.wrap(connect), replay: true });
const exits: number[] = [];
const runtime = new AppRuntime({
  client,
  links,
  env,
  attached: true,
  exit: (code) => exits.push(code),
  history: { entries: () => [], push: () => undefined },
});
runtime.start();
const screen = await mount(<App runtime={runtime} size={SIZE} />, SIZE);

function state(): Record<string, unknown> {
  const ui = uiStore.getState();
  const overlay = overlayStore.getState();
  const card = overlay.card as Record<string, unknown> | null;
  return {
    sid: ui.sid,
    busy: ui.busy,
    connected: ui.connected,
    status: ui.statusText,
    usage: ui.usage,
    card: card === null ? null : { kind: card.kind, requestId: card.requestId },
    messages: transcriptStore.getState().messages.map((m) => ({ role: m.role, text: m.text })),
    frame: screen.lastFrame(),
    exits,
  };
}

const lines = createInterface({ input: process.stdin });
for await (const line of lines) {
  if (!line.trim()) continue;
  const command = JSON.parse(line) as { op: string; text?: string; keys?: string[] };
  if (command.op === "type") {
    await screen.press(...(command.text ?? "").split(""));
    await screen.press("\r");
  } else if (command.op === "press") {
    await screen.press(...(command.keys ?? []));
  } else if (command.op === "quit") {
    screen.unmount();
    runtime.dispose();
    await client.close();
    process.stdout.write(`${JSON.stringify({ quit: true })}\n`);
    process.exit(0);
  }
  await screen.press("");
  process.stdout.write(`${JSON.stringify(state())}\n`);
}
