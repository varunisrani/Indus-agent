// The global key router (P12-20). Every key passes `decideKey` first: a pure function of the key,
// a snapshot of the screen state and a little memory (the last Esc). It returns what should
// happen; `performKey` makes it happen through injected actions, and `useKeyRouter` wires both to
// Ink. Priorities (R05.5, R05.11):
// - Ctrl+C: clear a draft → else interrupt a running turn → else quit.
// - Ctrl+D: quit only from a completely empty composer (text, continuation, tokens).
// - Esc Esc (≤ 500 ms): discard a draft — even under a card — else interrupt, but never under a
//   blocking card. A single Esc never stops work.
// - A card that owns input (`ownsInput`) handles its own keys, Ctrl+C included. Under a prompt
//   card (approval, confirm, clarify, sudo, secret) the scroll keys still reach the transcript, so
//   the context the prompt asks about stays readable. A card that does not own input leaves us
//   Ctrl+C (its cancel) and the scroll keys; everything else is the card's.

import { useInput, usePaste } from "ink";
import { useRef } from "react";
import type { StoreApi } from "zustand/vanilla";
import type { Rpc } from "../app/rpc.ts";
import type { Composer } from "../composer/composerStore.ts";
import { DOUBLE_ESC_MS, isDouble, lineAbove, lineBelow } from "../composer/editorModel.ts";
import { type Card, type CardKind, overlayStore } from "../state/overlayStore.ts";
import type { UiState } from "../state/uiStore.ts";
import type { Submitter } from "../submit/submission.ts";
import { fromInk, isAction, isAlt, isCtrl, isMac, isScrollKey, type KeyPress } from "./keymap.ts";

export interface KeyView {
  readonly busy: boolean;
  readonly hasSession: boolean;
  /** The open card, if any. */
  readonly card: Pick<Card, "id" | "kind" | "ownsInput"> | null;
  /** Text, continuation lines or tokens in the composer. */
  readonly hasDraft: boolean;
  readonly textEmpty: boolean;
  readonly continuationLines: number;
  readonly multiline: boolean;
  readonly cursorOnFirstLine: boolean;
  readonly cursorOnLastLine: boolean;
  readonly completionOpen: boolean;
  readonly historyBrowsing: boolean;
  readonly queueLength: number;
  readonly queueEditing: boolean;
  readonly hasSelection: boolean;
  readonly viewportRows: number;
}

export interface KeyMemory {
  lastEsc: number;
}

export type KeyDecision =
  | { readonly action: "clear-draft" }
  | { readonly action: "discard-draft" }
  | { readonly action: "interrupt" }
  | { readonly action: "exit" }
  | { readonly action: "cancel-card" }
  | { readonly action: "card" }
  | { readonly action: "scroll"; readonly rows: number }
  | { readonly action: "completion-cycle"; readonly step: 1 | -1 }
  | { readonly action: "completion-apply" }
  | { readonly action: "completion-close" }
  | { readonly action: "queue-cycle"; readonly step: 1 | -1 }
  | { readonly action: "history"; readonly step: 1 | -1 }
  | { readonly action: "cancel-queue-edit" }
  | { readonly action: "clear-selection" }
  | { readonly action: "copy-selection" }
  | { readonly action: "cut-selection" }
  | { readonly action: "delete-queued" }
  | { readonly action: "session-switcher" }
  | { readonly action: "model-picker" }
  | { readonly action: "redraw" }
  | { readonly action: "external-editor" }
  | { readonly action: "paste" }
  | { readonly action: "toggle-yolo" }
  | { readonly action: "send-queued" }
  | { readonly action: "to-editor" }
  | { readonly action: "none" };

export const WHEEL_ROWS = 3;

/** Cards that ask a question about the transcript: scrolling stays ours under them. */
const PROMPT_CARDS: ReadonlySet<CardKind> = new Set([
  "approval",
  "confirm",
  "clarify",
  "sudo",
  "secret",
]);

let pickerCount = 0;

/** Ctrl+O: the model picker, opened the same way `/model` opens it; the draft is untouched. */
export function openModelPicker(): void {
  pickerCount += 1;
  overlayStore.getState().open({
    id: `model-picker-${pickerCount}`,
    kind: "modelPicker",
    payload: {},
    ownsInput: true,
  });
}

/** Rows a scroll key moves the transcript, or null when `k` is not a scroll key. */
export function scrollRows(k: KeyPress, viewportRows: number): number | null {
  if (!isScrollKey(k)) return null;
  if (k.name === "wheelup") return -WHEEL_ROWS;
  if (k.name === "wheeldown") return WHEEL_ROWS;
  if (k.name === "pageup" || k.name === "pagedown") {
    const half = Math.max(4, Math.floor(viewportRows / 2));
    return k.name === "pageup" ? -half : half;
  }
  return k.name === "up" ? -1 : 1;
}

/** What a key does, given the state. Pure: `memory.lastEsc` is the only thing it updates. */
export function decideKey(
  k: KeyPress,
  view: KeyView,
  memory: KeyMemory,
  now: number,
  mac: boolean = isMac,
): KeyDecision {
  if (k.name === "escape") {
    const double = isDouble(memory.lastEsc, now, DOUBLE_ESC_MS);
    memory.lastEsc = double ? Number.NEGATIVE_INFINITY : now;
    if (double) {
      if (view.hasDraft) return { action: "discard-draft" };
      if (view.busy && view.hasSession && view.card === null) return { action: "interrupt" };
    }
  }

  if (view.card !== null) {
    const rows = scrollRows(k, view.viewportRows);
    if (view.card.ownsInput) {
      return rows !== null && PROMPT_CARDS.has(view.card.kind)
        ? { action: "scroll", rows }
        : { action: "card" };
    }
    if (isCtrl(k, "c")) return { action: "cancel-card" };
    return rows === null ? { action: "card" } : { action: "scroll", rows };
  }

  if (
    view.completionOpen &&
    !view.textEmpty &&
    !view.historyBrowsing &&
    (k.name === "up" || k.name === "down") &&
    !k.shift
  ) {
    return { action: "completion-cycle", step: k.name === "up" ? -1 : 1 };
  }

  const rows = scrollRows(k, view.viewportRows);
  if (rows !== null) return { action: "scroll", rows };

  if (k.name === "escape") {
    if (view.queueEditing) return { action: "cancel-queue-edit" };
    if (view.hasSelection) return { action: "clear-selection" };
    if (view.completionOpen) return { action: "completion-close" };
    return { action: "none" };
  }

  if (k.name === "up" && view.continuationLines === 0 && view.cursorOnFirstLine) {
    return view.queueLength > 0
      ? { action: "queue-cycle", step: 1 }
      : { action: "history", step: -1 };
  }
  if (
    k.name === "down" &&
    view.continuationLines === 0 &&
    (view.cursorOnLastLine || view.historyBrowsing)
  ) {
    return view.queueLength > 0
      ? { action: "queue-cycle", step: -1 }
      : { action: "history", step: 1 };
  }

  if (mac && k.super && !k.ctrl && k.input.toLowerCase() === "c") {
    return view.hasSelection ? { action: "copy-selection" } : { action: "none" };
  }
  if (isCtrl(k, "x")) {
    if (view.hasSelection) return { action: "cut-selection" };
    if (view.queueEditing) return { action: "delete-queued" };
    return { action: "session-switcher" };
  }
  if (isAction(k, "o", mac)) return { action: "model-picker" };
  if (isCtrl(k, "c")) {
    if (view.hasDraft) return { action: "clear-draft" };
    if (view.busy && view.hasSession) return { action: "interrupt" };
    return { action: "exit" };
  }
  if (isAction(k, "d", mac)) return view.hasDraft ? { action: "none" } : { action: "exit" };
  if (isAction(k, "l", mac)) return { action: "redraw" };
  if (isAction(k, "g", mac) || isAlt(k, "g")) return { action: "external-editor" };
  if (isAction(k, "v", mac) || isAlt(k, "v")) return { action: "paste" };
  if (k.name === "tab" && k.shift) {
    return view.completionOpen ? { action: "none" } : { action: "toggle-yolo" };
  }
  if (k.name === "tab") {
    return view.completionOpen && !view.multiline
      ? { action: "completion-apply" }
      : { action: "none" };
  }
  if (isAction(k, "k", mac) && view.queueLength > 0 && view.hasSession)
    return { action: "send-queued" };
  return { action: "to-editor" };
}

/** Everything the decisions need to take effect. The app (P12-27) wires the real ones. */
export interface KeyActions {
  readonly composer: Composer;
  readonly submitter: Submitter;
  readonly history: { entries(): readonly string[]; push(text: string): unknown };
  interrupt(): void;
  exit(): void;
  cancelCard(card: Pick<Card, "id" | "kind" | "ownsInput">): void;
  scroll(rows: number): void;
  redraw(): void;
  openModelPicker(): void;
  openSessionSwitcher(): void;
  openExternalEditor(): void;
  /** Clipboard paste (Ctrl+V / Alt+V). */
  pasteClipboard(): void;
  /** Pasted text: bracketed pastes and multi-line text arriving as one key event. */
  pasteText(text: string): void;
  toggleYolo(): void;
  /** Copy text to the clipboard; resolves false when no clipboard took it. */
  copy(text: string): Promise<boolean>;
}

/** Several lines typed in one event: a paste the terminal did not bracket. */
export function isUnbracketedPaste(k: KeyPress): boolean {
  if (k.name !== null || k.ctrl || k.meta || k.super || k.input.length < 2) return false;
  return /[\r\n]/.test(k.input.replace(/[\r\n]+$/, ""));
}

/** Apply a decision. Returns true when the key was consumed (false: nobody wanted it). */
export function performKey(
  decision: KeyDecision,
  k: KeyPress,
  view: KeyView,
  actions: KeyActions,
): boolean {
  const { composer, submitter } = actions;
  switch (decision.action) {
    case "clear-draft":
      submitter.clearDraft();
      return true;
    case "discard-draft": {
      const draft = composer.fullDraft();
      if (draft.trim() !== "") actions.history.push(draft);
      submitter.clearDraft();
      return true;
    }
    case "interrupt":
      actions.interrupt();
      return true;
    case "exit":
      actions.exit();
      return true;
    case "cancel-card":
      if (view.card !== null) actions.cancelCard(view.card);
      return true;
    case "card":
      return false;
    case "scroll":
      actions.scroll(decision.rows);
      return true;
    case "completion-cycle":
      composer.cycleCompletion(decision.step);
      return true;
    case "completion-apply": {
      const row = composer.completion.items[composer.completion.index];
      if (row !== undefined && row.text !== "" && row.disabled !== true) {
        const { replaceFrom } = composer.completion;
        const piece =
          composer.text[replaceFrom - 1] === "/" && row.text.startsWith("/")
            ? row.text.slice(1)
            : row.text;
        composer.setText(composer.text.slice(0, replaceFrom) + piece);
      }
      return true;
    }
    case "completion-close":
      composer.clearCompletion();
      return true;
    case "queue-cycle":
      return submitter.cycleQueue(decision.step);
    case "history":
      if (decision.step < 0) composer.historyBack(actions.history.entries());
      else composer.historyForward(actions.history.entries());
      return true;
    case "cancel-queue-edit":
      submitter.clearDraft();
      return true;
    case "clear-selection":
      composer.editor.moveTo(composer.editor.cursor);
      composer.notify(false);
      return true;
    case "copy-selection":
      void actions.copy(composer.editor.selectedText());
      return true;
    case "cut-selection": {
      const text = composer.editor.selectedText();
      void actions.copy(text).then((ok) => {
        // Only remove what is safely on the clipboard; the selection may have moved meanwhile.
        if (ok && composer.editor.selectedText() === text && composer.editor.deleteSelection())
          composer.notify();
      });
      return true;
    }
    case "delete-queued":
      submitter.deleteEdited();
      return true;
    case "session-switcher":
      actions.openSessionSwitcher();
      return true;
    case "model-picker":
      actions.openModelPicker();
      return true;
    case "redraw":
      composer.editor.moveTo(composer.editor.cursor);
      actions.redraw();
      return true;
    case "external-editor":
      actions.openExternalEditor();
      return true;
    case "paste":
      actions.pasteClipboard();
      return true;
    case "toggle-yolo":
      actions.toggleYolo();
      return true;
    case "send-queued":
      submitter.sendNextNow();
      return true;
    case "none":
      return true;
    case "to-editor": {
      if (isUnbracketedPaste(k)) {
        actions.pasteText(k.input);
        return true;
      }
      const outcome = composer.key(k);
      if (outcome === "submit") submitter.submit();
      return outcome !== "ignored";
    }
  }
}

/** The view the router decides on, read from the stores at the moment of the key. */
export function snapshotView(
  ui: UiState,
  card: Pick<Card, "id" | "kind" | "ownsInput"> | null,
  composer: Composer,
  queue: { length: number; editing: number | null },
  viewportRows: number,
): KeyView {
  const { text, cursor } = composer.editor;
  return {
    busy: ui.busy,
    hasSession: ui.sid !== null,
    card,
    hasDraft: composer.hasDraft(),
    textEmpty: text === "",
    continuationLines: composer.continuation.length,
    multiline: text.includes("\n") || composer.continuation.length > 0,
    cursorOnFirstLine: lineAbove(text, cursor) === null,
    cursorOnLastLine: lineBelow(text, cursor) === null,
    completionOpen: composer.completion.items.length > 0,
    historyBrowsing: composer.historyIndex !== null,
    queueLength: queue.length,
    queueEditing: queue.editing !== null,
    hasSelection: composer.editor.range() !== null,
    viewportRows,
  };
}

/** Shift+Tab: flip auto-approve for this session through `config.set key=yolo`. */
export async function toggleYolo(
  rpc: Rpc,
  ui: StoreApi<UiState>,
  system: (text: string) => void,
): Promise<void> {
  const { sid, info } = ui.getState();
  if (sid === null) {
    system("auto-approve needs a session first");
    return;
  }
  const next = info?.yolo !== true;
  try {
    const reply = (await rpc.call("config.set", { key: "yolo", value: next, session_id: sid })) as {
      value?: unknown;
    };
    const on = reply?.value === true || reply?.value === "on" || reply?.value === "1";
    if (ui.getState().sid !== sid) return;
    ui.getState().patch({ info: { ...(ui.getState().info ?? {}), yolo: on } });
    system(
      on ? "auto-approve on for this session (approval floors still apply)" : "auto-approve off",
    );
  } catch (error) {
    system(`auto-approve unchanged: ${error instanceof Error ? error.message : String(error)}`);
  }
}

export interface KeyRouterOptions {
  readonly actions: KeyActions;
  readonly ui: StoreApi<UiState>;
  readonly card: () => Pick<Card, "id" | "kind" | "ownsInput"> | null;
  readonly queue: { length: number; editing: number | null };
  readonly now: () => number;
  readonly viewportRows: () => number;
  readonly active?: boolean;
}

/** Mounts the router: one `useInput` for every key, one `usePaste` for pastes. */
export function useKeyRouter(options: KeyRouterOptions): void {
  const memory = useRef<KeyMemory>({ lastEsc: Number.NEGATIVE_INFINITY });
  const active = options.active ?? true;
  useInput(
    (input, key) => {
      const k = fromInk(input, key);
      const view = snapshotView(
        options.ui.getState(),
        options.card(),
        options.actions.composer,
        options.queue,
        options.viewportRows(),
      );
      performKey(decideKey(k, view, memory.current, options.now()), k, view, options.actions);
    },
    { isActive: active },
  );
  usePaste(
    (text) => {
      if (options.card() === null) options.actions.pasteText(text);
    },
    { isActive: active },
  );
}
