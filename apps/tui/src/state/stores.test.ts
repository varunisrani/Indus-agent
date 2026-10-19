import { beforeEach, describe, expect, it } from "vitest";
import { captureDestination, focusSession, isCurrent } from "./destination.ts";
import { type Card, overlayStore, resetOverlay } from "./overlayStore.ts";
import { transcriptStore } from "./transcriptStore.ts";
import { keepLast, TRAIL_LIMIT, turnStore } from "./turnStore.ts";
import { busyInputModeFrom, resetUi, uiStore } from "./uiStore.ts";

const card = (id: string, kind: Card["kind"], extra: Partial<Card> = {}): Card => ({
  id,
  kind,
  payload: null,
  ownsInput: true,
  ...extra,
});

beforeEach(() => {
  resetUi();
  resetOverlay();
  turnStore.getState().reset();
  transcriptStore.getState().clear();
});

describe("ui store", () => {
  it("starts from the TUI defaults", () => {
    const s = uiStore.getState();
    expect({
      busyInputMode: s.busyInputMode,
      detailsMode: s.detailsMode,
      statusBar: s.statusBar,
      streaming: s.streaming,
      showReasoning: s.showReasoning,
      inlineDiffs: s.inlineDiffs,
      timestamps: s.timestamps,
      pasteLines: s.pasteLines,
      pasteChars: s.pasteChars,
      mousePreset: s.mousePreset,
      busy: s.busy,
      sid: s.sid,
      revision: s.revision,
    }).toEqual({
      busyInputMode: "queue",
      detailsMode: "collapsed",
      statusBar: "top",
      streaming: true,
      showReasoning: false,
      inlineDiffs: true,
      timestamps: false,
      pasteLines: 5,
      pasteChars: 2000,
      mousePreset: "all",
      busy: false,
      sid: null,
      revision: 0,
    });
  });

  it("keeps queue when the config has no busy-input mode or an unknown one", () => {
    expect(busyInputModeFrom(undefined)).toBe("queue");
    expect(busyInputModeFrom("")).toBe("queue");
    expect(busyInputModeFrom("redirect-ish")).toBe("queue");
    expect(busyInputModeFrom("steer")).toBe("steer");
    expect(busyInputModeFrom("interrupt")).toBe("interrupt");
  });

  it("patches only the given fields", () => {
    uiStore.getState().patch({ busy: true, statusText: "working…" });
    expect(uiStore.getState().busy).toBe(true);
    expect(uiStore.getState().detailsMode).toBe("collapsed");
  });
});

describe("destination guard", () => {
  it("drops a late result after a session switch", async () => {
    focusSession("s-1");
    const dest = captureDestination();
    let painted = "";
    const late = Promise.resolve("old reply").then((text) => {
      if (isCurrent(dest)) painted = text;
    });
    focusSession("s-2");
    await late;
    expect(painted).toBe("");
  });

  it("treats a switch back to the same id as a new destination", () => {
    focusSession("s-1");
    const dest = captureDestination();
    focusSession("s-2");
    focusSession("s-1");
    expect(isCurrent(dest)).toBe(false);
    expect(isCurrent(captureDestination())).toBe(true);
  });

  it("keeps a result whose destination did not move", () => {
    focusSession("s-1", "stored-1");
    const dest = captureDestination();
    uiStore.getState().patch({ busy: true });
    expect(isCurrent(dest)).toBe(true);
  });
});

describe("overlay store", () => {
  it("is blocked by an input-owning card or any view", () => {
    const o = overlayStore.getState();
    expect(o.blocked()).toBe(false);
    o.open(card("c1", "pager", { ownsInput: false }));
    expect(overlayStore.getState().blocked()).toBe(false);
    overlayStore.getState().close("c1");
    overlayStore.getState().open(card("c2", "approval"));
    expect(overlayStore.getState().blocked()).toBe(true);
    overlayStore.getState().close("c2");
    overlayStore.getState().openView("agents");
    expect(overlayStore.getState().blocked()).toBe(true);
    overlayStore.getState().closeView("agents");
    expect(overlayStore.getState().blocked()).toBe(false);
  });

  it("holds a second card until the first closes", () => {
    const o = overlayStore.getState();
    o.open(card("a", "approval", { requestId: "r-a" }));
    o.open(card("b", "clarify", { requestId: "r-b" }));
    expect(overlayStore.getState().card?.id).toBe("a");
    expect(overlayStore.getState().waiting.map((c) => c.id)).toEqual(["b"]);
    overlayStore.getState().close("a");
    expect(overlayStore.getState().card?.id).toBe("b");
    expect(overlayStore.getState().waiting).toEqual([]);
  });

  it("closes a withdrawn request whether it is open or waiting", () => {
    const o = overlayStore.getState();
    o.open(card("a", "sudo", { requestId: "r-a" }));
    o.open(card("b", "secret", { requestId: "r-b" }));
    expect(overlayStore.getState().closeByRequest("r-b")).toBe(true);
    expect(overlayStore.getState().waiting).toEqual([]);
    expect(overlayStore.getState().closeByRequest("r-a")).toBe(true);
    expect(overlayStore.getState().card).toBeNull();
    expect(overlayStore.getState().closeByRequest("r-zz")).toBe(false);
  });

  it("closes the finished turn's flow cards but keeps the model picker", () => {
    const o = overlayStore.getState();
    o.open(card("approve", "approval", { requestId: "r1", turnId: "t1" }));
    o.closeFlowCards("t1");
    expect(overlayStore.getState().card).toBeNull();

    overlayStore.getState().open(card("picker", "modelPicker"));
    overlayStore.getState().open(card("late", "approval", { turnId: "t1" }));
    overlayStore.getState().closeFlowCards("t1");
    expect(overlayStore.getState().card?.kind).toBe("modelPicker");
    expect(overlayStore.getState().waiting).toEqual([]);
  });

  it("keeps a flow card that belongs to another turn", () => {
    overlayStore.getState().open(card("next", "approval", { turnId: "t2" }));
    overlayStore.getState().closeFlowCards("t1");
    expect(overlayStore.getState().card?.id).toBe("next");
  });

  it("re-opening the same card id replaces it instead of queueing a copy", () => {
    overlayStore.getState().open(card("a", "pager", { payload: 1 }));
    overlayStore.getState().open(card("a", "pager", { payload: 2 }));
    expect(overlayStore.getState().card?.payload).toBe(2);
    expect(overlayStore.getState().waiting).toEqual([]);
  });
});

describe("turn store", () => {
  it("resets every per-turn field", () => {
    turnStore.getState().patch({ streamBuffer: "x", reasoning: "y", turnId: "t1" });
    turnStore.getState().reset();
    const s = turnStore.getState();
    expect([s.streamBuffer, s.reasoning, s.turnId, s.segments.length]).toEqual(["", "", null, 0]);
  });

  it("keeps the trail to its limit", () => {
    const rows = Array.from({ length: 12 }, (_, i) => i);
    expect(keepLast(rows, TRAIL_LIMIT)).toEqual([4, 5, 6, 7, 8, 9, 10, 11]);
  });
});

describe("transcript store", () => {
  it("appends with fresh ids and updates in place", () => {
    const id = transcriptStore.getState().append({ role: "assistant", text: "a" });
    transcriptStore.getState().update(id, { text: "b" });
    expect(transcriptStore.getState().messages.map((m) => m.text)).toEqual(["b"]);
  });
});
