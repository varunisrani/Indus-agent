import { createElement, type ReactNode } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { Composer } from "../composer/composerStore.ts";
import { fakeBridge, renderInk, resetUi, settle, submitRig } from "../composer/testSupport.ts";
import { overlayStore } from "../state/overlayStore.ts";
import { uiStore } from "../state/uiStore.ts";
import { press, textPress } from "./keymap.ts";
import {
  decideKey,
  isUnbracketedPaste,
  type KeyActions,
  type KeyDecision,
  type KeyMemory,
  type KeyView,
  openModelPicker,
  performKey,
  snapshotView,
  toggleYolo,
  useKeyRouter,
} from "./routeKeys.ts";

const base: KeyView = {
  busy: false,
  hasSession: true,
  card: null,
  hasDraft: false,
  textEmpty: true,
  continuationLines: 0,
  multiline: false,
  cursorOnFirstLine: true,
  cursorOnLastLine: true,
  completionOpen: false,
  historyBrowsing: false,
  queueLength: 0,
  queueEditing: false,
  hasSelection: false,
  viewportRows: 24,
};

const approval = { id: "c1", kind: "approval" as const, ownsInput: true };
const loose = { id: "c2", kind: "confirm" as const, ownsInput: false };
const pager = { id: "c3", kind: "pager" as const, ownsInput: true };

function decide(
  spec: string,
  view: Partial<KeyView> = {},
  memory?: KeyMemory,
  now = 0,
): KeyDecision {
  return decideKey(
    press(spec),
    { ...base, ...view },
    memory ?? { lastEsc: Number.NEGATIVE_INFINITY },
    now,
    false,
  );
}

function escEsc(view: Partial<KeyView>, gap = 200): KeyDecision {
  const memory = { lastEsc: Number.NEGATIVE_INFINITY };
  decide("esc", view, memory, 1_000);
  return decide("esc", view, memory, 1_000 + gap);
}

interface Spy {
  readonly actions: KeyActions;
  readonly calls: string[];
  readonly history: string[];
}

function spy(composer: Composer): Spy {
  const rig = submitRig();
  const calls: string[] = [];
  const history: string[] = [];
  const note = (name: string) => () => {
    calls.push(name);
  };
  const submitter = Object.assign(rig.submitter, {
    clearDraft: () => {
      calls.push("clear");
      composer.clear();
    },
    submit: () => {
      calls.push("submit");
      return "sent" as const;
    },
  });
  return {
    calls,
    history,
    actions: {
      composer,
      submitter,
      history: { entries: () => history, push: (text) => history.push(text) },
      interrupt: note("interrupt"),
      exit: note("exit"),
      cancelCard: (card) => calls.push(`cancel:${card.kind}`),
      scroll: (rows) => calls.push(`scroll:${rows}`),
      redraw: note("redraw"),
      openModelPicker: note("model-picker"),
      openSessionSwitcher: note("session-switcher"),
      openExternalEditor: note("editor"),
      pasteClipboard: note("paste"),
      pasteText: (text) => calls.push(`paste:${text}`),
      toggleYolo: note("yolo"),
      copy: async (text) => {
        calls.push(`copy:${text}`);
        return true;
      },
    },
  };
}

beforeEach(() => resetUi());

describe("ctrl+c priority", () => {
  it.each<[string, Partial<KeyView>, KeyDecision["action"]]>([
    ["draft, idle", { hasDraft: true, textEmpty: false }, "clear-draft"],
    ["draft, busy", { hasDraft: true, textEmpty: false, busy: true }, "clear-draft"],
    [
      "continuation only, busy",
      { hasDraft: true, continuationLines: 1, busy: true },
      "clear-draft",
    ],
    ["token only, busy", { hasDraft: true, busy: true }, "clear-draft"],
    ["empty, busy", { busy: true }, "interrupt"],
    ["empty, idle", {}, "exit"],
  ])("%s → %s", (_state, view, action) => {
    expect(decide("ctrl+c", view).action).toBe(action);
  });

  it("exits rather than interrupting when there is no session", () => {
    expect(decide("ctrl+c", { busy: true, hasSession: false }).action).toBe("exit");
  });

  it("clearing removes text, continuation lines and tokens together", () => {
    const composer = new Composer();
    composer.pushContinuation("above");
    composer.setText("x ");
    composer.addToken({ kind: "paste", label: "[[paste 1 · 5 lines]]", text: "…" });
    const { actions, calls } = spy(composer);
    const view = snapshotView(uiStore.getState(), null, composer, { length: 0, editing: null }, 24);
    const k = press("ctrl+c");
    performKey(decideKey(k, view, { lastEsc: 0 }, 0, false), k, view, actions);
    expect(calls).toEqual(["clear"]);
    expect(composer.hasDraft()).toBe(false);
  });
});

describe("ctrl+d", () => {
  it("does nothing with only a paste token in the composer", () => {
    const composer = new Composer();
    composer.addToken({ kind: "paste", label: "[[paste 1 · 9 lines]]", text: "…" });
    const { actions, calls } = spy(composer);
    const view = snapshotView(uiStore.getState(), null, composer, { length: 0, editing: null }, 24);
    const k = press("ctrl+d");
    const decision = decideKey(k, view, { lastEsc: 0 }, 0, false);
    expect(decision.action).toBe("none");
    performKey(decision, k, view, actions);
    expect(calls).toEqual([]);
    expect(composer.tokens).toHaveLength(1);
  });

  it("exits from a fully empty composer", () => {
    expect(decide("ctrl+d").action).toBe("exit");
    expect(decide("ctrl+d", { busy: true }).action).toBe("exit");
  });
});

describe("esc esc", () => {
  it("with an approval card open: discards a draft but never interrupts", () => {
    expect(escEsc({ card: approval, hasDraft: true, busy: true })).toEqual({
      action: "discard-draft",
    });
    expect(escEsc({ card: approval, busy: true })).toEqual({ action: "card" });
  });

  it("without a card: interrupts a running turn when the composer is empty", () => {
    expect(escEsc({ busy: true })).toEqual({ action: "interrupt" });
    expect(escEsc({})).toEqual({ action: "none" });
  });

  it("needs both presses within 500 ms; a single Esc never stops work", () => {
    expect(escEsc({ busy: true }, 500)).toEqual({ action: "interrupt" });
    expect(escEsc({ busy: true }, 501)).toEqual({ action: "none" });
    expect(decide("esc", { busy: true }).action).toBe("none");
  });

  it("starts a new pair after a double", () => {
    const memory = { lastEsc: Number.NEGATIVE_INFINITY };
    decide("esc", { busy: true }, memory, 0);
    expect(decide("esc", { busy: true }, memory, 100).action).toBe("interrupt");
    expect(decide("esc", { busy: true }, memory, 200).action).toBe("none");
  });

  it("discarding keeps the draft in history", () => {
    const composer = new Composer();
    composer.setText("almost sent");
    const { actions, history } = spy(composer);
    const view = snapshotView(
      uiStore.getState(),
      approval,
      composer,
      { length: 0, editing: null },
      24,
    );
    const memory = { lastEsc: Number.NEGATIVE_INFINITY };
    for (const at of [0, 100]) {
      const k = press("esc");
      performKey(decideKey(k, view, memory, at, false), k, view, actions);
    }
    expect(history).toEqual(["almost sent"]);
    expect(composer.text).toBe("");
  });
});

describe("single esc", () => {
  it("cancels a queue edit before clearing a selection", () => {
    expect(decide("esc", { queueEditing: true, hasSelection: true }).action).toBe(
      "cancel-queue-edit",
    );
    expect(decide("esc", { hasSelection: true }).action).toBe("clear-selection");
    expect(decide("esc", { completionOpen: true }).action).toBe("completion-close");
  });
});

describe("scroll under a card", () => {
  it.each([
    ["pageup", -12],
    ["pagedown", 12],
    ["shift+up", -1],
    ["shift+down", 1],
    ["wheelup", -3],
  ])("%s reaches the transcript", (spec, rows) => {
    expect(decide(spec, { card: approval })).toEqual({ action: "scroll", rows });
  });

  it("a card that owns input gets every other key, Ctrl+C included", () => {
    for (const spec of ["up", "enter", "1", "y", "tab", "ctrl+x", "ctrl+c", "esc"]) {
      expect(decide(spec, { card: approval, hasDraft: true }).action).toBe("card");
    }
  });

  it("a card that does not own input leaves Ctrl+C (its cancel) and scrolling to us", () => {
    expect(decide("ctrl+c", { card: loose, hasDraft: true }).action).toBe("cancel-card");
    expect(decide("pageup", { card: loose }).action).toBe("scroll");
    expect(decide("enter", { card: loose }).action).toBe("card");
  });

  it("a pager keeps its own paging keys", () => {
    expect(decide("pagedown", { card: pager }).action).toBe("card");
  });

  it("pages by half the viewport, at least 4 rows", () => {
    expect(decide("pagedown", { viewportRows: 6 })).toEqual({ action: "scroll", rows: 4 });
  });
});

describe("the rest of the table", () => {
  it("Up/Down: completion list first, then the queue, then history", () => {
    const typed = { textEmpty: false, hasDraft: true };
    expect(decide("up", { ...typed, completionOpen: true })).toEqual({
      action: "completion-cycle",
      step: -1,
    });
    expect(decide("down", { ...typed, completionOpen: true })).toEqual({
      action: "completion-cycle",
      step: 1,
    });
    expect(decide("up", { queueLength: 2 })).toEqual({ action: "queue-cycle", step: 1 });
    expect(decide("up")).toEqual({ action: "history", step: -1 });
    expect(decide("down", { historyBrowsing: true })).toEqual({ action: "history", step: 1 });
    expect(decide("up", { cursorOnFirstLine: false }).action).toBe("to-editor");
    expect(decide("up", { continuationLines: 1 }).action).toBe("to-editor");
  });

  it("Ctrl+X: cut a selection, else drop the edited item, else switch session", () => {
    expect(decide("ctrl+x", { hasSelection: true, queueEditing: true }).action).toBe(
      "cut-selection",
    );
    expect(decide("ctrl+x", { queueEditing: true }).action).toBe("delete-queued");
    expect(decide("ctrl+x").action).toBe("session-switcher");
  });

  it("Ctrl+K sends the next queued prompt only when there is one", () => {
    expect(decide("ctrl+k", { queueLength: 1 }).action).toBe("send-queued");
    expect(decide("ctrl+k").action).toBe("to-editor");
    expect(decide("ctrl+k", { queueLength: 1, hasSession: false }).action).toBe("to-editor");
  });

  it("Shift+Tab toggles yolo unless a completion list is open; Tab applies outside multiline", () => {
    expect(decide("shift+tab").action).toBe("toggle-yolo");
    expect(decide("shift+tab", { completionOpen: true }).action).toBe("none");
    expect(decide("tab", { completionOpen: true }).action).toBe("completion-apply");
    expect(decide("tab", { completionOpen: true, multiline: true }).action).toBe("none");
  });

  it("global chords", () => {
    expect(decide("ctrl+l").action).toBe("redraw");
    expect(decide("ctrl+o").action).toBe("model-picker");
    expect(decide("ctrl+g").action).toBe("external-editor");
    expect(decide("alt+g").action).toBe("external-editor");
    expect(decide("ctrl+v").action).toBe("paste");
    expect(decide("alt+v").action).toBe("paste");
    expect(decide("enter").action).toBe("to-editor");
    expect(decide("ctrl+z").action).toBe("to-editor");
  });

  it("on macOS Cmd works as the action modifier and Cmd+C copies; Ctrl+C keeps its meaning", () => {
    const at = (spec: string, view: Partial<KeyView> = {}) =>
      decideKey(press(spec), { ...base, ...view }, { lastEsc: Number.NEGATIVE_INFINITY }, 0, true)
        .action;
    expect(at("cmd+o")).toBe("model-picker");
    expect(at("cmd+g")).toBe("external-editor");
    expect(at("cmd+c", { hasSelection: true })).toBe("copy-selection");
    expect(at("cmd+c", { hasDraft: true })).toBe("none");
    expect(at("ctrl+c", { hasDraft: true })).toBe("clear-draft");
  });
});

describe("effects", () => {
  it("Enter through the editor submits; Tab takes the completion", () => {
    const composer = new Composer();
    composer.setText("/he");
    composer.setCompletion({
      items: [{ text: "/help", display: "/help" }],
      index: 0,
      replaceFrom: 1,
    });
    const { actions, calls } = spy(composer);
    const view = snapshotView(uiStore.getState(), null, composer, { length: 0, editing: null }, 24);
    performKey({ action: "completion-apply" }, press("tab"), view, actions);
    expect(composer.text).toBe("/help");
    performKey({ action: "to-editor" }, press("enter"), view, actions);
    expect(calls).toEqual(["submit"]);
  });

  it("sends several lines typed in one event down the paste path", () => {
    const composer = new Composer();
    const { actions, calls } = spy(composer);
    const view = snapshotView(uiStore.getState(), null, composer, { length: 0, editing: null }, 24);
    performKey({ action: "to-editor" }, textPress("line 1\nline 2"), view, actions);
    expect(calls).toEqual(["paste:line 1\nline 2"]);
    expect(isUnbracketedPaste(textPress("committed\r"))).toBe(false);
    expect(isUnbracketedPaste(textPress("x"))).toBe(false);
  });

  it("cuts a selection only once the clipboard took it", async () => {
    const composer = new Composer();
    composer.setText("keep cut");
    composer.editor.moveTo(5);
    composer.editor.moveTo(8, true);
    const { actions, calls } = spy(composer);
    const view = snapshotView(uiStore.getState(), null, composer, { length: 0, editing: null }, 24);
    performKey({ action: "cut-selection" }, press("ctrl+x"), view, actions);
    await settle();
    expect(calls).toEqual(["copy:cut"]);
    expect(composer.text).toBe("keep ");
  });

  it("Shift+Tab asks the bridge to flip yolo for the session", async () => {
    const bridge = fakeBridge({
      "config.set": (params) => ({
        key: "yolo",
        value: params.value,
        applied: "now",
        saved: false,
      }),
    });
    const lines: string[] = [];
    await toggleYolo(bridge.rpc, uiStore, (text) => lines.push(text));
    await toggleYolo(bridge.rpc, uiStore, (text) => lines.push(text));
    expect(bridge.calls("config.set")).toEqual([
      { key: "yolo", value: true, session_id: "s1" },
      { key: "yolo", value: false, session_id: "s1" },
    ]);
    expect(lines).toEqual([
      "auto-approve on for this session (approval floors still apply)",
      "auto-approve off",
    ]);
    resetUi({ sid: null });
    await toggleYolo(bridge.rpc, uiStore, (text) => lines.push(text));
    expect(lines.at(-1)).toBe("auto-approve needs a session first");
  });
});

describe("model picker", () => {
  it("Ctrl+O opens the picker card the way /model does, leaving the draft alone", () => {
    const composer = new Composer();
    composer.setText("half-typed");
    overlayStore.setState({ card: null });
    openModelPicker();
    expect(overlayStore.getState().card).toMatchObject({
      kind: "modelPicker",
      ownsInput: true,
      payload: {},
    });
    expect(composer.text).toBe("half-typed");
    overlayStore.setState({ card: null });
  });
});

describe("mounted router", () => {
  it("routes real keystrokes: Ctrl+C clears a draft, then exits", async () => {
    const composer = new Composer();
    const { actions, calls } = spy(composer);
    function Router(): ReactNode {
      useKeyRouter({
        actions,
        ui: uiStore,
        card: () => null,
        queue: { length: 0, editing: null },
        now: () => 0,
        viewportRows: () => 24,
      });
      return null;
    }
    const harness = await renderInk(createElement(Router));
    await harness.type("draft");
    expect(composer.text).toBe("draft");
    await harness.type("\u0003");
    expect(composer.text).toBe("");
    await harness.type("\u0003");
    expect(calls).toEqual(["clear", "exit"]);
    harness.unmount();
  });
});
