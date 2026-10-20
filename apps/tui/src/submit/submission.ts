// What Enter does (P12-19). The composer text goes through one pipeline: take a highlighted
// completion, handle an empty Enter (double press interrupts a running turn; idle with a queue it
// sends the next item), move a `\`-ended line to the continuation, then dispatch: slash commands
// to the slash dispatcher, `!cmd` to `shell.exec`, everything else to `prompt.submit` — or, while a
// turn runs, to the busy mode (queue / steer / interrupt). `{!cmd}` expands only when a prompt
// actually leaves; collapsed pastes expand to their full text.

import { type Clock, GatewayRpcError } from "@indus/client";
import type { StoreApi } from "zustand/vanilla";
import type { Rpc } from "../app/rpc.ts";
import { completionOnEnter, isSlashCommandText } from "../composer/completion.ts";
import type { Composer } from "../composer/composerStore.ts";
import { expandPastes, expandTokens } from "../composer/paste.ts";
import type { Destination } from "../state/destination.ts";
import { steerParams } from "../state/turnStore.ts";
import type { UiState } from "../state/uiStore.ts";
import { hasInterpolation, interpolate, shellOutput } from "./interpolate.ts";
import type { PromptQueue, QueueItem } from "./queue.ts";

export const DOUBLE_ENTER_MS = 450;

export interface SubmitDeps {
  readonly rpc: Rpc;
  readonly composer: Composer;
  readonly queue: PromptQueue;
  readonly history: { push(text: string): unknown };
  readonly ui: StoreApi<UiState>;
  readonly clock: Clock;
  capture(): Destination;
  isCurrent(dest: Destination): boolean;
  /** The slash dispatcher (P12-24): `command` has pastes expanded, `display` is what was typed. */
  runSlash(command: string, display: string): void;
  /** The turn controller's interrupt (P12-11); `keepBusy` while queued prompts wait to drain. */
  interrupt(options: { keepBusy: boolean }): void;
  /** A system line in the transcript. */
  system(text: string): void;
  /** The user's own line in the transcript, shown when a prompt leaves. */
  userLine(text: string): void;
  /** A command the renderer knows by this exact name (Enter then submits instead of completing). */
  isKnownCommand?(name: string): boolean;
}

export type SubmitOutcome =
  | "completed"
  | "interrupted"
  | "drained"
  | "continued"
  | "slash"
  | "shell"
  | "queued"
  | "steering"
  | "redirecting"
  | "sent"
  | "ignored";

function messageOf(error: unknown): string {
  return error instanceof Error && error.message ? error.message : String(error);
}

function isBusyRefusal(error: unknown): boolean {
  return error instanceof GatewayRpcError && error.errorName === "SESSION_BUSY";
}

export class Submitter {
  private lastEmptyEnter = Number.NEGATIVE_INFINITY;
  private readonly shown = new Set<number>();
  private readonly deps: SubmitDeps;

  constructor(deps: SubmitDeps) {
    this.deps = deps;
  }

  private get state(): UiState {
    return this.deps.ui.getState();
  }

  /** Enter in the composer. */
  submit(): SubmitOutcome {
    const { composer, queue } = this.deps;
    const value = composer.text;

    const { items, index, replaceFrom } = composer.completion;
    if (items.length > 0) {
      const next = completionOnEnter(value, items[index], replaceFrom, this.deps.isKnownCommand);
      if (next !== null) {
        composer.setText(next);
        composer.clearCompletion();
        return "completed";
      }
    }

    if (value.trim() === "" && composer.continuation.length === 0) {
      const now = this.deps.clock.now();
      const double = now - this.lastEmptyEnter < DOUBLE_ENTER_MS;
      this.lastEmptyEnter = now;
      const { busy, sid } = this.state;
      if (double && busy && sid !== null) {
        this.lastEmptyEnter = Number.NEGATIVE_INFINITY;
        this.deps.interrupt({ keepBusy: queue.length > 0 });
        return "interrupted";
      }
      if (!busy && sid !== null && queue.editing === null && this.sendNextNow()) return "drained";
      return "ignored";
    }
    this.lastEmptyEnter = Number.NEGATIVE_INFINITY;

    if (value.endsWith("\\")) {
      composer.pushContinuation(value.slice(0, -1));
      return "continued";
    }
    return this.dispatch(composer.fullDraft());
  }

  /** Clear the composer and leave queue editing (Ctrl+C clear, Esc Esc, Esc in an edit). */
  clearDraft(): void {
    this.deps.composer.clear();
    if (this.deps.queue.editing !== null) this.deps.queue.setEditing(null);
  }

  /** Dispatch a complete draft (also used by the external editor's saved text). */
  dispatch(full: string): SubmitOutcome {
    const { composer, queue, history } = this.deps;
    if (full.trim() === "") return "ignored";
    const tokens = [...composer.tokens];
    const text = expandTokens(full, tokens);

    if (isSlashCommandText(full)) {
      history.push(text);
      this.clearDraft();
      this.deps.runSlash(expandPastes(full, tokens), full);
      return "slash";
    }
    if (full.startsWith("!")) {
      history.push(full);
      this.clearDraft();
      void this.shell(full.slice(1).trim());
      return "shell";
    }

    history.push(text);
    const editing = queue.editing;
    this.deps.composer.clear();
    if (editing !== null) {
      queue.setEditing(null);
      const item = queue.takeEdited(editing, text, full, tokens);
      if (item === undefined) return "ignored";
      if (this.state.busy || this.state.sid === null) {
        queue.addFront(item);
        return "queued";
      }
      void this.send(item);
      return "sent";
    }

    const item = queue.make(text, full, tokens);
    if (this.state.sid === null) {
      queue.push(item);
      return "queued";
    }
    if (this.state.busy) return this.whileBusy(item);
    void this.send(item);
    return "sent";
  }

  private whileBusy(item: QueueItem): SubmitOutcome {
    // An image cannot ride along as a correction to a running turn: it waits for its own turn.
    if (item.hasImage) {
      this.deps.queue.push(item);
      return "queued";
    }
    switch (this.state.busyInputMode) {
      case "steer":
        void this.steer(item);
        return "steering";
      case "interrupt":
        void this.send(item, "redirect");
        return "redirecting";
      default:
        this.deps.queue.push(item);
        return "queued";
    }
  }

  /** Expand `{!cmd}` through `shell.exec`; null (with a notice) when a command could not run. */
  private async expand(text: string, sid: string, dest: Destination): Promise<string | null> {
    if (!hasInterpolation(text)) return text;
    this.state.patch({ statusText: "running {!…}" });
    try {
      return await interpolate(text, async (command) =>
        shellOutput(
          (await this.deps.rpc.call("shell.exec", { command, session_id: sid })) as object,
        ),
      );
    } catch (error) {
      if (this.deps.isCurrent(dest)) {
        this.deps.system(`not sent: a {!…} command failed (${messageOf(error)})`);
        this.state.patch({ statusText: "" });
      }
      return null;
    }
  }

  private async steer(item: QueueItem): Promise<void> {
    const sid = this.state.sid;
    if (sid === null) {
      this.deps.queue.push(item);
      return;
    }
    const dest = this.deps.capture();
    const text = await this.expand(item.text, sid, dest);
    if (text === null) return;
    try {
      const reply = (await this.deps.rpc.call("session.steer", steerParams(sid, text))) as {
        queued?: unknown;
      };
      if (reply?.queued === true) {
        if (this.deps.isCurrent(dest)) this.deps.userLine(item.display);
        return;
      }
    } catch {
      // refused or unsupported: it becomes an ordinary queued prompt
    }
    this.deps.queue.push(item);
    this.drain();
  }

  private async send(item: QueueItem, whenBusy?: "redirect"): Promise<void> {
    const sid = this.state.sid;
    if (sid === null) {
      this.deps.queue.addFront(item);
      return;
    }
    const dest = this.deps.capture();
    const wasBusy = this.state.busy;
    if (!wasBusy) this.state.patch({ busy: true, statusText: "sending…" });
    const text = await this.expand(item.text, sid, dest);
    if (text === null) {
      if (!wasBusy && this.deps.isCurrent(dest)) this.state.patch({ busy: false });
      return;
    }
    if (!this.shown.has(item.id)) {
      this.shown.add(item.id);
      this.deps.userLine(item.display);
    }
    try {
      await this.deps.rpc.call("prompt.submit", {
        session_id: sid,
        text,
        ...(whenBusy ? { when_busy: whenBusy } : {}),
      });
    } catch (error) {
      if (isBusyRefusal(error)) {
        // Lost a race with the turn's settle: wait for the next one instead of dropping it.
        this.deps.queue.addFront(item);
        this.state.patch({ busy: true });
        return;
      }
      if (this.deps.isCurrent(dest)) {
        this.deps.system(`error: ${messageOf(error)}`);
        this.state.patch({ busy: false, statusText: "" });
      }
    }
  }

  /** `!cmd`: run on the host, print the output, and `exit N` when non-zero or silent. */
  async shell(command: string): Promise<void> {
    const sid = this.state.sid;
    const dest = this.deps.capture();
    this.deps.userLine(`!${command}`);
    this.state.patch({ busy: true, statusText: "running…" });
    try {
      const reply = (await this.deps.rpc.call("shell.exec", {
        command,
        ...(sid !== null ? { session_id: sid } : {}),
      })) as { stdout?: unknown; stderr?: unknown; code?: unknown };
      if (!this.deps.isCurrent(dest)) return;
      const out = shellOutput(reply);
      const code = typeof reply?.code === "number" ? reply.code : 0;
      if (out !== "") this.deps.system(out);
      if (code !== 0 || out === "") this.deps.system(`exit ${code}`);
    } catch (error) {
      if (this.deps.isCurrent(dest)) this.deps.system(`error: ${messageOf(error)}`);
    } finally {
      if (this.deps.isCurrent(dest)) this.state.patch({ busy: false, statusText: "" });
    }
  }

  /** Send one queued prompt if the session is idle and nothing is being edited. */
  drain(): boolean {
    const { queue } = this.deps;
    const { busy, sid } = this.state;
    if (busy || sid === null || queue.editing !== null || queue.length === 0) return false;
    const item = queue.shift();
    if (item === undefined) return false;
    void this.send(item);
    return true;
  }

  /** Ctrl+K (and an idle empty Enter): the next queued prompt goes now. */
  sendNextNow(): boolean {
    const { queue } = this.deps;
    const { busy, sid } = this.state;
    if (sid === null || queue.length === 0) return false;
    const item = queue.shift();
    if (item === undefined) return false;
    if (queue.editing !== null) queue.setEditing(null);
    if (busy && item.hasImage) {
      queue.addFront(item);
      return false;
    }
    void this.send(item, busy ? "redirect" : undefined);
    return true;
  }

  /** Open queued item `index` in the composer for editing. */
  editQueued(index: number): boolean {
    const { queue, composer } = this.deps;
    const item = queue.items[index];
    if (item === undefined) return false;
    queue.setEditing(index);
    composer.historyIndex = null;
    composer.setText(item.display);
    composer.tokens = [...item.tokens];
    composer.notify(false);
    return true;
  }

  /** Up/Down through the queue (Up starts at the front). False when the queue is empty. */
  cycleQueue(step: 1 | -1): boolean {
    const { queue } = this.deps;
    const count = queue.length;
    if (count === 0) return false;
    const at =
      queue.editing === null ? (step > 0 ? 0 : count - 1) : (queue.editing + step + count) % count;
    return this.editQueued(at);
  }

  /** Ctrl+X while editing: the queued item is dropped and the composer cleared. */
  deleteEdited(): boolean {
    const { queue } = this.deps;
    if (queue.editing === null) return false;
    queue.removeAt(queue.editing);
    this.clearDraft();
    return true;
  }

  /** Drain on every settle (busy → idle) and when a session first appears. */
  watch(): () => void {
    let last = this.state;
    return this.deps.ui.subscribe((now) => {
      const settled = last.busy && !now.busy;
      const bound = last.sid === null && now.sid !== null;
      last = now;
      if (settled || bound) this.drain();
    });
  }
}
