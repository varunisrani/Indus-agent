import { errorNameFor } from "@indus/client";
import { FakeRpcError } from "@indus/client/testing";
import { beforeEach, describe, expect, it } from "vitest";
import { resetUi, type SubmitRig, settle, submitRig } from "../composer/testSupport.ts";
import { uiStore } from "../state/uiStore.ts";
import { DOUBLE_ENTER_MS } from "./submission.ts";

const SESSION_BUSY = 3001;
const turn = { turn_id: "t1", generation: 1, status: "streaming" };

function type(rig: SubmitRig, text: string): string {
  rig.composer.setText(text);
  return rig.submitter.submit();
}

function sent(rig: SubmitRig): unknown[] {
  return rig.bridge.calls("prompt.submit").map((params) => params.text);
}

/** The running turn ends: the session settles. */
async function settleTurn(): Promise<void> {
  uiStore.getState().patch({ busy: false });
  await settle();
}

beforeEach(() => resetUi());

describe("double enter", () => {
  it("interrupts when the second empty Enter comes at 449 ms", () => {
    resetUi({ busy: true });
    const rig = submitRig();
    rig.clock.current = 10_000;
    expect(rig.submitter.submit()).toBe("ignored");
    rig.clock.current = 10_000 + DOUBLE_ENTER_MS - 1;
    expect(rig.submitter.submit()).toBe("interrupted");
    expect(rig.interrupts).toEqual([{ keepBusy: false }]);
  });

  it("does not interrupt at 451 ms", () => {
    resetUi({ busy: true });
    const rig = submitRig();
    rig.clock.current = 10_000;
    rig.submitter.submit();
    rig.clock.current = 10_451;
    expect(rig.submitter.submit()).toBe("ignored");
    expect(rig.interrupts).toEqual([]);
  });

  it("keeps the turn busy when prompts are queued", () => {
    resetUi({ busy: true });
    const rig = submitRig();
    rig.queue.add("waiting");
    rig.submitter.submit();
    rig.clock.current = 100;
    rig.submitter.submit();
    expect(rig.interrupts).toEqual([{ keepBusy: true }]);
  });

  it("sends the next queued prompt on an empty Enter while idle", async () => {
    const rig = submitRig({ "prompt.submit": () => turn });
    rig.queue.add("queued earlier");
    expect(rig.submitter.submit()).toBe("drained");
    await settle();
    expect(sent(rig)).toEqual(["queued earlier"]);
  });
});

describe("pipeline", () => {
  it("takes a highlighted completion first", () => {
    const rig = submitRig();
    rig.composer.setText("/he");
    rig.composer.setCompletion({
      items: [{ text: "/help", display: "/help" }],
      index: 0,
      replaceFrom: 1,
    });
    expect(rig.submitter.submit()).toBe("completed");
    expect(rig.composer.text).toBe("/help");
    expect(rig.slash).toEqual([]);
  });

  it("joins backslash continuations with newlines", async () => {
    const rig = submitRig({ "prompt.submit": () => turn });
    expect(type(rig, "first\\")).toBe("continued");
    expect(rig.composer.continuation).toEqual(["first"]);
    expect(type(rig, "second")).toBe("sent");
    await settle();
    expect(sent(rig)).toEqual(["first\nsecond"]);
    expect(rig.composer.hasDraft()).toBe(false);
  });

  it("runs slash commands at once, even while busy, with pastes expanded", () => {
    resetUi({ busy: true });
    const rig = submitRig();
    rig.composer.setText("/review ");
    rig.composer.addToken({
      kind: "paste",
      label: "[[paste 1 · 6 lines]]",
      text: "1\n2\n3\n4\n5\n6",
    });
    expect(rig.submitter.submit()).toBe("slash");
    expect(rig.slash).toEqual([
      { command: "/review 1\n2\n3\n4\n5\n6", display: "/review [[paste 1 · 6 lines]]" },
    ]);
    expect(rig.queue.length).toBe(0);
  });

  it("runs !cmd on the host and prints output plus exit code", async () => {
    const rig = submitRig({ "shell.exec": () => ({ stdout: "out\n", stderr: "warn", code: 3 }) });
    expect(type(rig, "!make test")).toBe("shell");
    await settle();
    expect(rig.bridge.calls("shell.exec")).toEqual([{ command: "make test", session_id: "s1" }]);
    expect(rig.user).toEqual(["!make test"]);
    expect(rig.system).toEqual(["out\nwarn", "exit 3"]);
    expect(uiStore.getState().busy).toBe(false);
  });

  it("prints exit 0 for a silent command", async () => {
    const rig = submitRig({ "shell.exec": () => ({ stdout: "", stderr: "", code: 0 }) });
    type(rig, "!true");
    await settle();
    expect(rig.system).toEqual(["exit 0"]);
  });

  it("queues without a session and sends once one is bound", async () => {
    resetUi({ sid: null, storedSid: null });
    const rig = submitRig({ "prompt.submit": () => turn });
    const stop = rig.submitter.watch();
    expect(type(rig, "early words")).toBe("queued");
    uiStore.getState().patch({ sid: "s1", storedSid: "s1" });
    await settle();
    expect(sent(rig)).toEqual(["early words"]);
    stop();
  });

  it("re-queues a prompt the bridge refused as busy", async () => {
    expect(errorNameFor(SESSION_BUSY)).toBe("SESSION_BUSY");
    const rig = submitRig({
      "prompt.submit": () => {
        throw new FakeRpcError(SESSION_BUSY, "a turn is running");
      },
    });
    type(rig, "raced");
    await settle();
    expect(rig.queue.items.map((item) => item.text)).toEqual(["raced"]);
    expect(rig.system).toEqual([]);
  });
});

describe("busy modes", () => {
  it("queue: prompts wait and drain in order, one per completed reply", async () => {
    resetUi({ busy: true, busyInputMode: "queue" });
    const rig = submitRig({ "prompt.submit": () => turn });
    const stop = rig.submitter.watch();
    for (const text of ["one", "two", "three"]) expect(type(rig, text)).toBe("queued");
    expect(sent(rig)).toEqual([]);
    await settleTurn();
    expect(sent(rig)).toEqual(["one"]);
    expect(uiStore.getState().busy).toBe(true);
    await settleTurn();
    await settleTurn();
    expect(sent(rig)).toEqual(["one", "two", "three"]);
    expect(rig.user).toEqual(["one", "two", "three"]);
    stop();
  });

  it("queue: nothing drains while an item is open for editing", async () => {
    resetUi({ busy: true });
    const rig = submitRig({ "prompt.submit": () => turn });
    const stop = rig.submitter.watch();
    type(rig, "first");
    rig.submitter.cycleQueue(1);
    await settleTurn();
    expect(sent(rig)).toEqual([]);
    stop();
  });

  it("steer: a refused steer (nothing running) is queued", async () => {
    resetUi({ busy: true, busyInputMode: "steer" });
    const rig = submitRig({ "session.steer": () => ({ queued: false }) });
    expect(type(rig, "use the other file")).toBe("steering");
    await settle();
    expect(rig.bridge.calls("session.steer")).toEqual([
      { session_id: "s1", text: "use the other file" },
    ]);
    expect(rig.queue.items.map((item) => item.text)).toEqual(["use the other file"]);
  });

  it("steer: a failed steer is queued too; an accepted one shows the line", async () => {
    resetUi({ busy: true, busyInputMode: "steer" });
    const failing = submitRig({
      "session.steer": () => {
        throw new Error("unsupported");
      },
    });
    type(failing, "x");
    await settle();
    expect(failing.queue.length).toBe(1);
    const accepting = submitRig({ "session.steer": () => ({ queued: true, ticket: "k" }) });
    type(accepting, "y");
    await settle();
    expect(accepting.queue.length).toBe(0);
    expect(accepting.user).toEqual(["y"]);
  });

  it("steer: a prompt with an image always queues", async () => {
    resetUi({ busy: true, busyInputMode: "steer" });
    const rig = submitRig();
    rig.composer.setText("what is this");
    rig.composer.addToken({ kind: "image", label: "[[image 1]]", path: "/a.png", name: "a.png" });
    expect(rig.submitter.submit()).toBe("queued");
    await settle();
    expect(rig.bridge.calls("session.steer")).toEqual([]);
    expect(rig.queue.items[0]?.hasImage).toBe(true);
  });

  it("interrupt: submits at once and lets the bridge redirect", async () => {
    resetUi({ busy: true, busyInputMode: "interrupt" });
    const rig = submitRig({ "prompt.submit": () => ({ ...turn, status: "redirected" }) });
    expect(type(rig, "stop, do this")).toBe("redirecting");
    await settle();
    expect(rig.bridge.calls("prompt.submit")).toEqual([
      { session_id: "s1", text: "stop, do this", when_busy: "redirect" },
    ]);
  });
});

describe("queue editing", () => {
  it("an edited item replaces the old one and goes next", async () => {
    resetUi({ busy: true });
    const rig = submitRig({ "prompt.submit": () => turn });
    const stop = rig.submitter.watch();
    for (const text of ["a", "b", "c"]) type(rig, text);
    rig.submitter.cycleQueue(1);
    rig.submitter.cycleQueue(1);
    expect(rig.composer.text).toBe("b");
    rig.composer.setText("b, but better");
    expect(rig.submitter.submit()).toBe("queued");
    expect(rig.queue.items.map((item) => item.text)).toEqual(["b, but better", "a", "c"]);
    await settleTurn();
    expect(sent(rig)).toEqual(["b, but better"]);
    stop();
  });

  it("an edited item is sent at once when idle; Ctrl+X drops one", async () => {
    const rig = submitRig({ "prompt.submit": () => turn });
    rig.queue.add("x");
    rig.queue.add("y");
    rig.submitter.editQueued(1);
    rig.composer.setText("y2");
    expect(rig.submitter.submit()).toBe("sent");
    await settle();
    expect(sent(rig)).toEqual(["y2"]);
    rig.submitter.editQueued(0);
    expect(rig.submitter.deleteEdited()).toBe(true);
    expect(rig.queue.length).toBe(0);
    expect(rig.composer.hasDraft()).toBe(false);
  });

  it("restores the item's tokens so a collapsed paste still expands", async () => {
    resetUi({ busy: true });
    const rig = submitRig({ "prompt.submit": () => turn });
    rig.composer.setText("check ");
    rig.composer.addToken({ kind: "paste", label: "[[paste 1 · 5 lines]]", text: "1\n2\n3\n4\n5" });
    rig.submitter.submit();
    rig.submitter.editQueued(0);
    expect(rig.composer.tokens).toHaveLength(1);
    rig.composer.setText(`${rig.composer.text} now`);
    rig.submitter.submit();
    uiStore.getState().patch({ busy: false });
    rig.submitter.drain();
    await settle();
    expect(sent(rig)).toEqual(["check 1\n2\n3\n4\n5 now"]);
  });

  it("Ctrl+K sends the next queued prompt now", async () => {
    resetUi({ busy: true });
    const rig = submitRig({ "prompt.submit": () => turn });
    rig.queue.add("urgent");
    expect(rig.submitter.sendNextNow()).toBe(true);
    await settle();
    expect(rig.bridge.calls("prompt.submit")).toEqual([
      { session_id: "s1", text: "urgent", when_busy: "redirect" },
    ]);
    expect(rig.submitter.sendNextNow()).toBe(false);
  });
});

describe("interpolation", () => {
  it("expands {!cmd} only when the prompt leaves; the queued draft keeps the raw text", async () => {
    resetUi({ busy: true });
    const rig = submitRig({
      "shell.exec": (params) => ({
        stdout: params.command === "echo hi" ? "hi\n" : "",
        stderr: "",
        code: 0,
      }),
      "prompt.submit": () => turn,
    });
    const stop = rig.submitter.watch();
    type(rig, "say {!echo hi} twice");
    expect(rig.queue.items[0]?.text).toBe("say {!echo hi} twice");
    expect(rig.bridge.calls("shell.exec")).toEqual([]);
    await settleTurn();
    expect(rig.bridge.calls("shell.exec")).toEqual([{ command: "echo hi", session_id: "s1" }]);
    expect(sent(rig)).toEqual(["say hi twice"]);
    expect(rig.user).toEqual(["say {!echo hi} twice"]);
    stop();
  });

  it("refuses the whole send when a command fails", async () => {
    const rig = submitRig({
      "shell.exec": () => {
        throw new Error("refused");
      },
    });
    type(rig, "x {!rm -rf /} y");
    await settle();
    expect(sent(rig)).toEqual([]);
    expect(rig.system[0]).toMatch(/^not sent/);
    expect(uiStore.getState().busy).toBe(false);
  });
});

describe("reconnect", () => {
  it("never re-sends a prompt that already left", async () => {
    const rig = submitRig({ "prompt.submit": () => turn });
    const stop = rig.submitter.watch();
    type(rig, "only once");
    await settle();
    // the bridge child dies and the session is reopened: busy flips and the id comes back
    uiStore.getState().patch({ busy: false, sid: null });
    await settle();
    uiStore.getState().patch({ sid: "s1", storedSid: "s1", busy: true });
    await settleTurn();
    expect(sent(rig)).toEqual(["only once"]);
    stop();
  });
});
