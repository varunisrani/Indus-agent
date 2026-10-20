import { afterEach, describe, expect, it } from "vitest";
import { overlayStore } from "../state/overlayStore.ts";
import { transcriptStore } from "../state/transcriptStore.ts";
import { uiStore } from "../state/uiStore.ts";
import { OUTCOME_UNKNOWN_LINE } from "./appRuntime.ts";
import { ATTACH_RESTART_LINE, RESTART_LINE } from "./recovery.ts";
import { type RuntimeRig, resumeAnswer, runtimeRig, settle } from "./testRuntime.ts";

let rig: RuntimeRig | null = null;
const use = (made: RuntimeRig): RuntimeRig => {
  rig = made;
  return made;
};
afterEach(() => {
  rig?.runtime.dispose();
  rig = null;
});

const lines = (): string[] =>
  transcriptStore.getState().messages.map((m) => `${m.role}: ${m.text}`);

/** Type `text` into the composer and press Enter. */
async function send(r: RuntimeRig, text: string): Promise<void> {
  r.runtime.composer.setText(text);
  r.runtime.submitter.submit();
  await settle();
}

/** A running turn in the open session, as the bridge would stream it. */
async function startTurn(r: RuntimeRig, turn: string): Promise<void> {
  const sid = String(uiStore.getState().sid);
  r.server.emit({ type: "turn.started", session_id: sid, turn_id: turn, generation: 1 });
  r.server.emit({ type: "message.delta", session_id: sid, turn_id: turn, text: "partial rep" });
  await settle();
}

const promptTexts = (r: RuntimeRig): unknown[] => r.calls("prompt.submit").map((p) => p.text);

describe("crash recovery against the fake server", () => {
  it("child dies mid-turn: same stored id resumed, nothing re-sent, queue kept", async () => {
    const stored: { role: string; text: string }[] = [];
    const r = use(
      runtimeRig({
        handlers: {
          "prompt.submit": (p) => {
            stored.push({ role: "user", text: String(p.text) });
            return { accepted: true };
          },
          "session.resume": (p) => resumeAnswer(String(p.session_id), stored),
        },
      }),
    );
    await r.ready();
    const sid = uiStore.getState().sid;
    await send(r, "first question");
    await startTurn(r, "t1");
    expect(uiStore.getState().busy).toBe(true);
    await send(r, "typed while busy");
    expect(r.runtime.queue.length).toBe(1);

    await r.die();
    expect(uiStore.getState().busy).toBe(false);
    expect(uiStore.getState().sid).toBeNull();
    expect(r.runtime.recovery.target).toBe(sid);
    expect(lines()).toContain(`system: ${RESTART_LINE}`);
    expect(r.server.connections).toBe(2);
    expect(promptTexts(r)).toEqual(["first question"]);

    await r.ready();
    expect(r.calls("session.resume")).toEqual([{ session_id: sid }]);
    expect(r.calls("session.create")).toHaveLength(1);
    expect(uiStore.getState().sid).toBe(sid);
    expect(r.runtime.recovery.target).toBeNull();
    // the in-flight prompt is not replayed; the never-sent queued one goes now, once
    expect(promptTexts(r)).toEqual(["first question", "typed while busy"]);
    expect(lines().filter((l) => l === "user: first question")).toHaveLength(1);
  });

  it("attached: a link lost mid-reply says the outcome is unknown and sends nothing again", async () => {
    const r = use(runtimeRig({ attached: true }));
    await r.ready();
    const sid = uiStore.getState().sid;
    await send(r, "first question");
    await startTurn(r, "t1");
    await r.die();
    expect(lines()).toContain(`system: ${ATTACH_RESTART_LINE}`);
    expect(lines()).not.toContain(`system: ${RESTART_LINE}`);
    await r.ready();
    expect(r.calls("session.resume")).toEqual([{ session_id: sid }]);
    // the line survives the reopen (which clears the transcript); the prompt is not sent again
    expect(lines()).toContain(`system: ${OUTCOME_UNKNOWN_LINE}`);
    expect(promptTexts(r)).toEqual(["first question"]);
    // an idle link lost later says nothing about an outcome
    await r.die();
    await r.ready();
    expect(lines().filter((l) => l === `system: ${OUTCOME_UNKNOWN_LINE}`)).toHaveLength(0);
  });

  it("the bridge child's restart keeps its own wording and no outcome line", async () => {
    const r = use(runtimeRig());
    await r.ready();
    await send(r, "first question");
    await startTurn(r, "t1");
    await r.die();
    expect(lines()).toContain(`system: ${RESTART_LINE}`);
    await r.ready();
    expect(lines()).not.toContain(`system: ${OUTCOME_UNKNOWN_LINE}`);
  });

  it("4th death in 60 s prints the error once; a later success still resumes the same id", async () => {
    const r = use(runtimeRig());
    await r.ready();
    const sid = uiStore.getState().sid;
    for (let i = 0; i < 3; i += 1) {
      r.clock.advance(1_000);
      await r.die();
      expect(r.server.connections).toBe(i + 2);
    }
    r.clock.advance(1_000);
    await r.die();
    expect(r.server.connections).toBe(4);
    const errors = () => lines().filter((l) => l.startsWith("system: error:"));
    expect(errors()).toHaveLength(1);
    expect(lines().filter((l) => l === `system: ${RESTART_LINE}`)).toHaveLength(3);
    // background retry on the backoff
    r.clock.advance(1_000);
    expect(r.server.connections).toBe(5);
    await r.die();
    expect(errors()).toHaveLength(1);
    r.clock.advance(2_000);
    expect(r.server.connections).toBe(6);
    await r.ready();
    expect(r.calls("session.resume")).toEqual([{ session_id: sid }]);
    expect(r.calls("session.create")).toHaveLength(1);
    expect(uiStore.getState().sid).toBe(sid);
  });

  it("a resume refused after a restart keeps the target and creates nothing", async () => {
    const r = use(runtimeRig());
    await r.ready();
    const sid = uiStore.getState().sid;
    await r.die();
    r.server.handle("session.resume", () => {
      throw new Error("store unreadable");
    });
    await r.ready();
    expect(r.runtime.recovery.target).toBe(sid);
    expect(r.calls("session.create")).toHaveLength(1);
    expect(lines().some((l) => l.includes(`could not reopen session ${sid}`))).toBe(true);
  });

  it("open approval cards are settled when the child dies", async () => {
    const r = use(runtimeRig());
    await r.ready();
    const sid = String(uiStore.getState().sid);
    void r.server.ask({
      kind: "approval",
      request_id: r.server.nextServerId(),
      session_id: sid,
      command: "rm -rf build",
      description: "delete the build folder",
    } as never);
    await settle();
    expect(overlayStore.getState().card?.kind).toBe("approval");
    await r.die();
    expect(overlayStore.getState().card).toBeNull();
    expect(r.runtime.cards.openCount).toBe(0);
  });

  it("our own restart's close of the old transport is not a loss", async () => {
    const r = use(runtimeRig());
    await r.ready();
    r.client.start();
    await settle();
    expect(r.server.connections).toBe(2);
    expect(lines()).not.toContain(`system: ${RESTART_LINE}`);
  });

  it("dispose stops restarts (the app is leaving)", async () => {
    const r = use(runtimeRig());
    await r.ready();
    r.runtime.dispose();
    await r.die();
    expect(r.server.connections).toBe(1);
  });
});

describe("wiring", () => {
  it("slash commands run through the dispatcher; /quit leaves through exit", async () => {
    const r = use(runtimeRig());
    await r.ready();
    await send(r, "/quit");
    expect(r.exits).toEqual([0]);
  });

  it("/sessions new starts a session and keeps the current one live", async () => {
    const r = use(runtimeRig());
    await r.ready();
    const first = uiStore.getState().sid;
    await send(r, "/sessions new planning");
    await settle();
    expect(r.calls("session.create").at(-1)).toMatchObject({ title: "planning" });
    expect(r.calls("session.close")).toEqual([]);
    expect(r.runtime.lifecycle.liveSessions().map((s) => s.id)).toContain(first);
  });

  it("/sessions opens the switcher card and Ctrl+O opens the model picker for the session", async () => {
    const r = use(runtimeRig());
    await r.ready();
    await send(r, "/sessions");
    expect(overlayStore.getState().card?.kind).toBe("sessionPicker");
    overlayStore.getState().close(String(overlayStore.getState().card?.id));
    r.runtime.keyActions.openModelPicker();
    expect(overlayStore.getState().card).toMatchObject({
      kind: "modelPicker",
      payload: { sessionId: uiStore.getState().sid, refresh: false },
    });
  });

  it("the switcher ops list live sessions and open stored ones", async () => {
    const r = use(
      runtimeRig({
        handlers: {
          "session.list": () => ({
            sessions: [{ session_id: "s-stored", started_at: 1, source: "cli" }],
          }),
        },
      }),
    );
    await r.ready();
    const ops = r.runtime.switcherOps;
    expect(ops.live().map((row) => row.id)).toEqual([uiStore.getState().sid]);
    expect((await ops.stored()).map((row) => row.session_id)).toEqual(["s-stored"]);
    ops.open("s-stored");
    await settle();
    expect(uiStore.getState().sid).toBe("s-stored");
  });

  it("session.info of a session opened later is applied when it becomes the open one", async () => {
    const r = use(runtimeRig());
    await r.ready();
    r.server.emit({
      type: "session.info",
      session_id: "s-b",
      model: "m-b",
      provider: "p",
      title: "B",
    });
    await settle();
    expect(uiStore.getState().info?.model).toBeUndefined();
    await r.runtime.lifecycle.switchTo("s-b");
    expect(uiStore.getState().info?.model).toBe("m-b");
    expect(uiStore.getState().title).toBe("B");
  });

  it("starts config sync once the first session exists", async () => {
    const r = use(runtimeRig());
    await r.ready();
    await settle();
    const keys = r.calls("config.get").map((p) => p.key);
    expect(keys).toContain("full");
    expect(keys).toContain("mtime");
  });
});
