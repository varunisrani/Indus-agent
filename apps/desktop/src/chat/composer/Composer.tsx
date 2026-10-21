// The composer (P20-19): where the next message is written.
//
// - completion: `/` at the start (slash, curated by P20-24) and `@word` (paths), arrows to move,
//   Tab/Enter to take, Esc to close; answers that arrive after a newer question are dropped;
// - attachments: dropped/pasted files travel as bytes; picker paths go as paths only in local mode
//   (R08.23, see `attachments.ts`);
// - Up/Down recall earlier prompts only while the composer is empty (or shows a recalled entry);
// - a per-conversation status stack (todos, the queue) above the editor, hidden by a ridge;
// - the model + effort pill (P20-23); Stop / Steer / Queue while a turn runs (P20-21);
// - a vanished session hands its unsent text to the fresh draft once, with an undoable strip.

import type { TodoItem } from "@indus/protocol";
import {
  type ChangeEvent,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import type { ChatConnection } from "../../gateway/connectionStore.ts";
import { useGatewayConnection } from "../../gateway/react.tsx";
import { useT } from "../../i18n/index.tsx";
import {
  ChatActions,
  type PreparedJournalPort,
  type Request,
  sharedActions,
} from "../busyActions.ts";
import type { InflightJournal } from "../inflightJournal.ts";
import { type LocalRows, sendShown } from "../localEcho.ts";
import { ModelPicker, ModelPrefs, modelKey } from "../ModelPicker.tsx";
import { QueuePanel, useBusySnapshot } from "../QueuePanel.tsx";
import { errorMessage, isUncertain } from "../rpcErrors.ts";
import { helpLines, isSlashLine, type LocalCommandName, runSlash } from "../slash.ts";
import {
  type Attachment,
  AttachmentError,
  attachmentFromPath,
  attachmentsFromFiles,
  bridgeGuardLocalPath,
  bridgeReadLocalBytes,
  bridgeUploadBytes,
  isImage,
  type StageDeps,
  type StagedAttachment,
  stageAttachment,
  withFileReferences,
} from "./attachments.ts";
import {
  applyCompletion,
  CompletionSource,
  type CompletionState,
  EMPTY_COMPLETION,
  PromptHistory,
} from "./completion.ts";
import {
  adoptGoneDraft,
  type DraftStore,
  dismissRestore,
  draftOf,
  FRESH,
  setDraft,
  undoRestore,
  verifySessionGone,
  windowDrafts,
} from "./goneSession.ts";

export interface ComposerProps {
  /** The session written to, or null for the window's fresh draft. */
  sessionId: string | null;
  profile?: string | null;
  connection?: ChatConnection | null;
  /** Tests inject; otherwise one per session is made over the connection. */
  actions?: ChatActions | null;
  journal?: PreparedJournalPort | null;
  inflight?: InflightJournal | null;
  drafts?: DraftStore;
  history?: PromptHistory;
  prefs?: ModelPrefs;
  /** The transcript's optimistic rows (the person's message shows before the daemon stores it). */
  localRows?: LocalRows | null;
  /** The session's folder (uploads land below it). */
  cwd?: string | null;
  /** The folder a fresh draft's session is created in (P20-25; null: detached). */
  newSessionCwd?: string | null;
  /** The fresh draft became a session. */
  onSessionCreated?(sessionId: string): void;
  /** The daemon confirmed the session is gone: show the fresh draft. */
  onSessionGone?(sessionId: string): void;
  /** `/new`. */
  onNewChat?(): void;
  /** `/reasoning show|hide` flips the transcript live. */
  onReasoningDisplay?(show: boolean): void;
  /** A system line in the conversation (slash output, notices). */
  onSystemLine?(text: string): void;
  /** The native file picker; resolves with chosen paths. */
  pickFiles?(): Promise<string[]>;
  attachmentDeps?: Partial<Pick<StageDeps, "guardLocalPath" | "readLocalBytes" | "uploadBytes">>;
  completionDebounceMs?: number;
}

const history = new PromptHistory();
const BUTTON =
  "rounded px-3 py-1 text-sm border border-line hover:bg-raised disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-focus";

function newKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `draft-${Date.now()}-${Math.random()}`;
}

function useDrafts(store: DraftStore) {
  return useSyncExternalStore(store.subscribe, store.snapshot, store.snapshot);
}

function TodoList({ todos }: { todos: readonly TodoItem[] }) {
  const t = useT();
  if (todos.length === 0) return null;
  return (
    <section aria-label={t("composer.todos")} className="px-3 py-2">
      <ul className="list-none m-0 p-0 text-sm">
        {todos.map((todo) => (
          <li key={todo.id} data-status={todo.status} className="flex gap-2">
            <span aria-hidden="true">
              {todo.status === "completed" ? "✓" : todo.status === "in_progress" ? "▸" : "○"}
            </span>
            <span className={todo.status === "completed" ? "line-through text-muted" : ""}>
              {todo.content}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Actions for a real session, wired to its events and journal. */
function useSessionActions(
  props: ComposerProps,
  connection: ChatConnection | null,
): ChatActions | null {
  const { sessionId, journal, inflight } = props;
  const made = useMemo(() => {
    if (props.actions !== undefined) return props.actions;
    if (connection === null || sessionId === null) return null;
    return sharedActions(
      connection,
      sessionId,
      () =>
        new ChatActions({
          sessionId,
          request: connection.request.bind(connection) as Request,
          ...(journal === undefined ? {} : { journal }),
          ...(inflight === undefined ? {} : { inflight }),
        }),
    );
  }, [props.actions, connection, sessionId, journal, inflight]);
  useEffect(() => {
    if (made === null || connection === null) return;
    const stop = connection.onSessionEvent(made.sessionId, (event) => made.handleEvent(event));
    void made.recoverPrepared();
    return stop;
  }, [made, connection]);
  return made;
}

const IDLE_ACTIONS = new ChatActions({
  sessionId: "",
  request: (() => Promise.reject(new Error("no session"))) as Request,
  journal: null,
});

export function Composer(props: ComposerProps) {
  const t = useT();
  const active = useGatewayConnection();
  const connection = props.connection === undefined ? active : props.connection;
  const store = props.drafts ?? windowDrafts;
  const drafts = useDrafts(store);
  const recall = props.history ?? history;
  const prefs = useMemo(() => props.prefs ?? new ModelPrefs(), [props.prefs]);
  const actions = useSessionActions(props, connection);
  const busy = useBusySnapshot(actions ?? IDLE_ACTIONS);
  const key = props.sessionId ?? FRESH;
  const text = draftOf(drafts, key);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [stackHidden, setStackHidden] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [todos, setTodos] = useState<readonly TodoItem[]>([]);
  const [completion, setCompletion] = useState<CompletionState>(EMPTY_COMPLETION);
  const [imageDraft, setImageDraft] = useState(() =>
    props.sessionId === null ? null : (props.inflight?.imageDraft(props.sessionId) ?? null),
  );
  const textarea = useRef<HTMLTextAreaElement>(null);
  const listId = useId();
  const composing = useRef(false);
  /** The fresh draft's create key: kept until the daemon answers definitely. */
  const createKey = useRef<string>(newKey());

  const request = useMemo(
    () => (connection === null ? null : (connection.request.bind(connection) as Request)),
    [connection],
  );

  const source = useMemo(
    () =>
      new CompletionSource({
        request,
        sessionId: props.sessionId,
        onChange: setCompletion,
        ...(props.completionDebounceMs === undefined
          ? {}
          : { debounceMs: props.completionDebounceMs }),
      }),
    [request, props.sessionId, props.completionDebounceMs],
  );
  useEffect(() => () => source.dispose(), [source]);

  useEffect(() => {
    setTodos([]);
    setImageDraft(
      props.sessionId === null ? null : (props.inflight?.imageDraft(props.sessionId) ?? null),
    );
    if (connection === null || props.sessionId === null) return;
    return connection.onSessionEvent(props.sessionId, (event) => {
      if (event.type === "todo.updated") setTodos(event.todos);
    });
  }, [connection, props.sessionId, props.inflight]);

  const system = useCallback((line: string) => props.onSystemLine?.(line), [props.onSystemLine]);

  const write = (next: string, caret?: number) => {
    store.update((state) => setDraft(state, key, next));
    const at = caret ?? next.length;
    source.update(next, at);
    if (caret !== undefined) {
      requestAnimationFrame(() => textarea.current?.setSelectionRange(caret, caret));
    }
  };

  const onChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    store.update((state) => setDraft(state, key, event.target.value));
    source.update(event.target.value, event.target.selectionStart ?? event.target.value.length);
  };

  // -- gone session ------------------------------------------------------------------------------

  const handleGone = useCallback(
    async (sessionId: string) => {
      if (request === null || !(await verifySessionGone(request, sessionId))) return false;
      store.update((state) => adoptGoneDraft(state, sessionId));
      props.onSessionGone?.(sessionId);
      return true;
    },
    [request, store, props.onSessionGone],
  );

  // -- attachments --------------------------------------------------------------------------------

  const stageAll = async (sessionId: string): Promise<StagedAttachment[]> => {
    if (attachments.length === 0 || connection === null || request === null) return [];
    const deps: StageDeps = {
      // Before main described the backend the mode is unknown: upload the bytes (works for both).
      mode: connection.mode ?? "remote",
      sessionId,
      cwd: props.cwd ?? null,
      request,
      guardLocalPath: props.attachmentDeps?.guardLocalPath ?? bridgeGuardLocalPath,
      readLocalBytes: props.attachmentDeps?.readLocalBytes ?? bridgeReadLocalBytes,
      uploadBytes: props.attachmentDeps?.uploadBytes ?? bridgeUploadBytes,
    };
    const staged: StagedAttachment[] = [];
    for (const attachment of attachments) staged.push(await stageAttachment(attachment, deps));
    return staged;
  };

  const addFiles = (files: Iterable<File>) => {
    const added = attachmentsFromFiles(files);
    if (added.length > 0) setAttachments((now) => [...now, ...added]);
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    if (event.dataTransfer.files.length === 0) return;
    event.preventDefault();
    addFiles(event.dataTransfer.files);
  };

  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    if (event.clipboardData.files.length === 0) return;
    event.preventDefault();
    addFiles(event.clipboardData.files);
  };

  const pick = async () => {
    if (props.pickFiles === undefined) return;
    const paths = await props.pickFiles();
    setAttachments((now) => [...now, ...paths.map((path) => attachmentFromPath(path))]);
  };

  // -- sending ------------------------------------------------------------------------------------

  const ensureSession = async (): Promise<string | null> => {
    if (props.sessionId !== null) return props.sessionId;
    if (request === null) return null;
    try {
      const folder = props.newSessionCwd ?? null;
      const created = await request("session.create", {
        idempotency_key: createKey.current,
        ...(folder === null ? {} : { cwd: folder }),
      });
      createKey.current = newKey();
      const sticky = prefs.sticky(props.profile ?? null);
      if (sticky !== null) {
        await request("config.set", {
          key: "model",
          value: modelKey(sticky),
          session_id: created.session_id,
        }).catch(() => {});
      }
      props.onSessionCreated?.(created.session_id);
      return created.session_id;
    } catch (error) {
      // An uncertain create keeps its key, so the retry returns the same session.
      if (!isUncertain(error)) createKey.current = newKey();
      setProblem(errorMessage(error));
      return null;
    }
  };

  const local: Record<LocalCommandName, (arg: string) => void | Promise<void>> = {
    new: () => props.onNewChat?.(),
    model: () => setPickerOpen(true),
    details: async (arg) => {
      if (request === null || props.sessionId === null) return;
      const value = arg.trim() === "" ? "all" : arg.trim();
      await request("config.set", { key: "details", value, session_id: props.sessionId });
      system(t("composer.detailsSet", { value }));
    },
    reasoning: async (arg) => {
      const word = arg.trim().toLowerCase();
      if (word === "show" || word === "hide") {
        props.onReasoningDisplay?.(word === "show");
        system(t(word === "show" ? "composer.reasoningShown" : "composer.reasoningHidden"));
        return;
      }
      if (request === null || props.sessionId === null || word === "") return;
      await request("config.set", { key: "reasoning", value: word, session_id: props.sessionId });
      system(t("composer.reasoningSet", { value: word }));
    },
    help: async () => {
      if (request === null) {
        system(t("composer.helpOffline"));
        return;
      }
      const catalog = await request("commands.catalog", { session_id: props.sessionId });
      system(helpLines(catalog.commands).join("\n"));
    },
  };

  const sendText = async (body: string, display: string): Promise<boolean> => {
    const sessionId = await ensureSession();
    if (sessionId === null || request === null) return false;
    // The fresh draft's first send uses the session's shared actions, which the composer
    // showing the new session picks up afterwards (one busy state, one journal bookkeeping).
    const make = () =>
      new ChatActions({
        sessionId,
        request,
        ...(props.journal === undefined ? {} : { journal: props.journal }),
        ...(props.inflight === undefined ? {} : { inflight: props.inflight }),
      });
    const target =
      actions !== null && actions.sessionId === sessionId
        ? actions
        : connection === null
          ? make()
          : sharedActions(connection, sessionId, make);
    let staged: StagedAttachment[];
    try {
      staged = await stageAll(sessionId);
    } catch (error) {
      setProblem(
        error instanceof AttachmentError
          ? t("composer.attachFailed", { name: error.attachment.name, message: error.message })
          : errorMessage(error),
      );
      return false;
    }
    const images = staged.filter((item) => item.image).map((item) => item.hostPath);
    const outcome = await sendShown(
      target,
      props.localRows ?? null,
      withFileReferences(body, staged),
      display,
      { images },
    );
    if (outcome === "rejected") {
      const failure = target.snapshot();
      setProblem(failure.error);
      if (props.sessionId !== null && isGoneName(failure.errorName)) {
        void handleGone(props.sessionId);
      }
      return false;
    }
    recall.push(display);
    setAttachments([]);
    return true;
  };

  const submit = async () => {
    const value = text;
    if (composing.current || sending) return;
    if (value.trim() === "" && attachments.length === 0) return;
    setProblem(null);
    setSending(true);
    source.close();
    try {
      if (isSlashLine(value) && attachments.length === 0) {
        write("");
        recall.push(value);
        await runSlash(value, {
          request,
          sessionId: props.sessionId,
          local,
          submit: (prompt, display) => void sendText(prompt, display),
          prefill: (prefill) => write(prefill),
          show: system,
        });
        return;
      }
      const ok = await sendText(value, value);
      if (ok) store.update((state) => setDraft(state, key, ""));
    } catch (error) {
      setProblem(errorMessage(error));
    } finally {
      setSending(false);
    }
  };

  // -- keys ---------------------------------------------------------------------------------------

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing || composing.current) return;
    if (completion.trigger !== null && completion.items.length > 0) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        source.move(event.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey)) {
        const item = completion.items[completion.selected];
        if (item !== undefined) {
          event.preventDefault();
          const applied = applyCompletion(text, completion.trigger, item);
          source.close();
          write(applied.text, applied.caret);
          return;
        }
      }
      if (event.key === "Escape") {
        event.preventDefault();
        source.close();
        return;
      }
    }
    if (event.key === "Escape" && busy.running) {
      event.preventDefault();
      void actions?.stop();
      return;
    }
    if ((event.key === "ArrowUp" || event.key === "ArrowDown") && recall.canRecall(text)) {
      const next = recall.step(event.key === "ArrowUp" ? -1 : 1, text);
      if (next !== null) {
        event.preventDefault();
        store.update((state) => setDraft(state, key, next));
      }
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  };

  const steer = async () => {
    if (actions === null || text.trim() === "") return;
    const value = text;
    const outcome = await actions.steer(value);
    if (outcome !== "rejected") {
      recall.push(value);
      store.update((state) => setDraft(state, key, ""));
    }
  };

  const restored = drafts.restored;
  const hasStack =
    todos.length > 0 ||
    busy.queue.length + busy.held.length > 0 ||
    busy.lost !== null ||
    busy.uncertain.length > 0;

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: the drop zone wraps the editor
    <div
      className="border-t border-line bg-bg"
      data-testid="composer"
      onDragOver={(event) => {
        if (event.dataTransfer.types.includes("Files")) event.preventDefault();
      }}
      onDrop={onDrop}
    >
      {hasStack ? (
        <div className="border-b border-line">
          <button
            type="button"
            aria-expanded={!stackHidden}
            aria-label={t(stackHidden ? "composer.showStack" : "composer.hideStack")}
            className="block h-2 w-full hover:bg-raised"
            onClick={() => setStackHidden(!stackHidden)}
          />
          {stackHidden ? null : (
            <div data-testid="status-stack">
              <TodoList todos={todos} />
              {actions !== null ? <QueuePanel actions={actions} /> : null}
            </div>
          )}
        </div>
      ) : null}

      {restored !== null && props.sessionId === null ? (
        <div role="status" className="flex items-center gap-2 px-3 py-1 text-xs bg-raised">
          <span className="flex-1">
            {restored.adopted ? t("composer.restored") : t("composer.restoredNotUsed")}
          </span>
          {restored.adopted ? (
            <button type="button" className="underline" onClick={() => store.update(undoRestore)}>
              {t("composer.undo")}
            </button>
          ) : (
            <button
              type="button"
              className="underline"
              onClick={() => void navigator.clipboard?.writeText(restored.text)}
            >
              {t("composer.copy")}
            </button>
          )}
          <button type="button" className="underline" onClick={() => store.update(dismissRestore)}>
            {t("composer.dismiss")}
          </button>
        </div>
      ) : null}

      {imageDraft !== null ? (
        <div role="status" className="flex items-center gap-2 px-3 py-1 text-xs bg-raised">
          <span className="flex-1">{t("composer.imageDraft")}</span>
          <button
            type="button"
            className="underline"
            onClick={() => {
              write(imageDraft.text);
              if (props.sessionId !== null) props.inflight?.clearImageDraft(props.sessionId);
              setImageDraft(null);
            }}
          >
            {t("composer.restoreImageDraft")}
          </button>
          <button
            type="button"
            className="underline"
            onClick={() => {
              if (props.sessionId !== null) props.inflight?.clearImageDraft(props.sessionId);
              setImageDraft(null);
            }}
          >
            {t("composer.dismiss")}
          </button>
        </div>
      ) : null}

      {attachments.length > 0 ? (
        <ul
          aria-label={t("composer.attachments")}
          className="flex flex-wrap gap-1 px-3 pt-2 list-none m-0"
        >
          {attachments.map((attachment) => (
            <li
              key={attachment.id}
              data-kind={isImage(attachment) ? "image" : "file"}
              className="flex items-center gap-1 rounded border border-line px-2 text-xs"
            >
              <span>{attachment.name}</span>
              <button
                type="button"
                aria-label={t("composer.removeAttachment", { name: attachment.name })}
                onClick={() => setAttachments((now) => now.filter((a) => a.id !== attachment.id))}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <div className="relative px-3 py-2">
        {completion.trigger !== null && completion.items.length > 0 ? (
          <div
            role="listbox"
            id={listId}
            aria-label={t("composer.completions")}
            className="absolute bottom-full left-3 z-20 mb-1 max-h-60 w-96 overflow-auto rounded border border-line bg-raised shadow p-1"
          >
            {completion.items.map((item, index) => (
              <div
                key={`${item.text}\u0000${item.display}`}
                id={`${listId}-${index}`}
                role="option"
                tabIndex={-1}
                aria-selected={index === completion.selected}
                className={`flex gap-2 rounded px-2 py-0.5 text-sm ${
                  index === completion.selected ? "bg-accent text-accent-fg" : ""
                }`}
                onMouseDown={(event) => {
                  event.preventDefault();
                  if (completion.trigger === null) return;
                  const applied = applyCompletion(text, completion.trigger, item);
                  source.close();
                  write(applied.text, applied.caret);
                }}
              >
                <span className="font-mono">{item.display}</span>
                {item.meta ? <span className="truncate text-muted">{item.meta}</span> : null}
              </div>
            ))}
          </div>
        ) : null}
        <textarea
          ref={textarea}
          aria-label={t("composer.label")}
          aria-autocomplete="list"
          aria-controls={completion.items.length > 0 ? listId : undefined}
          aria-activedescendant={
            completion.items.length > 0 ? `${listId}-${completion.selected}` : undefined
          }
          placeholder={t(busy.running ? "composer.placeholderBusy" : "composer.placeholder")}
          rows={3}
          className="w-full resize-none rounded border border-line bg-bg px-2 py-1 text-sm"
          value={text}
          onChange={onChange}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          onCompositionStart={() => {
            composing.current = true;
          }}
          onCompositionEnd={() => {
            composing.current = false;
          }}
        />
        {problem !== null ? (
          <p role="alert" className="text-xs text-danger">
            {problem}
          </p>
        ) : null}
        <div className="mt-1 flex items-center gap-2">
          <ModelPicker
            sessionId={props.sessionId}
            profile={props.profile ?? null}
            connection={connection}
            prefs={prefs}
            open={pickerOpen}
            onOpenChange={setPickerOpen}
          />
          {props.pickFiles !== undefined ? (
            <button type="button" className={BUTTON} onClick={() => void pick()}>
              {t("composer.attach")}
            </button>
          ) : null}
          <span className="flex-1" />
          {busy.running ? (
            <>
              <button type="button" className={BUTTON} onClick={() => void actions?.stop()}>
                {t("composer.stop")}
              </button>
              <button
                type="button"
                className={BUTTON}
                disabled={text.trim() === ""}
                onClick={() => void steer()}
              >
                {t("composer.steer")}
              </button>
              <button
                type="button"
                className={BUTTON}
                disabled={text.trim() === "" || sending}
                onClick={() => void submit()}
              >
                {t("composer.queue")}
              </button>
            </>
          ) : (
            <button
              type="button"
              className={BUTTON}
              disabled={(text.trim() === "" && attachments.length === 0) || sending}
              onClick={() => void submit()}
            >
              {t("composer.send")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function isGoneName(name: string | null): boolean {
  return name === "SESSION_NOT_FOUND" || name === "SESSION_REFUSED";
}
