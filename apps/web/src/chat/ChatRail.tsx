// The chat's side panel (P18-27, look rebuilt W52-a): model card with the feed's connection badge
// and the model picker, reasoning effort, the session list with New chat, and the workspace for new
// chats, next to the terminal. (Copy-last-answer and the image paste/drop live on the terminal card.)
//
// Its only live source is the chat's event feed (`eventsFeed.ts`). The rail never runs a second
// chat: changes it makes go into the terminal as the TUI's own slash commands (`/reasoning`,
// `/image`), and resuming or starting a session asks the host for a new PTY connection. Everything
// here is best-effort: the host wraps the rail in an error boundary and nothing here can stop or
// block the terminal (R09.1).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  FolderGit2,
  LoaderCircle,
  MessageSquarePlus,
  RefreshCw,
  X,
} from "lucide-react";
import { type FormEvent, useCallback, useEffect, useId, useState } from "react";
import { api, joinPath, request } from "../lib/api.ts";
import { relativeTime } from "../lib/format.ts";
import type { SessionInfo } from "../lib/restTypes.ts";
import { AUTOMATION_SOURCES } from "../pages/sessions/sources.ts";
import { Badge, type BadgeTone, Button, IconButton, inputClass } from "../ui/controls.tsx";
import { cx } from "../ui/cx.ts";
import { type ModelChoice, ModelPickerDialog } from "../ui/ModelPickerDialog.tsx";
import {
  browserFeedDeps,
  EventsFeed,
  type FeedDeps,
  type FeedEvent,
  type FeedStatus,
} from "./eventsFeed.ts";

export interface ChatRailProps {
  /** The host's channel id (the feed it was handed listens on it). */
  channel: string;
  /** Active profile ("" = the backend's own). */
  profile: string;
  /** True while the terminal socket is open. */
  connected: boolean;
  /** The session the terminal was asked to resume, if any. */
  resumeId: string | null;
  onResume(id: string): void;
  onNewChat(): void;
  /** Type `text` into the terminal as if the person did; false when not connected. */
  typeIntoChat(text: string): boolean;
  /** The chat's event feed; the host owns it (W52-sc-5) so the card and header can read it too. */
  feed: FeedState;
  /** Hide the panel (wide screens); absent in the narrow-screen drawer, which has its own close. */
  onCollapse?: () => void;
}

// -- live state from the feed ------------------------------------------------------------------

export interface LiveSession {
  sessionId?: string;
  model?: string;
  provider?: string;
  reasoning?: string | null;
  cwd?: string | null;
  title?: string | null;
}

export interface FeedState {
  status: FeedStatus;
  live: LiveSession | null;
  lastAnswer: string | null;
  reconnect(): void;
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

/** What a `session.info` event tells the rail. */
export function liveFrom(event: FeedEvent): LiveSession {
  const live: LiveSession = {};
  const sessionId = text(event.session_id);
  const model = text(event.model);
  const provider = text(event.provider);
  if (sessionId) live.sessionId = sessionId;
  if (model) live.model = model;
  if (provider) live.provider = provider;
  if ("reasoning_effort" in event) live.reasoning = text(event.reasoning_effort) ?? null;
  if ("cwd" in event) live.cwd = text(event.cwd) ?? null;
  if ("title" in event) live.title = text(event.title) ?? null;
  return live;
}

export function useEventsFeed(channel: string, deps: FeedDeps = browserFeedDeps): FeedState {
  const [status, setStatus] = useState<FeedStatus>({ phase: "connecting" });
  const [live, setLive] = useState<LiveSession | null>(null);
  const [lastAnswer, setLastAnswer] = useState<string | null>(null);
  const [feed, setFeed] = useState<EventsFeed | null>(null);

  useEffect(() => {
    const next = new EventsFeed(
      channel,
      {
        status: setStatus,
        event: (event) => {
          if (event.type === "session.info") {
            const update = liveFrom(event);
            setLive((was) =>
              was?.sessionId === update.sessionId ? { ...was, ...update } : update,
            );
          } else if (event.type === "session.title") {
            const title = text(event.title);
            if (title) setLive((was) => ({ ...(was ?? {}), title }));
          } else if (event.type === "message.complete") {
            const answer = text(event.text);
            if (answer) setLastAnswer(answer);
          }
        },
      },
      deps,
    );
    setFeed(next);
    next.start();
    return () => next.stop();
  }, [channel, deps]);

  const reconnect = useCallback(() => feed?.reconnect(), [feed]);
  return { status, live, lastAnswer, reconnect };
}

// -- workspace memory --------------------------------------------------------------------------

export const WORKSPACE_KEY = "indus.chat.workspace";
export const WORKSPACES_KEY = "indus.chat.workspaces";
const MAX_REMEMBERED = 8;

function profileKey(base: string, profile: string): string {
  return `${base}.${profile || "(serving)"}`;
}

function readLocal(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeLocal(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // storage unavailable: the choice lasts for this page only
  }
}

/** The folder a new chat of `profile` starts in ("" = the server's default). */
export function storedWorkspace(profile: string): string {
  return readLocal(profileKey(WORKSPACE_KEY, profile)) ?? "";
}

/** Folders picked before for `profile`, most recent first. */
export function rememberedWorkspaces(profile: string): string[] {
  const raw = readLocal(profileKey(WORKSPACES_KEY, profile));
  if (!raw) return [];
  try {
    const value: unknown = JSON.parse(raw);
    return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
  } catch {
    return [];
  }
}

/** Pick `path` for `profile` ("" = default); it moves to the front of the remembered list. */
export function chooseWorkspace(profile: string, path: string): void {
  writeLocal(profileKey(WORKSPACE_KEY, profile), path || null);
  if (!path) return;
  const list = [path, ...rememberedWorkspaces(profile).filter((p) => p !== path)];
  writeLocal(profileKey(WORKSPACES_KEY, profile), JSON.stringify(list.slice(0, MAX_REMEMBERED)));
}

/** True for an absolute POSIX or Windows path. */
export function isAbsolutePath(path: string): boolean {
  return path.startsWith("/") || /^[A-Za-z]:[\\/]/.test(path) || path.startsWith("\\\\");
}

/** Options for the picker: remembered folders, then the live session's folder, de-duplicated. */
export function workspaceOptions(profile: string, liveCwd: string | null | undefined): string[] {
  const out = rememberedWorkspaces(profile);
  if (liveCwd && !out.includes(liveCwd)) out.push(liveCwd);
  return out;
}

// -- images ------------------------------------------------------------------------------------

export const UPLOAD_FOLDER = "indus-chat-uploads";

function safeName(name: string): string {
  const cleaned = name.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[-.]+/, "");
  return cleaned.slice(-80) || "image";
}

/**
 * Upload an image through the Files API and attach it to the next message with `/image <path>`.
 * Throws with a plain message when it cannot.
 */
export async function attachImage(
  file: File,
  typeIntoChat: (text: string) => boolean,
  now: number = Date.now(),
): Promise<string> {
  if (!file.type.startsWith("image/")) throw new Error("Only images can be attached here.");
  const root = await api.files.list();
  const folder = joinPath(root.path, UPLOAD_FOLDER);
  const named = new File([file], `${now}-${safeName(file.name)}`, { type: file.type });
  await api.files.upload(folder, named);
  const path = joinPath(folder, named.name);
  if (!typeIntoChat(`/image ${path}\r`)) {
    throw new Error("The image was uploaded, but the chat is not connected to attach it.");
  }
  return path;
}

/** The first image among pasted or dropped files. */
export function firstImage(files: FileList | readonly File[] | null | undefined): File | null {
  if (!files) return null;
  for (const file of Array.from(files)) if (file.type.startsWith("image/")) return file;
  return null;
}

// -- the panel ---------------------------------------------------------------------------------

export const EFFORTS: ReadonlyArray<{ value: string; label: string }> = [
  { value: "minimal", label: "Minimal" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "Extra high" },
  { value: "none", label: "Off" },
];

interface ModelInfo {
  model: string | null;
  provider: string | null;
}

function sessionLabel(session: SessionInfo): string {
  return session.title?.trim() || session.preview?.trim() || "Untitled session";
}

const capsLabel = "type-heading whitespace-nowrap text-[0.75rem] text-ink-soft uppercase";

/** The connection badge: what the feed is doing, never what the model is doing. */
export function feedBadge(status: FeedStatus): { text: string; tone: BadgeTone } {
  switch (status.phase) {
    case "live":
      return { text: "live", tone: "ok" };
    case "connecting":
      return { text: "connecting", tone: "warn" };
    case "retrying":
      return { text: "retrying", tone: "warn" };
    default:
      return status.notice
        ? { text: "error", tone: "danger" }
        : { text: "closed", tone: "neutral" };
  }
}

function ModelCard({
  live,
  status,
  onPick,
}: {
  live: LiveSession | null;
  status: FeedStatus;
  onPick(): void;
}) {
  const configured = useQuery({
    queryKey: ["chat", "model-info"],
    queryFn: () => request<ModelInfo>("/api/model/info"),
    staleTime: 60_000,
  });
  const source = live?.model ? "live" : "configured";
  const model = live?.model ?? configured.data?.model;
  const provider = live?.model ? live.provider : configured.data?.provider;
  const name = model ?? (configured.isLoading ? "…" : "No model configured");
  const badge = feedBadge(status);
  return (
    <section
      aria-label="Model"
      data-testid="chat-model"
      data-source={source}
      className="flex items-center gap-2 border border-line px-3 py-2"
    >
      <div className="flex min-w-0 flex-1 flex-col">
        <h2 className={cx(capsLabel, "m-0")}>Model</h2>
        <button
          type="button"
          onClick={onPick}
          title={provider ? `${name} · ${provider}` : name}
          aria-label={`${name}: switch model`}
          className="flex min-w-0 items-center gap-1 text-left font-mono text-[0.875rem] font-medium text-ink hover:underline"
        >
          <span className="truncate">{name}</span>
          <ChevronDown aria-hidden="true" className="size-3.5 shrink-0 text-ink-muted" />
        </button>
      </div>
      <Badge tone={badge.tone} className="h-[1.375rem] text-[0.75rem]">
        <span data-testid="chat-feed-phase">{badge.text}</span>
      </Badge>
    </section>
  );
}

function SessionList({
  profile,
  current,
  onResume,
  onNewChat,
}: {
  profile: string;
  current: string | null;
  onResume(id: string): void;
  onNewChat(): void;
}) {
  const sessions = useQuery({
    queryKey: ["chat", "sessions", profile],
    queryFn: () =>
      api.sessions.list({ order: "recent", limit: 20, excludeSources: AUTOMATION_SOURCES }),
    staleTime: 15_000,
  });
  const rows = sessions.data?.sessions ?? [];
  return (
    <>
      <Button
        icon={<MessageSquarePlus />}
        className="h-7 w-full shrink-0 text-[0.75rem]"
        onClick={onNewChat}
      >
        New chat
      </Button>
      <section aria-label="Sessions" className="flex min-h-0 flex-1 flex-col">
        {sessions.isError ? (
          <div className="flex flex-col items-start gap-2 px-2 py-2">
            <p className="m-0 text-[0.75rem] text-danger">The session list could not be loaded.</p>
            <Button size="sm" onClick={() => void sessions.refetch()}>
              Retry
            </Button>
          </div>
        ) : rows.length === 0 ? (
          <p className="m-0 flex items-center justify-center gap-2 py-4 text-[0.75rem] text-ink-soft">
            {sessions.isLoading ? (
              <>
                <LoaderCircle aria-hidden="true" className="size-3.5 animate-spin" />
                Loading…
              </>
            ) : (
              "No sessions yet"
            )}
          </p>
        ) : (
          <ul className="m-0 flex list-none flex-col overflow-y-auto p-0">
            {rows.map((session) => {
              const here = session.id === current;
              const meta = [
                relativeTime(session.last_active),
                ...(session.message_count > 0 ? [`${session.message_count} msgs`] : []),
                ...(session.source && session.source !== "cli" ? [session.source] : []),
              ];
              return (
                <li key={session.id}>
                  <button
                    type="button"
                    data-session-id={session.id}
                    aria-current={here ? "true" : undefined}
                    className={cx(
                      "flex w-full flex-col gap-0.5 rounded-[4px] border-l-2 px-2 py-1.5 text-left",
                      here
                        ? "border-ink bg-ink/10 text-ink"
                        : "border-transparent text-ink-muted hover:bg-hover hover:text-ink",
                    )}
                    onClick={() => onResume(session.id)}
                  >
                    <span className="truncate font-mono text-[0.875rem] font-medium">
                      {sessionLabel(session)}
                    </span>
                    <span className="truncate font-mono text-[0.6875rem] text-ink-soft">
                      {meta.join(" · ")}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </>
  );
}

const OTHER = "\u0000other";

function WorkspacePicker({ profile, liveCwd }: { profile: string; liveCwd: string | null }) {
  const id = useId();
  const [selected, setSelected] = useState(() => storedWorkspace(profile));
  const [options, setOptions] = useState(() => workspaceOptions(profile, liveCwd));
  const [typing, setTyping] = useState(false);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => {
    setSelected(storedWorkspace(profile));
    setOptions(workspaceOptions(profile, liveCwd));
  }, [profile, liveCwd]);
  useEffect(reload, [reload]);

  const pick = (path: string) => {
    chooseWorkspace(profile, path);
    setSelected(path);
    setOptions(workspaceOptions(profile, liveCwd));
  };

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    const path = draft.trim();
    if (!path) {
      setTyping(false);
      return;
    }
    if (!isAbsolutePath(path)) {
      setError("Enter a full path, starting at the root of the disk.");
      return;
    }
    setError(null);
    setTyping(false);
    setDraft("");
    pick(path);
  };

  return (
    <section className="flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5">
        <FolderGit2 aria-hidden="true" className="size-3.5 text-ink-soft" />
        <label htmlFor={id} className={cx(capsLabel, "flex-1")}>
          Workspace
        </label>
        <IconButton label="Refresh workspaces" className="size-6 [&_svg]:size-3.5" onClick={reload}>
          <RefreshCw />
        </IconButton>
      </div>
      <span className="relative flex">
        <select
          id={id}
          title="Used when you start a new chat"
          className={cx(
            inputClass,
            "h-[2.125rem] w-full appearance-none truncate pr-8 text-[0.875rem]",
          )}
          value={typing ? OTHER : selected}
          onChange={(event) => {
            const value = event.target.value;
            if (value === OTHER) {
              setTyping(true);
              return;
            }
            setTyping(false);
            pick(value);
          }}
        >
          <option value="">Default{liveCwd && !selected ? ` · ${liveCwd}` : ""}</option>
          {options.map((path) => (
            <option key={path} value={path}>
              {path}
            </option>
          ))}
          <option value={OTHER}>Custom…</option>
        </select>
        <ChevronDown
          aria-hidden="true"
          className="pointer-events-none absolute top-1/2 right-2.5 size-3.5 -translate-y-1/2 text-ink-soft"
        />
      </span>
      {typing ? (
        <form onSubmit={submit}>
          <input
            aria-label="Workspace path"
            // biome-ignore lint/a11y/noAutofocus: the field appears because the person picked Custom…
            autoFocus
            className={cx(inputClass, "h-8 w-full text-[0.8125rem]")}
            placeholder="/path/to/project"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onBlur={() => submit()}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                setTyping(false);
                setDraft("");
                setError(null);
              }
            }}
          />
        </form>
      ) : null}
      {error ? <p className="m-0 text-[0.75rem] text-danger">{error}</p> : null}
    </section>
  );
}

function ReasoningPicker({
  current,
  onPick,
}: {
  current: string | null | undefined;
  onPick(value: string): void;
}) {
  const id = useId();
  const [chosen, setChosen] = useState(current ?? "");
  useEffect(() => setChosen(current ?? ""), [current]);
  return (
    <section className="flex items-center gap-2 border border-line px-3 py-2">
      <label htmlFor={id} className={cx(capsLabel, "flex-1")}>
        Reasoning effort
      </label>
      <span className="relative inline-flex">
        <select
          id={id}
          className="type-data h-7 appearance-none border border-line bg-transparent pr-7 pl-2 text-[0.75rem] text-ink outline-none focus:border-ink"
          value={chosen}
          onChange={(event) => {
            const value = event.target.value;
            if (!value) return;
            setChosen(value);
            onPick(value);
          }}
        >
          <option value="">{current ? "Current" : "Default"}</option>
          {EFFORTS.map((effort) => (
            <option key={effort.value} value={effort.value}>
              {effort.label}
            </option>
          ))}
        </select>
        <ChevronDown
          aria-hidden="true"
          className="pointer-events-none absolute top-1/2 right-2 size-3 -translate-y-1/2 text-ink-soft"
        />
      </span>
    </section>
  );
}

function NoticeCard({
  tone,
  children,
  action,
}: {
  tone: "warn" | "danger" | "neutral";
  children: string;
  action?: { label: string; run(): void };
}) {
  return (
    <div
      role={tone === "danger" ? "alert" : "status"}
      className={cx(
        "flex flex-col items-start gap-2 border px-3 py-2 text-[0.75rem]",
        tone === "danger"
          ? "border-danger/50 bg-danger-wash text-danger"
          : tone === "warn"
            ? "border-warning/50 bg-warning-wash text-warning"
            : "border-line text-ink-muted",
      )}
    >
      <p className="m-0">{children}</p>
      {action ? (
        <Button size="sm" className="h-6 text-[0.6875rem]" onClick={action.run}>
          {action.label}
        </Button>
      ) : null}
    </div>
  );
}

export function ChatRail({
  profile,
  resumeId,
  onResume,
  onNewChat,
  typeIntoChat,
  feed,
  onCollapse,
}: ChatRailProps) {
  const [message, setMessage] = useState<{ tone: "neutral" | "warn"; text: string } | null>(null);
  const [picking, setPicking] = useState(false);
  const sessions = useQuery({
    queryKey: ["chat", "sessions", profile],
    queryFn: () =>
      api.sessions.list({ order: "recent", limit: 20, excludeSources: AUTOMATION_SOURCES }),
    staleTime: 15_000,
  });

  const say = (text: string, tone: "neutral" | "warn" = "neutral") => setMessage({ text, tone });
  const client = useQueryClient();
  // The picker saves the main model for the active profile; the live chat keeps its model.
  const switchModel = useMutation({
    mutationFn: (pick: ModelChoice) =>
      api.model.set({ scope: "main", provider: pick.provider, model: pick.model }),
    onSuccess: (_ok, pick) => {
      void client.invalidateQueries({ queryKey: ["chat", "model-info"] });
      void client.invalidateQueries({ queryKey: ["models"] });
      say(`Model set to ${pick.model}. Start a new chat to use it.`, "warn");
      setPicking(false);
    },
  });
  const current = feed.live?.sessionId ?? resumeId;
  const refetchSessions = sessions.refetch;
  // A new or retitled session, or a finished answer: the list's titles and counts moved.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the feed values are the triggers
  useEffect(() => {
    if (feed.live?.sessionId || feed.lastAnswer) void refetchSessions();
  }, [feed.live?.sessionId, feed.live?.title, feed.lastAnswer, refetchSessions]);
  const feedNotice = feed.status.notice;

  return (
    <section
      aria-label="Chat tools"
      data-testid="chat-rail"
      className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto"
    >
      {onCollapse ? (
        <div className="flex h-8 shrink-0 items-center justify-end">
          <IconButton label="Collapse side panel" className="[&_svg]:size-3.5" onClick={onCollapse}>
            <X />
          </IconButton>
        </div>
      ) : null}

      <ModelCard live={feed.live} status={feed.status} onPick={() => setPicking(true)} />

      <ReasoningPicker
        current={feed.live?.reasoning}
        onPick={(value) => {
          if (!typeIntoChat(`/reasoning ${value}\r`)) {
            say("The chat is not connected; the reasoning effort was not changed.", "warn");
          }
        }}
      />

      {feedNotice ? (
        <div data-testid="chat-feed-notice">
          <NoticeCard
            tone={feed.status.phase === "stopped" ? "danger" : "warn"}
            {...(feed.status.phase === "stopped"
              ? {
                  action: feed.status.reload
                    ? { label: "Reload page", run: () => window.location.reload() }
                    : { label: "Reconnect", run: feed.reconnect },
                }
              : {})}
          >
            {feedNotice}
          </NoticeCard>
        </div>
      ) : null}

      {message ? (
        <NoticeCard tone={message.tone === "warn" ? "warn" : "neutral"}>{message.text}</NoticeCard>
      ) : null}

      <div className="flex h-5 shrink-0 items-center gap-2">
        <h2 className={cx(capsLabel, "m-0 flex-1")}>Sessions</h2>
        <IconButton
          label="Refresh sessions"
          className="size-6 [&_svg]:size-3.5"
          onClick={() => void sessions.refetch()}
        >
          <RefreshCw className={sessions.isFetching ? "animate-spin" : ""} />
        </IconButton>
      </div>

      <WorkspacePicker profile={profile} liveCwd={feed.live?.cwd ?? null} />

      <SessionList profile={profile} current={current} onResume={onResume} onNewChat={onNewChat} />

      <ModelPickerDialog
        open={picking}
        onOpenChange={(next) => {
          if (next) switchModel.reset();
          setPicking(next);
        }}
        busy={switchModel.isPending}
        error={switchModel.error}
        note="Saved to config; new chats use it."
        onApply={(choice) => switchModel.mutate(choice)}
      />
    </section>
  );
}
