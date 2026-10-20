// P17-26: the TUI's integration flows against both hosts (`hosts.ts`): its own stdio bridge child
// and the profile's daemon over the attach WebSocket. The renderer is the real one — `App` in
// Ink's test renderer over the real `AppRuntime`, client and transports — and so are the agent
// processes; the model is the scripted fake on a loopback port. What a person sees must be the
// same on both hosts.

import { afterEach, describe, expect, it } from "vitest";
import { App } from "../app/App.tsx";
import { KEY, type Mounted, mount } from "../cards/inkHarness.ts";
import { overlayStore } from "../state/overlayStore.ts";
import { transcriptStore } from "../state/transcriptStore.ts";
import { uiStore } from "../state/uiStore.ts";
import { HOST_KINDS, type HostRig, hostsMissing, type Step, startHost, until } from "./hosts.ts";

const missing = hostsMissing();
const SIZE = { columns: 100, rows: 30 };

function said(role: string): string[] {
  return transcriptStore
    .getState()
    .messages.filter((m) => m.role === role)
    .map((m) => m.text);
}

describe.skipIf(missing !== null).each(HOST_KINDS)("the TUI on the %s host", (kind) => {
  let rig: HostRig | null = null;
  let screen: Mounted | null = null;

  afterEach(async () => {
    screen?.unmount();
    screen = null;
    await rig?.dispose();
    rig = null;
  }, 90_000);

  async function open(turns: Step[][]): Promise<{ r: HostRig; s: Mounted }> {
    const r = await startHost(kind, turns);
    rig = r;
    const s = await mount(<App runtime={r.runtime} size={SIZE} />, SIZE);
    screen = s;
    await until(
      () => uiStore.getState().sid !== null && uiStore.getState().connected,
      "an open session",
      60_000,
      () => s.lastFrame(),
    );
    return { r, s };
  }

  async function send(s: Mounted, text: string): Promise<void> {
    await s.press(...text.split(""));
    await s.press(KEY.enter);
  }

  it("a prompt streams, settles into the transcript and the usage shows", async () => {
    const { r, s } = await open([
      [{ text: ["Hello ", "from the agent"] }, { usage: { prompt: 12, completion: 4 } }],
    ]);
    await send(s, "hi");
    await until(
      () => said("assistant").includes("Hello from the agent") && !uiStore.getState().busy,
      "the settled reply",
      60_000,
      () => s.lastFrame(),
    );
    await until(() => uiStore.getState().usage !== null, "the usage", 10_000);
    expect(said("user")).toEqual(["hi"]);
    expect(uiStore.getState().usage?.api_calls).toBe(1);
    expect(r.model.turns).toBe(1);
    await s.press("");
    expect(s.lastFrame()).toContain("Hello from the agent");
  }, 120_000);

  it("/steer while a reply runs is handed to it", async () => {
    const { s } = await open([
      [
        { text: ["Working"] },
        { stall: 3 },
        { tool_call: { name: "terminal", arguments: { command: "echo ok" }, id: "c1" } },
      ],
      [{ text: "Steered done." }],
    ]);
    await send(s, "start");
    await until(
      () => uiStore.getState().busy,
      "the reply running",
      60_000,
      () => s.lastFrame(),
    );
    await send(s, "/steer use python");
    await until(
      () => said("system").includes("Steer note handed to the running reply."),
      "the steer taken",
      30_000,
      () => s.lastFrame(),
    );
    await until(
      () => said("assistant").includes("Steered done.") && !uiStore.getState().busy,
      "the reply's end",
      60_000,
      () => s.lastFrame(),
    );
  }, 120_000);

  it("an approval card is answered from the keyboard and the turn finishes", async () => {
    const { r, s } = await open([
      [
        {
          tool_call: {
            name: "terminal",
            arguments: { command: "find victim -delete" },
            id: "c1",
          },
        },
      ],
      [{ text: "Cleaned up." }],
    ]);
    await send(s, "clean up");
    await until(
      () => overlayStore.getState().card !== null,
      "the approval card",
      60_000,
      () => s.lastFrame(),
    );
    await s.press("");
    expect(s.lastFrame()).toContain("find victim -delete");
    await s.press(KEY.enter); // the first choice: allow once
    await until(
      () => said("assistant").includes("Cleaned up.") && !uiStore.getState().busy,
      "the reply after the approval",
      60_000,
      () => s.lastFrame(),
    );
    expect(overlayStore.getState().card).toBeNull();
    expect(r.model.turns).toBe(2);
  }, 120_000);
});
