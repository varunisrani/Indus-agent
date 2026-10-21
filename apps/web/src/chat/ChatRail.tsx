// The chat's right rail (P18-27): model badge, session switcher, workspace and reasoning pickers,
// copy-last-answer and image upload, next to the terminal.
//
// Its only live source is the chat's event feed (`eventsFeed.ts`). The rail never runs a second
// chat: changes it makes go into the terminal as the TUI's own slash commands (`/reasoning`,
// `/image`), and resuming or starting a session asks the host for a new PTY connection. Everything
// here is best-effort: the host wraps the rail in an error boundary and nothing here can stop or
// block the terminal (R09.1).

import { useQuery } from "@tanstack/react-query";
import { type FormEvent, useCallback, useEffect, useId, useState } from "react";
import { api, joinPath, request } from "../lib/api.ts";
import { relativeTime } from "../lib/format.ts";
import type { SessionInfo } from "../lib/restTypes.ts";
import { AUTOMATION_SOURCES } from "../pages/sessions/sources.ts";
import { Badge, Button, inputClass, Notice } from "../ui/controls.tsx";
import {
  browserFeedDeps,
  EventsFeed,
  type FeedDeps,
  type FeedEvent,
  type FeedStatus,
} from "./eventsFeed.ts";

export interface ChatRailProps {
  /** The host's channel id: the feed listens to the same channel as the PTY child publishes on. */
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
  feedDeps?: FeedDeps;
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

// -- the rail ----------------------------------------------------------------------------------

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
  return session.title?.trim() || session.preview?.trim() || session.id;
}

function ModelBadge({ live }: { live: LiveSession | null }) {
  const configured = useQuery({
    queryKey: ["chat", "model-info"],
    queryFn: () => request<ModelInfo>("/api/model/info"),
    staleTime: 60_000,
  });
  if (live?.model) {
    return (
      <div data-testid="chat-model" data-source="live" className="flex flex-col gap-1">
        <span className="truncate font-mono text-sm" title={live.model}>
          {live.model}
        </span>
        <span className="flex items-center gap-1 text-xs text-muted">
          <Badge tone="ok">answering</Badge>
          {live.provider ? <span className="truncate">{live.provider}</span> : null}
        </span>
      </div>
    );
  }
  const model = configured.data?.model;
  return (
    <div data-testid="chat-model" data-source="configured" className="flex flex-col gap-1">
      <span className="truncate font-mono text-sm">
        {model ?? (configured.isLoading ? "…" : "No model configured")}
      </span>
      {model ? (
        <span className="flex items-center gap-1 text-xs text-muted">
          <Badge>configured</Badge>
          {configured.data?.provider ? <span>{configured.data.provider}</span> : null}
        </span>
      ) : null}
    </div>
  );
}

function SessionSwitcher({
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
    <section aria-labelledby="chat-rail-sessions" className="flex min-h-0 flex-col gap-2">
      <div className="flex items-center gap-2">
        <h2 id="chat-rail-sessions" className="flex-1 text-xs font-semibold uppercase text-muted">
          Sessions
        </h2>
        <Button
          tone="ghost"
          className="px-2 py-0.5 text-xs"
          onClick={() => void sessions.refetch()}
          aria-label="Refresh sessions"
        >
          Refresh
        </Button>
        <Button tone="primary" className="px-2 py-0.5 text-xs" onClick={onNewChat}>
          New chat
        </Button>
      </div>
      {sessions.isError ? (
        <Notice tone="warn">The session list could not be loaded.</Notice>
      ) : rows.length === 0 ? (
        <p className="text-xs text-muted">{sessions.isLoading ? "Loading…" : "No sessions yet."}</p>
      ) : (
        <ul className="flex flex-col gap-1 overflow-auto">
          {rows.map((session) => (
            <li key={session.id}>
              <button
                type="button"
                data-session-id={session.id}
                aria-current={session.id === current ? "true" : undefined}
                className={`w-full rounded-md border px-2 py-1.5 text-left text-sm hover:bg-code ${
                  session.id === current ? "border-accent" : "border-transparent"
                }`}
                onClick={() => onResume(session.id)}
              >
                <span className="block truncate">{sessionLabel(session)}</span>
                <span className="flex gap-2 text-xs text-muted">
                  <span>{relativeTime(session.last_active)}</span>
                  <span>
                    {session.message_count} {session.message_count === 1 ? "message" : "messages"}
                  </span>
                  {session.source ? <span>{session.source}</span> : null}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
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

  useEffect(() => {
    setSelected(storedWorkspace(profile));
    setOptions(workspaceOptions(profile, liveCwd));
  }, [profile, liveCwd]);

  const pick = (path: string) => {
    chooseWorkspace(profile, path);
    setSelected(path);
    setOptions(workspaceOptions(profile, liveCwd));
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const path = draft.trim();
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
    <section className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-semibold uppercase text-muted">
        Workspace for new chats
      </label>
      <select
        id={id}
        className={inputClass}
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
        <option value="">Default folder</option>
        {options.map((path) => (
          <option key={path} value={path}>
            {path}
          </option>
        ))}
        <option value={OTHER}>Other path…</option>
      </select>
      {typing ? (
        <form className="flex gap-1" onSubmit={submit}>
          <input
            aria-label="Workspace path"
            className={`${inputClass} min-w-0 flex-1`}
            placeholder="/path/to/project"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
          <Button type="submit" className="px-2 py-1 text-xs">
            Use
          </Button>
        </form>
      ) : null}
      {error ? <p className="text-xs text-danger">{error}</p> : null}
      <p className="text-xs text-muted">Used when you start a new chat.</p>
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
    <section className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-semibold uppercase text-muted">
        Reasoning effort
      </label>
      <select
        id={id}
        className={inputClass}
        value={chosen}
        onChange={(event) => {
          const value = event.target.value;
          if (!value) return;
          setChosen(value);
          onPick(value);
        }}
      >
        <option value="">{current ? "Current" : "Session default"}</option>
        {EFFORTS.map((effort) => (
          <option key={effort.value} value={effort.value}>
            {effort.label}
          </option>
        ))}
      </select>
    </section>
  );
}

export function ChatRail({
  channel,
  profile,
  connected,
  resumeId,
  onResume,
  onNewChat,
  typeIntoChat,
  feedDeps,
}: ChatRailProps) {
  const feed = useEventsFeed(channel, feedDeps ?? browserFeedDeps);
  const [message, setMessage] = useState<{ tone: "neutral" | "warn"; text: string } | null>(null);
  const [dragging, setDragging] = useState(false);

  const say = (text: string, tone: "neutral" | "warn" = "neutral") => setMessage({ text, tone });

  const copyLast = async () => {
    if (!feed.lastAnswer) return;
    try {
      await navigator.clipboard.writeText(feed.lastAnswer);
      say("Last answer copied.");
    } catch {
      say("The browser did not allow copying.", "warn");
    }
  };

  const upload = async (file: File | null) => {
    if (!file) {
      say("Drop or paste an image file.", "warn");
      return;
    }
    say(`Uploading ${file.name}…`);
    try {
      await attachImage(file, typeIntoChat);
      say(`${file.name} goes with your next message.`);
    } catch (error) {
      say(error instanceof Error ? error.message : "The image could not be attached.", "warn");
    }
  };

  const current = feed.live?.sessionId ?? resumeId;

  return (
    <section
      aria-label="Chat tools"
      data-testid="chat-rail"
      className={`flex h-full min-h-0 flex-col gap-4 overflow-auto p-3 text-sm ${
        dragging ? "outline outline-2 outline-accent" : ""
      }`}
      onDragOver={(event) => {
        if (Array.from(event.dataTransfer.types).includes("Files")) {
          event.preventDefault();
          setDragging(true);
        }
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        void upload(firstImage(event.dataTransfer.files));
      }}
    >
      <section aria-label="Model" className="flex flex-col gap-1">
        <h2 className="text-xs font-semibold uppercase text-muted">Model</h2>
        <ModelBadge live={feed.live} />
      </section>

      {feed.status.notice ? (
        <div className="flex flex-col gap-1" data-testid="chat-feed-notice">
          <Notice tone="warn">{feed.status.notice}</Notice>
          {feed.status.phase === "stopped" ? (
            feed.status.reload ? (
              <Button className="self-start text-xs" onClick={() => window.location.reload()}>
                Reload page
              </Button>
            ) : (
              <Button className="self-start text-xs" onClick={feed.reconnect}>
                Reconnect
              </Button>
            )
          ) : null}
        </div>
      ) : null}

      <SessionSwitcher
        profile={profile}
        current={current}
        onResume={onResume}
        onNewChat={onNewChat}
      />

      <WorkspacePicker profile={profile} liveCwd={feed.live?.cwd ?? null} />

      <ReasoningPicker
        current={feed.live?.reasoning}
        onPick={(value) => {
          if (!typeIntoChat(`/reasoning ${value}\r`)) {
            say("The chat is not connected; the reasoning effort was not changed.", "warn");
          }
        }}
      />

      <section className="flex flex-col gap-2">
        <Button
          disabled={!feed.lastAnswer}
          onClick={() => void copyLast()}
          title={feed.lastAnswer ? undefined : "No answer in this chat yet"}
        >
          Copy last answer
        </Button>
        <label className="flex cursor-pointer flex-col gap-1 rounded-md border border-dashed border-line p-2 text-xs text-muted">
          <span>Drop or paste an image to attach it to your next message, or pick one.</span>
          <input
            type="file"
            accept="image/*"
            className="text-xs"
            disabled={!connected}
            onChange={(event) => {
              void upload(firstImage(event.target.files));
              event.target.value = "";
            }}
          />
        </label>
      </section>

      {message ? (
        <p
          role="status"
          className={`text-xs ${message.tone === "warn" ? "text-warn" : "text-muted"}`}
        >
          {message.text}
        </p>
      ) : null}
    </section>
  );
}
