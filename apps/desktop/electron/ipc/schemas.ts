// Payload and answer schemas for every channel in `channels.ts` (zod). Main validates every
// incoming payload against these before a handler runs (P20-03); the bridge typings
// (`src/global.d.ts`) are generated from them (P20-05). Request objects are strict: an unknown key
// is a malformed payload, never silently dropped.
//
// Shapes per kind: invoke { request, response }, send { request }, sync { response },
// event { payload }. `response` and `payload` describe what main sends; main code is typed
// against them (`z.input`), the renderer receives them as written.

import { z } from "zod";
import type { Channel, ChannelKind, ChannelName } from "./channels.ts";

// -- building blocks ----------------------------------------------------------------------------

/** "No payload": the bridge method takes no argument (or `{}`). */
const none = z.strictObject({}).default({});
const ok = z.object({ ok: z.literal(true) });
const accepted = z.object({ accepted: z.boolean() });
const profileName = z.string().min(1).max(64);
const absPath = z.string().min(1).max(4096);
const id = z.string().min(1).max(256);

export const windowKind = z.enum(["main", "secondary", "instance", "hud", "quick", "hidden"]);

export const launchFacts = z.object({
  platform: z.string(),
  appVersion: z.string(),
  packaged: z.boolean(),
  dev: z.boolean(),
  windowKind,
  /** A secondary window opened as a spectator (resume lazily). */
  watch: z.boolean(),
  /** The profile this window boots against, null for the app's active profile. */
  profile: z.string().nullable(),
  titleBarHeight: z.number(),
  deepLinkScheme: z.string(),
});

export const bootPhase = z.enum([
  "resolve",
  "runtime",
  "remote-connect",
  "update-wait",
  "first-run",
  "ready",
  "error",
]);

export const bootProgress = z.object({
  phase: bootPhase,
  message: z.string(),
  progress: z.number().min(0).max(100),
  running: z.boolean(),
  error: z.string().optional(),
  retryable: z.boolean().optional(),
  statusCode: z.number().int().optional(),
  /** A local failure latched in this process (Retry does not re-run the installer). */
  latched: z.boolean().optional(),
});

const stageState = z.enum(["running", "succeeded", "skipped", "failed"]);

export const installerEvent = z.discriminatedUnion("type", [
  z.object({
    type: z.literal("manifest"),
    stages: z.array(
      z.object({
        name: z.string(),
        title: z.string(),
        category: z.string(),
        needsUserInput: z.boolean(),
      }),
    ),
  }),
  z.object({
    type: z.literal("log"),
    stage: z.string().optional(),
    line: z.string(),
    stream: z.enum(["stdout", "stderr"]),
  }),
  z.object({
    type: z.literal("stage"),
    name: z.string(),
    state: stageState,
    error: z.string().optional(),
    durationMs: z.number().optional(),
  }),
  z.object({ type: z.literal("complete"), marker: z.string() }),
  z.object({ type: z.literal("failed"), stage: z.string().optional(), error: z.string() }),
]);

/** The local daemon was restarted after it stopped, or keeps crashing (P20-13). */
export const backendNotice = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("restarted"), message: z.string() }),
  z.object({
    kind: z.literal("crashing"),
    message: z.string(),
    exitCode: z.number().int().nullable(),
    /** The last lines the daemon wrote (redacted). */
    recentOutput: z.array(z.string()).max(40),
  }),
]);

/** Credential-free: tickets come only from `indus:connection:ticket`, tokens never. */
export const connectionDescriptor = z.object({
  mode: z.enum(["local", "remote"]),
  profile: z.string().nullable(),
  profileId: z.string(),
  instanceId: z.string(),
  codeVersion: z.string(),
  /** `ws://127.0.0.1:PORT/ws` with no query, user-info or fragment. */
  wsUrl: z.string(),
});

const legState = z.enum(["ok", "unauthorized", "unreachable", "error"]);

export const remoteTestResult = z.object({
  ok: z.boolean(),
  http: legState,
  ws: legState,
  message: z.string().optional(),
});

/** `url` is normalised by main (a bare `host:port` gets `http://`); a bad one is `bad-request`. */
const remoteInput = z.strictObject({
  url: z.string().min(1).max(2048),
  token: z.string().min(1).max(8192),
});

export const apiRequest = z.strictObject({
  method: z.enum(["GET", "POST", "PUT", "PATCH", "DELETE"]),
  /** Always under `/api/` on the current connection; never a full URL. */
  path: z.string().startsWith("/api/").max(4096),
  body: z.unknown().optional(),
  profile: profileName.optional(),
  timeoutMs: z.number().int().min(1).max(600_000).optional(),
});

export const apiResult = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("response"), status: z.number().int(), body: z.unknown() }),
  /** An expected miss: a value, not an exception. */
  z.object({ kind: z.literal("not-found"), status: z.literal(404), body: z.unknown() }),
]);

export const profileState = z.object({
  active: z.string().nullable(),
  defaultProfile: z.string().nullable(),
});

export const windowState = z.object({
  maximized: z.boolean(),
  fullscreen: z.boolean(),
  focused: z.boolean(),
});

export const zoomState = z.object({ level: z.number(), percent: z.number() });

export const externalOpenResult = z.union([
  z.object({ ok: z.literal(true) }),
  z.object({
    ok: z.literal(false),
    reason: z.enum([
      "invalid",
      "scheme",
      "credentials",
      "too-long",
      "no-user-activation",
      "failed",
    ]),
  }),
]);

const fsEntry = z.object({
  name: z.string(),
  path: z.string(),
  kind: z.enum(["file", "dir", "symlink", "other"]),
  size: z.number().optional(),
  mtimeMs: z.number().optional(),
});

const gitFile = z.object({ path: z.string(), index: z.string(), worktree: z.string() });

export const updateStatus = z.object({
  owner: z.enum(["electron-updater", "manual"]),
  channel: z.enum(["stable", "canary"]),
  state: z.enum(["idle", "checking", "available", "downloading", "ready", "error", "manual"]),
  version: z.string().optional(),
  progress: z.number().min(0).max(100).optional(),
  error: z.string().optional(),
  /** For the manual owner: the command or link to show. */
  manualHint: z.string().optional(),
});

export const secretsPolicyView = z.object({
  /** What the person chose (default keychain). */
  store: z.enum(["keychain", "file"]),
  /** What this run uses (file when the keychain is unusable). */
  effective: z.enum(["keychain", "file"]),
  keychainUsable: z.boolean(),
  reason: z.string(),
});

const journalEntry = z.object({
  /** The client's id for the input (`request_id` on `prompt.submit`). */
  requestId: id,
  sessionId: id,
  createdAt: z.number(),
  updatedAt: z.number(),
  /** `uncertain`: the last send had no definite answer; offer Retry / Dismiss. */
  state: z.enum(["prepared", "uncertain"]),
  /** The daemon's admission id, once an answer named one. */
  admissionId: id.optional(),
  payload: z.unknown(),
});

// -- the table -----------------------------------------------------------------------------------

type InvokeSchemas = { request: z.ZodType; response: z.ZodType };
type SendSchemas = { request: z.ZodType };
type SyncSchemas = { response: z.ZodType };
type EventSchemas = { payload: z.ZodType };

export type SchemasFor<K extends ChannelKind> = K extends "invoke"
  ? InvokeSchemas
  : K extends "send"
    ? SendSchemas
    : K extends "sync"
      ? SyncSchemas
      : EventSchemas;

type SchemaTable = {
  readonly [N in ChannelName]: SchemasFor<Extract<Channel, { name: N }>["kind"]>;
};

export const SCHEMAS = {
  "indus:facts": { response: launchFacts },

  "indus:boot:get": { request: none, response: bootProgress },
  "indus:boot:progress": { payload: bootProgress },
  "indus:boot:retry": {
    request: z.strictObject({ action: z.enum(["retry", "repair", "reset"]) }),
    response: accepted,
  },
  "indus:boot:choose": {
    request: z.strictObject({ choice: z.enum(["install-local", "connect-remote", "reset"]) }),
    response: accepted,
  },
  "indus:installer:event": { payload: installerEvent },
  "indus:installer:cancel": { request: none, response: accepted },
  "indus:backend:notice": { payload: backendNotice },
  "indus:connection:get": {
    request: z.strictObject({ profile: profileName.optional() }).default({}),
    response: connectionDescriptor,
  },
  "indus:connection:ticket": {
    request: z.strictObject({ profile: profileName.optional() }).default({}),
    response: z.object({
      ticket: z.string(),
      wsUrl: z.string(),
      expiresInMs: z.number(),
      /** Offer exactly these WebSocket subprotocols, in order (the URL carries no credential). */
      protocols: z.array(z.string()).min(1).max(4),
    }),
  },
  "indus:connection:changed": { payload: connectionDescriptor.nullable() },
  "indus:remote:get": {
    request: none,
    response: z.object({ url: z.string().nullable(), hasToken: z.boolean() }),
  },
  "indus:remote:test": { request: remoteInput, response: remoteTestResult },
  "indus:remote:save": {
    request: remoteInput,
    response: remoteTestResult.extend({ saved: z.boolean() }),
  },
  "indus:remote:forget": { request: none, response: ok },

  "indus:api": { request: apiRequest, response: apiResult },

  "indus:profile:get": { request: none, response: profileState },
  "indus:profile:set": {
    request: z.strictObject({ profile: profileName.nullable() }),
    response: profileState,
  },
  "indus:profile:set-default": {
    request: z.strictObject({ profile: profileName.nullable() }),
    response: profileState,
  },
  "indus:profile:changed": { payload: profileState },

  "indus:window:control": {
    request: z.strictObject({ action: z.enum(["minimize", "toggle-maximize", "close"]) }),
  },
  "indus:window:painted": { request: none },
  "indus:window:theme": {
    request: z.strictObject({
      mode: z.enum(["light", "dark", "system"]),
      titleBar: z
        .strictObject({
          color: z.string().regex(/^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/),
          symbolColor: z.string().regex(/^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/),
        })
        .optional(),
    }),
  },
  "indus:window:state": { payload: windowState },
  "indus:window:open-session": {
    request: z.strictObject({
      sessionId: id,
      watch: z.boolean().optional(),
      profile: profileName.optional(),
    }),
    response: z.object({ opened: z.boolean() }),
  },
  "indus:window:open-instance": {
    request: z.strictObject({ profile: profileName.optional() }).default({}),
    response: z.object({ opened: z.boolean() }),
  },

  "indus:zoom:get": { request: none, response: zoomState },
  "indus:zoom:set": {
    request: z.strictObject({ level: z.number().finite() }),
    response: zoomState,
  },
  "indus:zoom:step": {
    request: z.strictObject({ direction: z.enum(["in", "out", "reset"]) }),
    response: zoomState,
  },
  "indus:zoom:changed": { payload: zoomState },

  "indus:external:open": {
    request: z.strictObject({
      url: z.string().max(8192),
      /** The preview guest the person clicked in (P20-28); absent for the window itself. */
      guestContentsId: z.number().int().nonnegative().optional(),
    }),
    response: externalOpenResult,
  },

  "indus:fs:read-dir": {
    request: z.strictObject({ path: absPath, workspace: absPath.optional() }),
    response: z.object({ entries: z.array(fsEntry) }),
  },
  "indus:fs:read-text": {
    request: z.strictObject({ path: absPath, maxBytes: z.number().int().positive().optional() }),
    response: z.object({ text: z.string(), truncated: z.boolean(), size: z.number() }),
  },
  "indus:fs:read-data-url": {
    request: z.strictObject({ path: absPath, purpose: z.enum(["preview", "attach"]) }),
    response: z.object({ dataUrl: z.string(), size: z.number(), mimeType: z.string() }),
  },
  "indus:fs:rename": {
    request: z.strictObject({ from: absPath, to: absPath }),
    response: z.object({ path: z.string() }),
  },
  "indus:fs:trash": { request: z.strictObject({ path: absPath }), response: ok },
  "indus:fs:reveal": { request: z.strictObject({ path: absPath }), response: ok },
  "indus:fs:select-paths": {
    request: z.strictObject({
      mode: z.enum(["open-file", "open-dir", "save"]),
      multiple: z.boolean().optional(),
      title: z.string().max(256).optional(),
      defaultPath: absPath.optional(),
    }),
    response: z.object({ paths: z.array(z.string()), canceled: z.boolean() }),
  },
  "indus:fs:watch": {
    request: z.strictObject({ path: absPath }),
    response: z.object({ watchId: z.string() }),
  },
  "indus:fs:unwatch": { request: z.strictObject({ watchId: id }), response: ok },
  "indus:fs:changed": {
    payload: z.object({
      watchId: z.string(),
      path: z.string(),
      kind: z.enum(["add", "change", "unlink"]),
    }),
  },

  "indus:git:status": {
    request: z.strictObject({ repo: absPath }),
    response: z.object({
      branch: z.string().nullable(),
      upstream: z.string().nullable(),
      ahead: z.number().int(),
      behind: z.number().int(),
      files: z.array(gitFile),
    }),
  },
  "indus:git:diff": {
    request: z.strictObject({
      repo: absPath,
      scope: z.enum(["uncommitted", "branch", "last-turn"]),
      path: z.string().max(4096).optional(),
    }),
    response: z.object({ diff: z.string(), readOnly: z.boolean() }),
  },
  "indus:git:stage": {
    request: z.strictObject({ repo: absPath, paths: z.array(z.string().max(4096)) }),
    response: ok,
  },
  "indus:git:unstage": {
    request: z.strictObject({ repo: absPath, paths: z.array(z.string().max(4096)) }),
    response: ok,
  },
  "indus:git:revert": {
    request: z.strictObject({ repo: absPath, paths: z.array(z.string().max(4096)) }),
    response: ok,
  },
  "indus:git:commit": {
    request: z.strictObject({
      repo: absPath,
      message: z.string().min(1).max(65_536),
      push: z.boolean().optional(),
    }),
    response: z.object({ commit: z.string(), pushed: z.boolean() }),
  },
  "indus:git:create-pr": {
    request: z.strictObject({
      repo: absPath,
      title: z.string().min(1).max(256),
      body: z.string().max(65_536).optional(),
    }),
    response: z.object({ url: z.string().nullable(), ghAvailable: z.boolean() }),
  },

  "indus:terminal:start": {
    request: z.strictObject({
      cwd: absPath.optional(),
      cols: z.number().int().min(1).max(1000),
      rows: z.number().int().min(1).max(1000),
    }),
    response: z.object({ terminalId: z.string(), pid: z.number().int() }),
  },
  "indus:terminal:write": {
    request: z.strictObject({ terminalId: id, data: z.string().max(1_048_576) }),
  },
  "indus:terminal:resize": {
    request: z.strictObject({
      terminalId: id,
      cols: z.number().int().min(1).max(1000),
      rows: z.number().int().min(1).max(1000),
    }),
  },
  "indus:terminal:dispose": { request: z.strictObject({ terminalId: id }), response: ok },
  "indus:terminal:data": { payload: z.object({ terminalId: z.string(), data: z.string() }) },
  "indus:terminal:exit": {
    payload: z.object({
      terminalId: z.string(),
      exitCode: z.number().int().nullable(),
      signal: z.string().nullable(),
    }),
  },

  "indus:preview:capture": {
    request: z.strictObject({ guestContentsId: z.number().int().nonnegative() }),
    response: z.object({ dataUrl: z.string() }),
  },
  "indus:preview:watch-file": {
    request: z.strictObject({ path: absPath }),
    response: z.object({ watchId: z.string() }),
  },
  "indus:preview:unwatch-file": { request: z.strictObject({ watchId: id }), response: ok },
  "indus:preview:file-changed": { payload: z.object({ watchId: z.string(), path: z.string() }) },
  "indus:preview:close-requested": { payload: z.object({}) },

  "indus:updates:status": { request: none, response: updateStatus },
  "indus:updates:check": { request: none, response: updateStatus },
  "indus:updates:apply": { request: none, response: accepted },
  "indus:updates:set-channel": {
    request: z.strictObject({ channel: z.enum(["stable", "canary"]) }),
    response: updateStatus,
  },
  "indus:updates:progress": { payload: updateStatus },

  "indus:notify": {
    request: z.strictObject({
      title: z.string().min(1).max(256),
      body: z.string().max(4096).optional(),
      tag: z.string().max(256).optional(),
      actions: z
        .array(z.strictObject({ id, label: z.string().min(1).max(64) }))
        .max(4)
        .optional(),
    }),
    response: z.object({ shown: z.boolean() }),
  },
  "indus:notification:action": {
    payload: z.object({ tag: z.string().nullable(), actionId: z.string().nullable() }),
  },

  "indus:deep-link": {
    payload: z.object({
      kind: z.enum(["open", "skill", "mcp", "close-preview"]),
      name: z.string(),
      params: z.record(z.string(), z.string()),
    }),
  },
  "indus:deep-link:ready": { request: none },

  "indus:logs:recent": {
    request: z.strictObject({ limit: z.number().int().min(1).max(5000).optional() }).default({}),
    response: z.object({ lines: z.array(z.string()) }),
  },
  "indus:logs:reveal": { request: none, response: ok },
  "indus:logs:renderer-error": {
    request: z.strictObject({
      message: z.string().max(8192),
      stack: z.string().max(65_536).optional(),
      source: z.string().max(1024).optional(),
    }),
  },

  "indus:secrets:policy:get": { request: none, response: secretsPolicyView },
  "indus:secrets:policy:set": {
    request: z.strictObject({ store: z.enum(["keychain", "file"]) }),
    response: secretsPolicyView,
  },

  "indus:journal:list": { request: none, response: z.object({ entries: z.array(journalEntry) }) },
  "indus:journal:prepare": {
    request: z.strictObject({ requestId: id, sessionId: id, payload: z.unknown() }),
    response: ok,
  },
  "indus:journal:retire": {
    request: z.strictObject({
      requestId: id,
      receipt: z.strictObject({
        outcome: z.enum(["accepted", "rejected", "unknown"]),
        admissionId: id.optional(),
      }),
    }),
    response: z.object({ retired: z.boolean() }),
  },

  "indus:work:active": {
    request: z.strictObject({
      items: z.array(z.strictObject({ id, label: z.string().max(256) })).max(64),
    }),
  },
} as const satisfies SchemaTable;

export type Schemas = typeof SCHEMAS;

/** What a handler receives for `N` (after parsing and defaults). */
export type RequestOf<N extends ChannelName> = Schemas[N] extends {
  request: infer S extends z.ZodType;
}
  ? z.output<S>
  : never;
/** What a handler returns for `N`. */
export type ResponseOf<N extends ChannelName> = Schemas[N] extends {
  response: infer S extends z.ZodType;
}
  ? z.input<S>
  : never;
/** What main pushes on event channel `N`. */
export type PayloadOf<N extends ChannelName> = Schemas[N] extends {
  payload: infer S extends z.ZodType;
}
  ? z.input<S>
  : never;
