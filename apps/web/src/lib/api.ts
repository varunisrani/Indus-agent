// The dashboard's one REST client (P18-18). Every page goes through `request` (JSON) or
// `rawRequest` (uploads, downloads), which add the base path, the loopback session header, the
// selected profile for profile-scoped families, and handle 401s in one place.

import { authRequired, basePath, servingProfile, sessionToken } from "./bootstrap.ts";
import type {
  ActionStarted,
  ActionStatus,
  AuxiliaryResponse,
  CheckpointsResponse,
  ConfigObject,
  ConfigSchema,
  CredentialPoolResponse,
  CronJob,
  CronJobInput,
  DaemonInfo,
  DeliveryTarget,
  DownloadTicket,
  EffectiveConfig,
  EnvKeys,
  EnvWriteResponse,
  FileContent,
  FileListing,
  HostStats,
  HubSearchResponse,
  LogComponent,
  LogFile,
  LoginSessionInfo,
  LoginStarted,
  LogLevel,
  LogsResponse,
  LogTail,
  McpServerCreate,
  McpServersResponse,
  McpTestResult,
  MemoryInfo,
  MessagingPlatformsResponse,
  ModelAssignment,
  ModelInfo,
  ModelOptions,
  MutationIdentity,
  MutationSnapshot,
  OkResponse,
  PairingApproveRequest,
  PairingResponse,
  PersonaResponse,
  PlatformTestResult,
  PlatformUpdate,
  PlatformUpdateResponse,
  PluginInstallRequest,
  PluginsResponse,
  PoolAddRequest,
  ProfileCommandResponse,
  ProfileCreate,
  ProfileExportResponse,
  ProfilesResponse,
  PruneResponse,
  SessionInfo,
  SessionListResponse,
  SessionMessagesResponse,
  SessionSearchResponse,
  SessionStats,
  ShellHookCreate,
  ShellHooksResponse,
  SkillInfo,
  StatusResponse,
  ToolsetInfo,
  UpdateInfo,
  UsageResponse,
  WebhookCreate,
  WebhookCreated,
  WebhooksResponse,
} from "./restTypes.ts";

export const SESSION_HEADER = "X-Indus-Session-Token";
/** Where the SPA was when a gated 401 sent it to the login page. */
export const LAST_LOCATION_KEY = "indus.lastLocation";
/** Session-storage guard: set when a loopback 401 reloaded the page, cleared by the next 2xx. */
export const TOKEN_RELOAD_KEY = "indus.tokenReload";

/**
 * Endpoint families that take `?profile=` (R09.20). A request whose path is one of these, or sits
 * below one (whole path segments), gets the selected profile appended unless it names one already.
 * Files and profiles are machine-level and stay out.
 */
export const PROFILE_SCOPED_FAMILIES = [
  "/api/actions",
  "/api/analytics",
  "/api/config",
  "/api/credentials",
  "/api/cron",
  "/api/daemon",
  "/api/env",
  "/api/learning",
  "/api/logs",
  "/api/mcp",
  "/api/memory",
  "/api/messaging",
  "/api/model",
  "/api/ops",
  "/api/pairing",
  "/api/plugins",
  "/api/providers/login",
  "/api/session-import",
  "/api/sessions",
  "/api/skills",
  "/api/status",
  "/api/system",
  "/api/tools/toolsets",
  "/api/webhooks",
] as const;

/**
 * Routes inside a scoped family that are not per profile: the public config schema and defaults,
 * the signed cron fire webhook and the MCP OAuth callback (the generated route list pins them).
 */
export const UNSCOPED_PATHS: ReadonlySet<string> = new Set([
  "/api/config/defaults",
  "/api/config/schema",
  "/api/cron/fire",
  "/api/mcp/oauth/callback",
]);

const UNREACHABLE_MESSAGE =
  "The dashboard backend is unreachable. Check that indus dashboard is running.";

export class ApiError extends Error {
  /** HTTP status, or 0 when no response arrived at all. */
  readonly status: number;
  /** Method, path and response text, for the console only. */
  readonly details: string;
  /** A machine-readable code from the body (`session_busy`, `unauthenticated`, …). */
  readonly code: string | undefined;
  readonly body: unknown;

  constructor(status: number, message: string, details: string, code?: string, body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
    this.code = code;
    this.body = body;
  }
}

// -- browser seam ------------------------------------------------------------------------------

export interface BrowserNavigation {
  assign(url: string): void;
  reload(): void;
}

const realNavigation: BrowserNavigation = {
  assign: (url) => window.location.assign(url),
  reload: () => window.location.reload(),
};
let navigation: BrowserNavigation = realNavigation;

/** Replace full-page navigation (tests); returns a function that restores the real one. */
export function setBrowserNavigation(next: BrowserNavigation): () => void {
  navigation = next;
  return () => {
    navigation = realNavigation;
  };
}

/** Full-page navigation through the same seam the 401 flow uses (sign-out, P18-28). */
export function assignLocation(url: string): void {
  navigation.assign(url);
}

type FailureListener = (error: ApiError) => void;
const unreachableListeners = new Set<FailureListener>();

/** Called once per request that got no response (the shell shows one plain toast). */
export function onUnreachable(listener: FailureListener): () => void {
  unreachableListeners.add(listener);
  return () => {
    unreachableListeners.delete(listener);
  };
}

// -- profile scope -----------------------------------------------------------------------------

let selected = "";

/** The profile the switcher picked; "" means the backend's own profile. */
export function setSelectedProfile(name: string): void {
  selected = name.trim();
}

/** The profile management calls target: the selection, else the profile this backend serves. */
export function activeProfile(): string {
  return selected || servingProfile();
}

function splitUrl(url: string): { path: string; query: string } {
  const at = url.indexOf("?");
  return at < 0 ? { path: url, query: "" } : { path: url.slice(0, at), query: url.slice(at + 1) };
}

/**
 * True when `path` is one of the scoped families or below one (matched on whole segments), except
 * the few unscoped routes inside them.
 */
export function isProfileScoped(path: string): boolean {
  const bare = splitUrl(path).path;
  if (UNSCOPED_PATHS.has(bare)) return false;
  return PROFILE_SCOPED_FAMILIES.some((family) => bare === family || bare.startsWith(`${family}/`));
}

/** Append `profile=<name>` to a scoped URL that does not name a profile already. */
export function withProfile(url: string, profile: string = activeProfile()): string {
  if (!profile) return url;
  const { path, query } = splitUrl(url);
  if (!isProfileScoped(path)) return url;
  if (new URLSearchParams(query).has("profile")) return url;
  const sep = query ? "&" : "?";
  return `${url}${url.endsWith("?") ? "" : sep}profile=${encodeURIComponent(profile)}`;
}

/** Build `path?k=v&…`, skipping empty values. */
export function withQuery(
  path: string,
  params: Record<string, string | number | boolean | undefined | null>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `${path}?${text}` : path;
}

// -- requests ----------------------------------------------------------------------------------

export interface ApiInit {
  method?: string;
  /** Sent as a JSON body with the matching content type. */
  json?: unknown;
  body?: BodyInit;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /**
   * Keep a loopback 401 local to the caller instead of reloading the page (the identity probe of
   * the auth widget, P18-28, asks "am I signed in?" and must not trigger the rotated-token path).
   */
  allowUnauthorized?: boolean;
}

function buildInit(init: ApiInit): RequestInit {
  const headers = new Headers(init.headers);
  const token = sessionToken();
  if (token && !headers.has(SESSION_HEADER)) headers.set(SESSION_HEADER, token);
  const out: RequestInit = {
    method: init.method ?? (init.json === undefined && init.body === undefined ? "GET" : "POST"),
    headers,
    credentials: "include",
  };
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    out.body = JSON.stringify(init.json);
  } else if (init.body !== undefined) {
    out.body = init.body;
  }
  if (init.signal) out.signal = init.signal;
  return out;
}

function report(error: ApiError): void {
  // The toast carries only the sentence; the details are for bug reports.
  // biome-ignore lint/suspicious/noConsole: the row asks for details in the console only
  console.warn("[indus api]", error.details);
}

async function send(url: string, init: ApiInit): Promise<Response> {
  const scoped = withProfile(url);
  const requestInit = buildInit(init);
  try {
    return await fetch(`${basePath()}${scoped}`, requestInit);
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    const reason = cause instanceof Error ? cause.message : String(cause);
    const error = new ApiError(
      0,
      UNREACHABLE_MESSAGE,
      `${requestInit.method} ${scoped}: ${reason}`,
    );
    report(error);
    for (const listener of unreachableListeners) listener(error);
    throw error;
  }
}

/**
 * Authenticated request that returns the raw response (uploads, binary reads). Same base path,
 * header and profile scope as `request`; it neither throws on a status nor runs the 401 flow.
 */
export function rawRequest(url: string, init: ApiInit = {}): Promise<Response> {
  return send(url, init);
}

function never<T>(): Promise<T> {
  return new Promise<T>(() => {});
}

function storageGet(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key: string, value: string): boolean {
  try {
    window.sessionStorage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

function storageRemove(key: string): void {
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    // storage unavailable: nothing to clear
  }
}

function rememberLocation(): void {
  storageSet(LAST_LOCATION_KEY, `${window.location.pathname}${window.location.search}`);
}

/** Reload once for a rotated loopback token; false when already tried (or storage is gone). */
function reloadOnce(): boolean {
  if (storageGet(TOKEN_RELOAD_KEY) !== null) return false;
  if (!storageSet(TOKEN_RELOAD_KEY, String(Date.now()))) return false;
  navigation.reload();
  return true;
}

interface GatedEnvelope {
  error: "unauthenticated" | "session_expired";
  login_url: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isGatedEnvelope(body: unknown): body is GatedEnvelope {
  return (
    isRecord(body) &&
    (body.error === "unauthenticated" || body.error === "session_expired") &&
    typeof body.login_url === "string" &&
    body.login_url.length > 0
  );
}

function parseBody(text: string): unknown {
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function errorParts(body: unknown, fallback: string): { message: string; code?: string } {
  if (typeof body === "string" && body.trim()) return { message: body.trim().slice(0, 300) };
  if (!isRecord(body)) return { message: fallback };
  const detail = body.detail;
  if (isRecord(detail)) {
    const message = typeof detail.message === "string" ? detail.message : fallback;
    return typeof detail.code === "string" ? { message, code: detail.code } : { message };
  }
  const message =
    typeof detail === "string"
      ? detail
      : typeof body.message === "string"
        ? body.message
        : fallback;
  const code =
    typeof body.code === "string"
      ? body.code
      : typeof body.error === "string"
        ? body.error
        : undefined;
  return code === undefined ? { message } : { message, code };
}

/** JSON request; resolves to the parsed body (undefined for an empty one) or throws `ApiError`. */
export async function request<T>(url: string, init: ApiInit = {}): Promise<T> {
  const res = await send(url, init);
  const text = await res.text().catch(() => "");
  const body = parseBody(text);
  if (res.status === 401) {
    if (isGatedEnvelope(body)) {
      rememberLocation();
      navigation.assign(body.login_url);
      return never<T>();
    }
    // Loopback: the token rotated with a backend restart under this open tab. The page is served
    // no-store, so one reload picks up the new token; the guard stops a loop. Never in gated mode.
    if (!authRequired() && !init.allowUnauthorized && reloadOnce()) return never<T>();
  }
  if (res.ok) {
    storageRemove(TOKEN_RELOAD_KEY);
    return body as T;
  }
  const { message, code } = errorParts(body, res.statusText || `HTTP ${res.status}`);
  const error = new ApiError(
    res.status,
    message,
    `${init.method ?? "GET"} ${url} → ${res.status}: ${text.slice(0, 500)}`,
    code,
    body,
  );
  report(error);
  throw error;
}

// -- WebSocket-free helpers --------------------------------------------------------------------

function segment(value: string): string {
  return encodeURIComponent(value);
}

function newRequestId(): string {
  const source = globalThis.crypto;
  if (source && typeof source.randomUUID === "function") return source.randomUUID();
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/** Turn a mutation snapshot into the identity a fenced write must carry (R03.22). */
export function identityFrom(snapshot: MutationSnapshot): MutationIdentity {
  const identity: MutationIdentity = {
    request_id: newRequestId(),
    expected_revision: snapshot.revision,
  };
  if (snapshot.exists && snapshot.generation !== null) {
    identity.expected_generation = snapshot.generation;
  }
  return identity;
}

/**
 * A download link for a host file. No credential ever rides in a URL: the link carries a ticket
 * minted (with the usual header or cookie) for this one path, good once, for 30 seconds.
 */
export async function fileDownloadUrl(path: string): Promise<string> {
  const minted = await request<DownloadTicket>("/api/files/download-ticket", { json: { path } });
  return `${basePath()}${withQuery("/api/files/download", { path, ticket: minted.ticket })}`;
}

/** Download a host file: mint its ticket, then let the browser fetch the attachment. */
export async function downloadFile(path: string): Promise<void> {
  const link = document.createElement("a");
  link.href = await fileDownloadUrl(path);
  link.download = path.slice(path.lastIndexOf("/") + 1);
  link.rel = "noopener";
  link.click();
}

/**
 * `url` aimed at one named profile instead of the selected one (the profile builder writes into the
 * profile it just made; a profile's export runs under that profile). No name → `url` unchanged.
 */
export function forProfile(url: string, profile?: string): string {
  const name = profile?.trim();
  if (!name) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}profile=${encodeURIComponent(name)}`;
}

/** Fetch an authenticated attachment (the session header rides along) and offer it as a file. */
export async function saveAttachment(url: string, filename: string): Promise<void> {
  const res = await rawRequest(url);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    const { message, code } = errorParts(parseBody(text), res.statusText || `HTTP ${res.status}`);
    throw new ApiError(res.status, message, `GET ${url} → ${res.status}`, code);
  }
  const blob = await res.blob();
  if (typeof URL.createObjectURL !== "function") return;
  const href = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = href;
  link.download = filename;
  link.rel = "noopener";
  link.click();
  URL.revokeObjectURL(href);
}

export interface SessionListQuery {
  sources?: readonly string[];
  excludeSources?: readonly string[];
  source?: string;
  order?: "created" | "recent";
  limit?: number;
  offset?: number;
}

export interface LogsQuery {
  file: LogFile;
  level: LogLevel;
  component: LogComponent;
  lines: LogTail;
}

export const api = {
  profiles: () => request<ProfilesResponse>("/api/profiles"),
  status: () => request<StatusResponse>("/api/status"),

  profile: {
    create: (body: ProfileCreate) =>
      request<ProfileCommandResponse>("/api/profiles", { json: body }),
    setSticky: (name: string) =>
      request<{ ok: boolean; active: string }>("/api/profiles/active", { json: { name } }),
    rename: (name: string, newName: string) =>
      request<ProfileCommandResponse>(`/api/profiles/${segment(name)}`, {
        method: "PATCH",
        json: { new_name: newName },
      }),
    remove: (name: string) =>
      request<ProfileCommandResponse>(`/api/profiles/${segment(name)}`, { method: "DELETE" }),
    persona: (name: string) => request<PersonaResponse>(`/api/profiles/${segment(name)}/persona`),
    setPersona: (name: string, content: string) =>
      request<OkResponse>(`/api/profiles/${segment(name)}/persona`, {
        method: "PUT",
        json: { content },
      }),
    setDescription: (name: string, description: string) =>
      request<OkResponse>(`/api/profiles/${segment(name)}/description`, {
        method: "PUT",
        json: { description },
      }),
    setModel: (name: string, provider: string, model: string) =>
      request<OkResponse>(`/api/profiles/${segment(name)}/model`, {
        method: "PUT",
        json: { provider, model },
      }),
    exportArchive: (name: string) =>
      request<ProfileExportResponse>(`/api/profiles/${segment(name)}/export`, { method: "POST" }),
    importArchive: (archive: string, name?: string) =>
      request<{ ok: boolean; name: string; path: string }>("/api/profiles/import", {
        json: name ? { archive, name } : { archive },
      }),
  },

  actions: {
    /** `profile` names the profile the run belongs to when it is not the selected one. */
    status: (name: string, lines = 400, profile?: string) =>
      request<ActionStatus>(
        forProfile(withQuery(`/api/actions/${segment(name)}/status`, { lines }), profile),
      ),
  },

  daemon: {
    get: () => request<DaemonInfo>("/api/daemon"),
    start: () => request<ActionStarted>("/api/daemon/start", { method: "POST" }),
    stop: () => request<ActionStarted>("/api/daemon/stop", { method: "POST" }),
    restart: () => request<ActionStarted>("/api/daemon/restart", { method: "POST" }),
  },

  messaging: {
    platforms: () => request<MessagingPlatformsResponse>("/api/messaging/platforms"),
    update: (platform: string, body: PlatformUpdate) =>
      request<PlatformUpdateResponse>(`/api/messaging/platforms/${segment(platform)}`, {
        method: "PUT",
        json: body,
      }),
    test: (platform: string) =>
      request<PlatformTestResult>(`/api/messaging/platforms/${segment(platform)}/test`, {
        method: "POST",
      }),
  },

  pairing: {
    list: () => request<PairingResponse>("/api/pairing"),
    approve: (body: PairingApproveRequest) =>
      request<{ ok: boolean; user: { user_id: string; user_name: string | null } }>(
        "/api/pairing/approve",
        { json: body },
      ),
    revoke: (platform: string, userId: string) =>
      request<OkResponse>("/api/pairing/revoke", { json: { platform, user_id: userId } }),
    clearPending: () =>
      request<{ ok: boolean; cleared: number }>("/api/pairing/clear-pending", { json: {} }),
  },

  webhooks: {
    list: () => request<WebhooksResponse>("/api/webhooks"),
    create: (body: WebhookCreate) => request<WebhookCreated>("/api/webhooks", { json: body }),
    remove: (name: string) =>
      request<OkResponse>(`/api/webhooks/${segment(name)}`, { method: "DELETE" }),
    setEnabled: (name: string, enabled: boolean) =>
      request<OkResponse>(`/api/webhooks/${segment(name)}/enabled`, {
        method: "PUT",
        json: { enabled },
      }),
    setReceiver: (enabled: boolean) =>
      request<OkResponse>("/api/webhooks/receiver", { method: "PUT", json: { enabled } }),
  },

  model: {
    info: () => request<ModelInfo>("/api/model/info"),
    options: () => request<ModelOptions>("/api/model/options"),
    auxiliary: () => request<AuxiliaryResponse>("/api/model/auxiliary"),
    set: (body: ModelAssignment) => request<OkResponse>("/api/model/set", { json: body }),
  },

  providerLogin: {
    start: (provider: string) =>
      request<LoginStarted>("/api/providers/login/start", { json: { provider } }),
    poll: (sessionId: string) =>
      request<LoginSessionInfo>(`/api/providers/login/${segment(sessionId)}`),
    cancel: (sessionId: string) =>
      request<LoginSessionInfo>(`/api/providers/login/${segment(sessionId)}`, {
        method: "DELETE",
      }),
  },

  system: {
    stats: () => request<HostStats>("/api/system/stats"),
    update: (force = false) =>
      request<UpdateInfo>(withQuery("/api/system/update", { force: force ? 1 : undefined })),
    applyUpdate: () => request<ActionStarted>("/api/system/update", { method: "POST" }),
    memory: () => request<MemoryInfo>("/api/memory"),
    setMemoryProvider: (provider: string) =>
      request<{ ok: boolean; active: string }>("/api/memory/provider", {
        method: "PUT",
        json: { provider },
      }),
    pool: () => request<CredentialPoolResponse>("/api/credentials/pool"),
    addToPool: (body: PoolAddRequest) =>
      request<{ ok: boolean; provider: string; index: number; id: string }>(
        "/api/credentials/pool",
        { json: body },
      ),
    removeFromPool: (provider: string, index: number) =>
      request<OkResponse>(`/api/credentials/pool/${segment(provider)}/${index}`, {
        method: "DELETE",
      }),
    doctor: () => request<ActionStarted>("/api/ops/doctor", { method: "POST" }),
    backup: () => request<ActionStarted>("/api/ops/backup", { method: "POST" }),
    downloadBackup: (archive: string) =>
      saveAttachment(
        withQuery("/api/ops/backup/download", { archive }),
        archive.slice(archive.lastIndexOf("/") + 1) || "backup.zip",
      ),
    importArchive: (archive: string, force: boolean) =>
      request<ActionStarted>("/api/ops/import", { json: { archive, force } }),
    importUpload: (file: File, force: boolean) => {
      const form = new FormData();
      form.set("file", file);
      form.set("force", force ? "true" : "false");
      return request<ActionStarted>("/api/ops/import-upload", { method: "POST", body: form });
    },
    checkpoints: () => request<CheckpointsResponse>("/api/ops/checkpoints"),
    pruneCheckpoints: (retentionDays: number, maxSizeMb: number) =>
      request<ActionStarted>("/api/ops/checkpoints/prune", {
        json: { retention_days: retentionDays, max_size_mb: maxSizeMb },
      }),
    hooks: () => request<ShellHooksResponse>("/api/ops/hooks"),
    addHook: (body: ShellHookCreate) => request<OkResponse>("/api/ops/hooks", { json: body }),
    removeHook: (event: string, command: string) =>
      request<OkResponse>("/api/ops/hooks", { method: "DELETE", json: { event, command } }),
  },

  sessions: {
    list: (query: SessionListQuery = {}) =>
      request<SessionListResponse>(
        withQuery("/api/sessions", {
          source: query.source,
          sources: query.sources?.length ? query.sources.join(",") : undefined,
          exclude_sources: query.excludeSources?.length
            ? query.excludeSources.join(",")
            : undefined,
          order: query.order ?? "recent",
          limit: query.limit ?? 50,
          offset: query.offset ?? 0,
        }),
      ),
    search: (q: string) => request<SessionSearchResponse>(withQuery("/api/sessions/search", { q })),
    stats: () => request<SessionStats>("/api/sessions/stats"),
    one: (id: string) => request<SessionInfo>(`/api/sessions/${segment(id)}`),
    messages: (id: string) =>
      request<SessionMessagesResponse>(
        withQuery(`/api/sessions/${segment(id)}/messages`, { limit: 500, order: "oldest" }),
      ),
    snapshot: (id: string) =>
      request<MutationSnapshot>(`/api/sessions/${segment(id)}/mutation-snapshot`),
    rename: async (id: string, title: string) => {
      const identity = identityFrom(await api.sessions.snapshot(id));
      return request<SessionInfo>(`/api/sessions/${segment(id)}`, {
        method: "PATCH",
        json: { title, ...identity },
      });
    },
    remove: async (id: string) => {
      const identity = identityFrom(await api.sessions.snapshot(id));
      return request<OkResponse>(withQuery(`/api/sessions/${segment(id)}`, { ...identity }), {
        method: "DELETE",
      });
    },
    exportJson: (id: string) => request<unknown>(`/api/sessions/${segment(id)}/export`),
    prune: (olderThanDays: number, dryRun: boolean) =>
      request<PruneResponse>("/api/sessions/prune", {
        method: "POST",
        json: { older_than_days: olderThanDays, dry_run: dryRun },
      }),
  },

  config: {
    get: () => request<ConfigObject>("/api/config"),
    put: (config: ConfigObject) =>
      request<OkResponse>("/api/config", { method: "PUT", json: { config } }),
    schema: () => request<ConfigSchema>("/api/config/schema"),
    effective: () => request<EffectiveConfig>("/api/config/effective"),
  },

  env: {
    list: () => request<EnvKeys>("/api/env"),
    set: (key: string, value: string) =>
      request<EnvWriteResponse>("/api/env", { method: "PUT", json: { key, value } }),
    remove: (key: string) => request<OkResponse>("/api/env", { method: "DELETE", json: { key } }),
  },

  logs: (query: LogsQuery) =>
    request<LogsResponse>(
      withQuery("/api/logs", {
        file: query.file,
        lines: query.lines,
        level: query.level === "ALL" ? undefined : query.level,
        component: query.component === "all" ? undefined : query.component,
      }),
    ),

  analytics: {
    usage: (days: number) => request<UsageResponse>(withQuery("/api/analytics/usage", { days })),
  },

  files: {
    list: (path?: string) => request<FileListing>(withQuery("/api/files", { path })),
    read: (path: string) => request<FileContent>(withQuery("/api/files/read", { path })),
    mkdir: (path: string) => request<OkResponse>("/api/files/mkdir", { json: { path } }),
    upload: (directory: string, file: File) => {
      const form = new FormData();
      form.set("path", joinPath(directory, file.name));
      form.set("file", file);
      return request<OkResponse>("/api/files/upload", { method: "POST", body: form });
    },
    remove: (path: string) =>
      request<OkResponse>(withQuery("/api/files", { path }), { method: "DELETE" }),
  },

  cron: {
    jobs: () => request<{ jobs: CronJob[] }>("/api/cron/jobs"),
    targets: () => request<{ targets: DeliveryTarget[] }>("/api/cron/delivery-targets"),
    create: (input: CronJobInput) => request<CronJob>("/api/cron/jobs", { json: input }),
    update: (id: string, input: CronJobInput) =>
      request<CronJob>(`/api/cron/jobs/${segment(id)}`, { method: "PUT", json: input }),
    pause: (id: string) =>
      request<CronJob>(`/api/cron/jobs/${segment(id)}/pause`, { method: "POST" }),
    resume: (id: string) =>
      request<CronJob>(`/api/cron/jobs/${segment(id)}/resume`, { method: "POST" }),
    trigger: (id: string) =>
      request<CronJob>(`/api/cron/jobs/${segment(id)}/trigger`, { method: "POST" }),
    remove: (id: string) =>
      request<OkResponse>(`/api/cron/jobs/${segment(id)}`, { method: "DELETE" }),
  },

  skills: {
    list: (profile?: string) =>
      request<{ skills: SkillInfo[] }>(forProfile("/api/skills", profile)),
    toggle: (name: string, enabled: boolean, profile?: string) =>
      request<OkResponse>(forProfile("/api/skills/toggle", profile), {
        json: { name, enabled },
      }),
    toolsets: () => request<{ toolsets: ToolsetInfo[] }>("/api/tools/toolsets"),
    hubSearch: (q: string) =>
      request<HubSearchResponse>(withQuery("/api/skills/hub/search", { q })),
    hubInstall: (identifier: string) =>
      request<OkResponse>("/api/skills/hub/install", { json: { identifier } }),
    hubUpdate: (name: string) => request<OkResponse>("/api/skills/hub/update", { json: { name } }),
    hubUninstall: (name: string) =>
      request<OkResponse>("/api/skills/hub/uninstall", { json: { name } }),
  },

  plugins: {
    list: () => request<PluginsResponse>("/api/plugins"),
    setEnabled: (name: string, enabled: boolean) =>
      request<OkResponse>(`/api/plugins/${segment(name)}/${enabled ? "enable" : "disable"}`, {
        method: "POST",
      }),
    install: (body: PluginInstallRequest) =>
      request<OkResponse>("/api/plugins/install", { json: body }),
  },

  mcp: {
    servers: () => request<McpServersResponse>("/api/mcp/servers"),
    add: (body: McpServerCreate, profile?: string) =>
      request<OkResponse>(forProfile("/api/mcp/servers", profile), { json: body }),
    test: (name: string) =>
      request<McpTestResult>(`/api/mcp/servers/${segment(name)}/test`, { method: "POST" }),
    setEnabled: (name: string, enabled: boolean) =>
      request<OkResponse>(`/api/mcp/servers/${segment(name)}/enabled`, {
        method: "PUT",
        json: { enabled },
      }),
    remove: (name: string) =>
      request<OkResponse>(`/api/mcp/servers/${segment(name)}`, { method: "DELETE" }),
  },
};

/** Join a directory and a name with exactly one separator. */
export function joinPath(directory: string, name: string): string {
  if (!directory) return name;
  return directory.endsWith("/") ? `${directory}${name}` : `${directory}/${name}`;
}
