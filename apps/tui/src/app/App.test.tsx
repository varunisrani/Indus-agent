import { afterEach, describe, expect, it } from "vitest";
import { KEY, type Mounted, mount } from "../cards/inkHarness.ts";
import { overlayStore } from "../state/overlayStore.ts";
import { transcriptStore } from "../state/transcriptStore.ts";
import { uiStore } from "../state/uiStore.ts";
import { App, cardHeight, PLACEHOLDER, toItem } from "./App.tsx";
import { type RuntimeRig, runtimeRig, settle } from "./testRuntime.ts";

let rig: RuntimeRig | null = null;
let screen: Mounted | null = null;
afterEach(() => {
  screen?.unmount();
  rig?.runtime.dispose();
  screen = null;
  rig = null;
});

async function boot(): Promise<{ r: RuntimeRig; s: Mounted }> {
  const r = runtimeRig({
    handlers: {
      "session.list": () => ({
        sessions: [{ session_id: "s-stored", title: "older chat", started_at: 1, source: "cli" }],
      }),
    },
  });
  rig = r;
  await r.ready();
  const s = await mount(<App runtime={r.runtime} size={{ columns: 100, rows: 30 }} />, {
    columns: 100,
    rows: 30,
  });
  screen = s;
  return { r, s };
}

describe("App", () => {
  it("shows the transcript, the status rule and the composer", async () => {
    const { r, s } = await boot();
    const sid = String(uiStore.getState().sid);
    r.server.emit({ type: "session.info", session_id: sid, model: "model-x", provider: "p" });
    transcriptStore.getState().append({ role: "user", text: "what is up" });
    transcriptStore.getState().append({ role: "assistant", text: "all quiet here" });
    await settle();
    await s.press("");
    const frame = s.lastFrame();
    expect(frame).toContain("what is up");
    expect(frame).toContain("all quiet here");
    expect(frame).toContain(PLACEHOLDER.slice(1, 20));
    expect(frame).toContain("model-x");
    expect(frame).toContain("ready");
  });

  it("typed keys reach the composer through the key router; Enter sends", async () => {
    const { r, s } = await boot();
    await s.press("h", "i");
    expect(r.runtime.composer.text).toBe("hi");
    await s.press(KEY.enter);
    await settle();
    expect(r.calls("prompt.submit").map((p) => p.text)).toEqual(["hi"]);
  });

  it("a card that owns the keyboard hides the composer", async () => {
    const { r, s } = await boot();
    void r.server.ask({
      kind: "approval",
      request_id: r.server.nextServerId(),
      session_id: String(uiStore.getState().sid),
      command: "make clean",
      description: "remove build output",
    } as never);
    await settle();
    await s.press("");
    const frame = s.lastFrame();
    expect(frame).toContain("make clean");
    expect(frame).not.toContain(PLACEHOLDER.slice(1, 20));
    await s.press("x");
    expect(r.runtime.composer.text).toBe("");
  });

  it("Ctrl+X opens the session switcher with new, live and stored rows", async () => {
    const { s } = await boot();
    await s.press(KEY.ctrl("x"));
    await settle();
    await s.press("");
    const frame = s.lastFrame();
    expect(overlayStore.getState().card?.kind).toBe("sessionPicker");
    expect(frame).toContain("Sessions");
    expect(frame).toContain("+ new session");
    expect(frame).toContain("older chat");
    await s.press(KEY.escape);
    expect(overlayStore.getState().card).toBeNull();
  });

  it("maps settled messages to rows once per message and sizes cards", () => {
    const message = {
      id: "m1",
      role: "assistant" as const,
      text: "x",
      kind: "diff" as const,
      createdAt: 1,
    };
    expect(toItem(message)).toBe(toItem(message));
    expect(toItem(message).role).toBe("diff");
    expect(cardHeight(null, 30)).toBe(0);
    expect(cardHeight({ id: "p", kind: "pager", payload: {}, ownsInput: true }, 30)).toBe(27);
    expect(cardHeight({ id: "c", kind: "confirm", payload: {}, ownsInput: true }, 30)).toBe(7);
  });
});
