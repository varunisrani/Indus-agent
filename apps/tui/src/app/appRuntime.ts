// Everything the TUI does that is not drawing (P12-27): one object that owns the client wiring, the
// turn controller, the server-request cards, submission and the queue, slash dispatch, completion,
// config sync, the session lifecycle and crash recovery. `App.tsx` renders from the stores this
// fills; tests drive it against the fake server without Ink.

import { writeFile } from "node:fs/promises";
import {
  type ClientEvent,
  type Clock,
  type CloseInfo,
  type ConnectHooks,
  type IndusClient,
  systemClock,
  type Transport,
} from "@indus/client";
import type { CatalogEntry, CommandsCatalogResult, SessionRow } from "@indus/protocol";
import { askConfirm, openPager } from "../cards/openCards.ts";
import { createServerRequestCards, type ServerRequestCards } from "../cards/serverRequests.ts";
import { detailsStore, readOverrides } from "../components/details.ts";
import { readStatusFields } from "../components/statusSegments.ts";
import { scrollBy, scrollToBottom } from "../components/virtualRows.ts";
import { readClipboard, type TerminalQuery, writeClipboard } from "../composer/clipboard.ts";
import { CompletionController, launchCwd, sessionCwd } from "../composer/completion.ts";
import { Composer } from "../composer/composerStore.ts";
import { type EditorOptions, editComposer } from "../composer/externalEditor.ts";
import { InputHistory } from "../composer/history.ts";
import { handlePaste, type PasteDeps, pasteFromClipboard } from "../composer/paste.ts";
import { type ConfigExtras, ConfigSync, DEFAULT_EXTRAS } from "../config/configSync.ts";
import { createEventHandler, type EventHandler } from "../events/handleEvent.ts";
import { type KeyActions, toggleYolo } from "../input/routeKeys.ts";
import type { SwitcherOps } from "../pickers/SessionSwitcher.tsx";
import type { LiveInput } from "../pickers/sessionSwitcherState.ts";
import { createDispatcher, type Dispatcher, findLocal } from "../slash/dispatch.ts";
import { LOCAL_COMMANDS } from "../slash/localCommands.ts";
import type { SlashContext } from "../slash/types.ts";
import { captureDestination, isCurrent } from "../state/destination.ts";
import { type Card, overlayStore } from "../state/overlayStore.ts";
import { transcriptStore } from "../state/transcriptStore.ts";
import { STATUS_READY, uiStore } from "../state/uiStore.ts";
import { PromptQueue } from "../submit/queue.ts";
import { Submitter } from "../submit/submission.ts";
import type { ExitReason } from "../terminal/lifecycle.ts";
import { attachFailureLines } from "../transport/attach.ts";
import { TurnController } from "../turn/controller.ts";
import type { Env } from "./launchOptions.ts";
import { type ExitInfo, Recovery } from "./recovery.ts";
import { type Rpc, rpcFromClient } from "./rpc.ts";
import { type OpenReason, SessionLifecycle } from "./sessionLifecycle.ts";

/** The feature our server-request cards answer (the bridge's `SERVER_REQUESTS_FEATURE`). */
export const CLIENT_FEATURES: readonly string[] = ["server_requests"];

/**
 * Watches the transports a client makes and reports when the current one closes. Closes of a
 * transport that was already replaced (our own restart) are not losses.
 */
export class LinkWatch {
  private current: Transport | null = null;
  private listener: ((info: CloseInfo) => void) | null = null;

  /** Wrap a client's `connect` factory. */
  wrap(connect: (hooks: ConnectHooks) => Transport): (hooks: ConnectHooks) => Transport {
    return (hooks) => {
      const transport = connect(hooks);
      this.current = transport;
      transport.onClose((info) => {
        if (this.current !== transport) return;
        this.current = null;
        // After the channel and client have seen the close too.
        queueMicrotask(() => this.listener?.(info));
      });
      return transport;
    };
  }

  onLost(listener: ((info: CloseInfo) => void) | null): void {
    this.listener = listener;
  }
}

export interface RuntimeOptions {
  client: IndusClient;
  links: LinkWatch;
  env: Env;
  clock?: Clock;
  /** Leave the program (the entry's `Lifecycle.quit`). */
  exit(code: number, reason: ExitReason): void;
  redraw?(): void;
  /** Hand the terminal to an external program and take it back. */
  suspend?: EditorOptions["suspend"];
  /** Ask the terminal something and read the reply (OSC 52 clipboard read). */
  query?: TerminalQuery;
  /** Write a raw sequence to the terminal (bell, OSC 52 write). */
  emit?(sequence: string): void;
  history?: { entries(): readonly string[]; push(text: string): unknown };
  themePinned?: boolean;
  newKey?(): string;
  writeFile?(path: string, text: string): void;
}

const BELL = "\u0007";

export class AppRuntime {
  readonly client: IndusClient;
  readonly rpc: Rpc;
  readonly controller: TurnController;
  readonly composer = new Composer();
  readonly queue = new PromptQueue();
  readonly submitter: Submitter;
  readonly dispatcher: Dispatcher;
  readonly cards: ServerRequestCards;
  readonly completion: CompletionController;
  readonly configSync: ConfigSync;
  readonly lifecycle: SessionLifecycle;
  readonly recovery: Recovery;
  readonly keyActions: KeyActions;
  readonly switcherOps: SwitcherOps;
  readonly history: { entries(): readonly string[]; push(text: string): unknown };
  private readonly options: RuntimeOptions;
  private readonly clock: Clock;
  private readonly handle: EventHandler;
  private readonly infos = new Map<string, Record<string, unknown>>();
  private catalog: CatalogEntry[] | null = null;
  private extras: ConfigExtras = DEFAULT_EXTRAS;
  private lastExit: ExitInfo | null = null;
  /** The session open when the link went down (undefined: not parked). */
  private parked: string | null | undefined = undefined;
  private readonly cleanups: (() => void)[] = [];
  private disposed = false;
  private configStarted = false;
  private switcherSerial = 0;
  private pickerSerial = 0;
  /** Bumped on every ready frame; a bootstrap of an older frame stops early. */
  private readyCount = 0;

  constructor(options: RuntimeOptions) {
    this.options = options;
    this.client = options.client;
    this.clock = options.clock ?? systemClock;
    this.rpc = rpcFromClient(options.client);
    this.history = options.history ?? new InputHistory();
    const system = (text: string): void => this.system(text);

    this.controller = new TurnController({ rpc: this.rpc, clock: this.clock });

    this.cards = createServerRequestCards({
      rpc: this.rpc,
      ringBell: () => this.options.emit?.(BELL),
      bellOnPrompt: () => this.extras.bellOnPrompt,
      systemLine: system,
    });
    this.client.handle(this.cards.handlers);

    this.submitter = new Submitter({
      rpc: this.rpc,
      composer: this.composer,
      queue: this.queue,
      history: this.history,
      ui: uiStore,
      clock: this.clock,
      capture: captureDestination,
      isCurrent,
      runSlash: (command) => void this.dispatcher.run(command),
      interrupt: (o) => void this.controller.interrupt(o),
      system,
      userLine: (text) => this.userLine(text),
      isKnownCommand: (name) =>
        findLocal(name, LOCAL_COMMANDS) !== undefined ||
        (this.catalog ?? []).some((entry) => entry.name === name),
    });
    this.cleanups.push(this.submitter.watch());

    this.dispatcher = createDispatcher(this.slashContext());

    this.completion = new CompletionController({
      rpc: this.rpc,
      composer: this.composer,
      clock: this.clock,
      context: () => ({
        sessionId: uiStore.getState().sid,
        sessionCwd: sessionCwd(uiStore.getState().info),
        launchCwd: launchCwd(this.options.env as NodeJS.ProcessEnv),
      }),
      blocked: () => overlayStore.getState().blocked(),
    });
    this.cleanups.push(this.composer.subscribe(() => this.completion.update()));

    this.configSync = new ConfigSync({
      rpc: this.rpc,
      clock: this.clock,
      onExtras: (extras) => {
        this.extras = extras;
        detailsStore.getState().set({ overrides: readOverrides(extras.detailSections ?? {}) });
      },
      onMcpReloaded: (message) => this.controller.pushActivity(message),
    });

    this.lifecycle = new SessionLifecycle({
      port: {
        call: (method, params) => this.rpc.call(method, params),
        resume: (params) => this.client.resume(params),
      },
      env: options.env,
      resetTurn: () => this.controller.reset(),
      system,
      userLine: (text) => this.userLine(text),
      infoFor: (id) => this.infos.get(id) ?? null,
      onOpened: (id, reason) => this.opened(id, reason),
      ...(options.newKey ? { newKey: options.newKey } : {}),
      ...(options.writeFile ? { writeFile: options.writeFile } : {}),
    });

    this.recovery = new Recovery({
      clock: this.clock,
      restart: () => {
        if (!this.disposed) this.client.start();
      },
      say: system,
      status: (text) => this.controller.setStatus(text),
    });

    this.handle = createEventHandler({
      controller: this.controller,
      sys: system,
      onReady: () => void this.onReady(),
      ...(options.themePinned === undefined ? {} : { themePinned: options.themePinned }),
    });
    this.cleanups.push(this.client.subscribe((event) => this.onEvent(event)));
    options.links.onLost((info) => this.linkLost(info));

    this.keyActions = this.makeKeyActions();
    this.switcherOps = this.makeSwitcherOps();
  }

  /** Start the first child. */
  start(): void {
    this.client.start();
  }

  /** Stop timers and restarts before the program leaves (the child is stopped by the entry). */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.options.links.onLost(null);
    this.recovery.stop();
    this.configSync.stop();
    this.completion.dispose();
    for (const cleanup of this.cleanups.splice(0)) cleanup();
  }

  /** Leave the program through the entry's exit path. */
  exit(code: number, reason: ExitReason = "quit"): void {
    this.dispose();
    this.options.exit(code, reason);
  }

  /** What the status rule shows, from config (`display.status_bar.fields`). */
  get statusFields(): ReadonlySet<string> | null {
    return readStatusFields(this.extras.statusBarFields);
  }

  system(text: string): void {
    transcriptStore.getState().append({ role: "system", text });
  }

  private userLine(text: string): void {
    transcriptStore.getState().append({ role: "user", text });
  }

  // -- events ------------------------------------------------------------------------------------

  private onEvent(event: ClientEvent): void {
    if (event.type === "session.info") {
      const { type: _type, seq: _seq, ...info } = event;
      this.infos.set(event.session_id, info);
      this.lifecycle.noteInfo(event.session_id, info);
    } else if (event.type === "transport.exit") {
      this.lastExit = { code: event.code, signal: event.signal, stderrLast: event.stderr_last };
      if (!this.disposed) this.park();
    } else if (event.type === "tool.completed" && event.name === "clarify") {
      this.cards.flushOpenClarify();
    } else if (event.type === "transport.attach_failed") {
      // P17-19: a terminal verdict is said once (with its repair hint); a retry is a status.
      // With a local fallback a session opens next, which clears the transcript: carry it.
      if (event.terminal && event.fallback) this.lifecycle.carry(attachFailureLines(event));
      else if (event.terminal) this.system(attachFailureLines(event));
      else this.controller.setStatus(`attach failed (${event.code}); retrying…`, 4_000);
    }
    this.handle(event);
  }

  private async onReady(): Promise<void> {
    if (this.disposed) return;
    this.readyCount += 1;
    const mine = this.readyCount;
    this.lastExit = null;
    const target = this.recovery.ready();
    try {
      // `surface` tells a daemon this viewer is the TUI (busy default queue, presence, P17-08).
      await this.rpc.call("client.capabilities", {
        features: [...CLIENT_FEATURES],
        surface: "tui",
      });
    } catch {
      // an older bridge without the method: cards still work for whatever it sends
    }
    if (mine !== this.readyCount || this.disposed) return;
    void this.loadCatalog();
    const result = await this.lifecycle.bootstrap(target);
    if (mine !== this.readyCount || this.disposed) return;
    if (result.ok && !this.configStarted) {
      this.configStarted = true;
      void this.configSync.start();
    }
  }

  private async loadCatalog(): Promise<void> {
    try {
      const sid = uiStore.getState().sid;
      const result = (await this.rpc.call(
        "commands.catalog",
        sid === null ? {} : { session_id: sid },
      )) as CommandsCatalogResult | null;
      this.catalog = result?.commands ?? null;
    } catch {
      // slash dispatch falls back to the bridge for unknown names
    }
  }

  private opened(id: string, reason: OpenReason): void {
    scrollToBottom();
    // Opening the recovery target ends the recovery; opening another session on purpose drops it.
    this.recovery.settled(reason === "recover" ? id : null);
    if (reason !== "recover") void this.loadCatalog();
  }

  /**
   * Detach the stores from the session before anything else reacts to the loss: with no session
   * open, the busy → idle settle cannot drain queued prompts into a dead or brand-new child.
   */
  private park(): void {
    if (this.parked !== undefined) return;
    const ui = uiStore.getState();
    this.parked = ui.storedSid ?? ui.sid;
    ui.patch({
      sid: null,
      storedSid: null,
      revision: ui.revision + 1,
      busy: false,
      compacting: false,
      connected: false,
    });
  }

  /**
   * The link is gone: reset, park the session, restart within the budget. A terminal close (an
   * attach verdict no retry can fix, P17-19) stops here: the reason was already said.
   */
  private linkLost(info?: CloseInfo): void {
    if (this.disposed) return;
    this.park();
    const live = this.parked ?? null;
    this.parked = undefined;
    const exit = this.lastExit;
    this.lastExit = null;
    this.controller.reset();
    this.cards.settleAll();
    overlayStore.getState().closeFlowCards();
    if (info?.terminal === true) {
      this.recovery.stop();
      this.controller.setStatus(DAEMON_UNAVAILABLE_STATUS);
      return;
    }
    this.recovery.lost(live, exit);
  }

  // -- slash hooks -------------------------------------------------------------------------------

  private slashContext(): SlashContext {
    return {
      rpc: this.rpc,
      ui: uiStore,
      overlay: overlayStore,
      systemLine: (text) => this.system(text),
      page: (text, title) => openPager(text, title),
      submit: (body, shown) => {
        this.queue.addFront(this.queue.make(body, shown ?? body, []));
        this.submitter.drain();
      },
      prefill: (text) => this.composer.setText(text),
      confirm: (options) => askConfirm(options),
      exit: (code) => this.exit(code, code === 42 ? "update" : "quit"),
      catalog: () => this.catalog,
      env: this.options.env,
      later: (run, ms) => void this.clock.setTimeout(run, ms),
      newSession: (title) => void this.lifecycle.newSession(title),
      newLiveSession: (title) => void this.lifecycle.newSession(title, true),
      switchSession: (id) => void this.lifecycle.switchTo(id),
      openSessions: () => this.openSwitcher(),
      redraw: () => this.options.redraw?.(),
      copy: (arg) => void this.copyReply(arg),
      paste: () => this.keyActions.pasteClipboard(),
      openEditor: (seed) => {
        if (seed) this.composer.setText(seed);
        this.keyActions.openExternalEditor();
      },
      logLines: () => this.client.logs.tail(200),
      transcriptText: () =>
        transcriptStore
          .getState()
          .messages.map((m) => `${m.role}: ${m.text}`)
          .join("\n\n"),
      enqueue: (text) => void this.queue.add(text),
      queued: () => this.queue.items.map((item) => item.display),
      writeFile: (path, text) =>
        this.options.writeFile
          ? Promise.resolve(this.options.writeFile(path, text))
          : writeFile(path, text, { mode: 0o600 }),
    };
  }

  /** `/copy [n]`: the n-th newest reply (default the newest) to the clipboard. */
  private async copyReply(arg: string): Promise<void> {
    const n = Math.max(1, Number.parseInt(arg, 10) || 1);
    const replies = transcriptStore
      .getState()
      .messages.filter((m) => m.role === "assistant" && m.text.trim() !== "");
    const reply = replies[replies.length - n];
    if (reply === undefined) {
      this.system("Nothing to copy yet.");
      return;
    }
    const ok = await this.copy(reply.text);
    this.system(ok ? "Copied the reply." : "No clipboard took the text.");
  }

  private copy(text: string): Promise<boolean> {
    const emit = this.options.emit;
    return writeClipboard(text, emit ? { emit } : {});
  }

  // -- keys and pickers --------------------------------------------------------------------------

  /** Ctrl+X, `/sessions`: the session switcher card. */
  openSwitcher(): void {
    this.switcherSerial += 1;
    overlayStore.getState().open({
      id: `sessions:${this.switcherSerial}`,
      kind: "sessionPicker",
      payload: {},
      ownsInput: true,
    });
  }

  /** Ctrl+O: the model picker, as `/model` opens it. */
  openModelPicker(): void {
    this.pickerSerial += 1;
    overlayStore.getState().open({
      id: `model-picker:key:${this.pickerSerial}`,
      kind: "modelPicker",
      payload: { sessionId: uiStore.getState().sid, refresh: false },
      ownsInput: true,
    });
  }

  private pasteDeps(): PasteDeps {
    return {
      rpc: this.rpc,
      composer: this.composer,
      sessionId: () => uiStore.getState().sid,
      capture: captureDestination,
      isCurrent,
      limits: () => ({
        lines: uiStore.getState().pasteLines,
        chars: uiStore.getState().pasteChars,
      }),
    };
  }

  private makeKeyActions(): KeyActions {
    return {
      composer: this.composer,
      submitter: this.submitter,
      history: this.history,
      interrupt: () => void this.controller.interrupt({ keepBusy: this.queue.length > 0 }),
      exit: () => this.exit(0, "quit"),
      cancelCard: (card: Pick<Card, "id">) => overlayStore.getState().close(card.id),
      scroll: (rows) => scrollBy(rows),
      redraw: () => this.options.redraw?.(),
      openModelPicker: () => this.openModelPicker(),
      openSessionSwitcher: () => this.openSwitcher(),
      openExternalEditor: () => {
        const suspend = this.options.suspend;
        void editComposer(
          this.composer,
          (text) => void this.submitter.dispatch(text),
          suspend ? { suspend } : {},
        ).then((result) => {
          if (result.status === "failed") this.system(`editor did not start: ${result.error}`);
        });
      },
      pasteClipboard: () => {
        const query = this.options.query;
        void pasteFromClipboard(
          this.pasteDeps(),
          () => readClipboard(query ? { query } : {}),
          (text) => this.controller.setStatus(text, 3_000),
        );
      },
      pasteText: (text) => void handlePaste(text, this.pasteDeps()),
      toggleYolo: () => void toggleYolo(this.rpc, uiStore, (text) => this.system(text)),
      copy: (text) => this.copy(text),
    };
  }

  private makeSwitcherOps(): SwitcherOps {
    const lifecycle = this.lifecycle;
    return {
      live: (): LiveInput[] => {
        const ui = uiStore.getState();
        return lifecycle.liveSessions().map((session) => {
          const info = this.infos.get(session.id);
          const working = session.id === ui.sid ? ui.busy : info?.running === true;
          return {
            id: session.id,
            title: session.title,
            model: session.model,
            status: working ? "working" : "idle",
          };
        });
      },
      stored: (): Promise<SessionRow[]> => lifecycle.listStored(),
      currentId: () => uiStore.getState().sid,
      busy: () => uiStore.getState().busy,
      open: (id) => void lifecycle.switchTo(id),
      newSession: (firstPrompt) => {
        void lifecycle.newSession(undefined, true).then((result) => {
          if (result?.ok && firstPrompt) this.submitter.dispatch(firstPrompt);
        });
      },
      closeLive: (id) => lifecycle.closeLive(id),
      remove: (id) => lifecycle.deleteStored(id),
    };
  }
}

/** Status text once attaching failed for good. */
export const DAEMON_UNAVAILABLE_STATUS = "agent daemon unavailable";

/** Status text while nothing is open yet. */
export const STATUS_IDLE = STATUS_READY;
