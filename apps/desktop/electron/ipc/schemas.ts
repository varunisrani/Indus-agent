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

export const windowKind = z.enum([
  "main",
  "secondary",
  "instance",
  "hud",
  "quick",
  "pet",
  "wake",
  "hidden",
]);

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
  /** The person's home folder (the default root of the repo scan, P20-25); "" when unknown. */
  homeDir: z.string(),
  titleBarHeight: z.number(),
  deepLinkScheme: z.string(),
});

// -- P24-19 pet, P24-20 wake indicator, P24-33 marketplace themes ---------------------------------

/** A mood picture: an image data URI (PNG, JPEG or WebP), at most 12 MiB of text. */
const moodPicture = z
  .string()
  .max(12 * 1024 * 1024)
  .regex(/^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/);

export const petView = z.object({
  visible: z.boolean(),
  name: z.string().nullable(),
  /** 0.25-3 (`pet.scale`). */
  scale: z.number(),
  revision: z.string().nullable(),
  moods: z.record(z.string(), z.string()),
  /** A short status line in the speech bubble, or null. */
  bubble: z.string().nullable(),
});

export const wakeIndicatorState = z.enum(["hidden", "listening", "heard", "capturing"]);

export const marketplaceItem = z.object({
  publisher: z.string(),
  extension: z.string(),
  displayName: z.string(),
  description: z.string(),
  version: z.string(),
  installs: z.number(),
});

const marketplaceName = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z0-9][A-Za-z0-9._-]*$/);

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
const connectionLeg = z.enum(["ok", "unauthorized", "unreachable", "error"]);
const connectionTest = z.object({
  ok: z.boolean(),
  http: connectionLeg,
  ws: connectionLeg,
  nativeSignIn: z.boolean(),
  message: z.string().optional(),
});
const sshTarget = z.strictObject({
  host: z.string().min(1).max(255),
  user: z.string().min(1).max(64).optional(),
  port: z.number().int().min(1).max(65_535).optional(),
  keyPath: z.string().min(1).max(4096).optional(),
  remoteProfile: z.string().min(1).max(64).optional(),
});
const connectionInput = z.strictObject({
  id: z.string().min(1).max(64).optional(),
  kind: z.enum(["remote", "ssh"]),
  label: z.string().max(64).optional(),
  url: z.string().max(2048).optional(),
  authMode: z.enum(["token", "oauth", "basic"]).optional(),
  ssh: sshTarget.optional(),
  token: z.string().max(4096).optional(),
  username: z.string().max(256).optional(),
  password: z.string().max(4096).optional(),
  headers: z.record(z.string().max(128), z.string().max(4096)).optional(),
});
const connectionsView = z.object({
  connections: z.array(
    z.object({
      id: z.string(),
      kind: z.enum(["local", "remote", "ssh"]),
      label: z.string(),
      url: z.string().optional(),
      authMode: z.enum(["token", "oauth", "basic"]).optional(),
      ssh: z
        .object({
          host: z.string(),
          user: z.string().optional(),
          port: z.number().optional(),
          keyPath: z.string().optional(),
          remoteProfile: z.string().optional(),
        })
        .optional(),
      primary: z.boolean(),
      lastUsed: z.boolean(),
      hasCredential: z.boolean(),
      signedIn: z.boolean(),
      headerNames: z.array(z.string()),
    }),
  ),
  primaryId: z.string(),
  launchMode: z.enum(["primary", "last-used"]),
  quarantined: z.number().int(),
});
const connectionId = z.strictObject({ id: z.string().min(1).max(64) });

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
  /** The profile main's boot attempt runs (P20-26: one decision per attempt). */
  launch: z.string().nullable(),
});

const profileChange = z.enum(["rename", "delete"]);

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
  /** For a symlink: what it points at (`missing` when broken). */
  target: z.enum(["file", "dir", "other", "missing"]).optional(),
  size: z.number().optional(),
  mtimeMs: z.number().optional(),
  /** A key store or `.env`-like file: listed, never read into a renderer (P20-29). */
  sensitive: z.boolean().optional(),
});

export const fsLimits = z.object({
  /** The Settings value for preview data-URL reads, in MiB. */
  dataUrlMaxMb: z.number().int(),
  dataUrlDefaultMb: z.number().int(),
  dataUrlMinMb: z.number().int(),
  dataUrlCeilingMb: z.number().int(),
  /** Attachments (bytes uploaded or a path handed to the agent). */
  attachMaxBytes: z.number().int(),
  /** Text previews. */
  textPreviewMaxBytes: z.number().int(),
});

/** One changed file. `index` / `worktree` are porcelain codes (`M`, `A`, `D`, `R`, `?`, `U`, ` `). */
const gitFile = z.object({
  path: z.string(),
  /** The path before a rename or copy. */
  from: z.string().optional(),
  index: z.string(),
  worktree: z.string(),
  added: z.number().int().optional(),
  removed: z.number().int().optional(),
});

const gitScope = z.enum(["uncommitted", "branch", "last-turn"]);
/** A commit id: the last-turn baseline (never a ref name, so never an option). */
const commitId = z.string().regex(/^[0-9a-f]{7,64}$/);
const repoRelPath = z.string().min(1).max(4096);

export const updateChannel = z.enum(["stable", "canary"]);

export const updateStatus = z.object({
  /** Declared by the build stamp (P20-38); never inferred at run time. */
  owner: z.enum(["electron-updater", "manual"]),
  channel: updateChannel,
  /**
   * idle: nothing newer (or not checked yet); available: `version` can be applied; manual: a newer
   * `version` exists and `manualHint` / `downloadUrl` say how to get it; ready: downloaded and
   * accepted; installing: quitting to install.
   */
  state: z.enum([
    "idle",
    "checking",
    "available",
    "downloading",
    "ready",
    "installing",
    "error",
    "manual",
  ]),
  currentVersion: z.string(),
  version: z.string().optional(),
  progress: z.number().min(0).max(100).optional(),
  error: z.string().optional(),
  /** For the manual owner: the command to run. */
  manualHint: z.string().optional(),
  /** For the manual owner: the release page (opened through `external.open` by a click). */
  downloadUrl: z.string().optional(),
  /** When the last check finished (ms since the epoch). */
  checkedAt: z.number().optional(),
});

/** A renderer, bundle or runtime generation mismatch: offer update/reinstall (R08.14). */
export const updateSkew = z.object({
  kind: z.enum(["mixed-generations", "app-behind-runtime", "renderer-stale"]),
  detail: z.string(),
});

export const hudState = z.object({
  open: z.boolean(),
  sessionId: z.string().nullable(),
  profile: z.string().nullable(),
});

export const shortcutAction = z.enum(["hud-toggle", "hud-snap", "quick-entry"]);

export const shortcutRefusal = z.enum([
  "empty",
  "no-modifier",
  "no-key",
  "invalid-key",
  "invalid-modifier",
  "reserved",
]);

export const shortcutsView = z.object({
  shortcuts: z.array(
    z.object({
      action: shortcutAction,
      accelerator: z.string(),
      enabled: z.boolean(),
      /** True when the OS accepted the chord (the hud-snap chord only while a HUD is open). */
      registered: z.boolean(),
      error: z.enum(["invalid", "taken", "conflict", "failed"]).nullable(),
      /** Set with `conflict`: the action that already holds the chord. */
      conflictWith: shortcutAction.optional(),
    }),
  ),
  /** A `set` whose accelerator was refused (nothing saved). */
  rejected: z.object({ action: shortcutAction, reason: shortcutRefusal }).optional(),
});

export const keepAwakeMode = z.enum(["never", "while-working", "always"]);

export const ambientState = z.object({
  tray: z.object({
    /** The person's choice (per device, off by default). */
    enabled: z.boolean(),
    /** A tray is really shown (Linux: a StatusNotifier host is registered). */
    available: z.boolean(),
    reason: z.string().nullable(),
  }),
  keepAwake: keepAwakeMode,
  notifications: z.boolean(),
  sounds: z.boolean(),
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

const accelerator = z.string().max(128);

/** Settings main holds for this device (P20-32); P20-35/36 read them in main. */
export const prefsSnapshot = z.object({
  /** Closing or minimising hides to the tray (off by default, per device). */
  minimizeToTray: z.boolean(),
  keepAwake: z.enum(["never", "while-working", "always"]),
  /** Global shortcuts; an empty string turns one off. */
  quickEntryShortcut: accelerator,
  hudSnapShortcut: accelerator,
  hudToggleShortcut: accelerator,
  notifications: z.object({
    turnComplete: z.boolean(),
    approvals: z.boolean(),
    sounds: z.boolean(),
  }),
  /** In-app shortcut overrides: action id → accelerator ("" = unbound). */
  keybindings: z.record(z.string().max(64), accelerator),
  /** Chat text size in percent of the UI size. */
  chatTextScale: z.number().int().min(80).max(160),
});

export const prefsPatch = z.strictObject({
  minimizeToTray: z.boolean().optional(),
  keepAwake: z.enum(["never", "while-working", "always"]).optional(),
  quickEntryShortcut: accelerator.optional(),
  hudSnapShortcut: accelerator.optional(),
  hudToggleShortcut: accelerator.optional(),
  notifications: z
    .strictObject({
      turnComplete: z.boolean().optional(),
      approvals: z.boolean().optional(),
      sounds: z.boolean().optional(),
    })
    .optional(),
  keybindings: z.record(z.string().max(64), accelerator).optional(),
  chatTextScale: z.number().int().min(80).max(160).optional(),
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

// -- desktop plugins (P24-32) and uninstall (P24-34) --------------------------------------------

export const pluginFile = z.object({
  id: z.string(),
  kind: z.enum(["disk", "package"]),
  file: z.string(),
  size: z.number().int().min(0),
  /** The module's name under `indus-plugin://disk/<hash>.js`; "" when refused. */
  hash: z.string(),
  status: z.enum(["ready", "refused"]),
  reason: z.string().optional(),
  packageName: z.string().optional(),
});

export const uninstallLevel = z.enum(["gui", "keep-data", "full"]);

export const uninstallSummary = z.object({
  /** False hides every option (`reason` says why). */
  allowed: z.boolean(),
  levels: z.array(uninstallLevel),
  reason: z.string().optional(),
  installKind: z.string(),
  home: z.string(),
  /** The exact words "Everything" asks the person to type. */
  confirmPhrase: z.string(),
});

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

  "indus:connections:list": { request: none, response: connectionsView },
  "indus:connections:test": { request: connectionInput, response: connectionTest },
  "indus:connections:save": {
    request: connectionInput,
    response: z.object({
      saved: z.boolean(),
      id: z.string().optional(),
      test: connectionTest.nullable(),
      droppedHeaders: z.array(z.string()),
      message: z.string().optional(),
    }),
  },
  "indus:connections:remove": { request: connectionId, response: connectionsView },
  "indus:connections:set-primary": { request: connectionId, response: connectionsView },
  "indus:connections:set-launch-mode": {
    request: z.strictObject({ mode: z.enum(["primary", "last-used"]) }),
    response: connectionsView,
  },
  "indus:connections:sign-in": {
    request: connectionId,
    response: z.object({ ok: z.boolean(), message: z.string().optional() }),
  },
  "indus:connections:sign-out": { request: connectionId, response: ok },

  "indus:artifacts:download": {
    request: z.strictObject({
      path: z.string().min(1).max(4096),
      profile: profileName.nullable(),
      sessionId: id,
    }),
    response: z.object({ savedTo: z.string() }),
  },

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
  "indus:profile:prepare-change": {
    request: z.strictObject({ action: profileChange, profile: profileName }),
    response: ok,
  },
  "indus:profile:commit-change": {
    request: z.strictObject({
      action: profileChange,
      profile: profileName,
      to: profileName.optional(),
    }),
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
    request: z.strictObject({ path: absPath }),
    response: z.object({ path: z.string(), entries: z.array(fsEntry), truncated: z.boolean() }),
  },
  "indus:fs:read-text": {
    request: z.strictObject({
      path: absPath,
      maxBytes: z.number().int().positive().max(16_777_216).optional(),
    }),
    response: z.object({
      text: z.string(),
      truncated: z.boolean(),
      size: z.number(),
      /** Not text (a NUL byte in the first bytes); `text` is empty. */
      binary: z.boolean(),
    }),
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
  "indus:fs:set-roots": {
    /** The workspace folders this window works in (replaces the previous set; home is implicit). */
    request: z.strictObject({ roots: z.array(absPath).max(32) }),
    response: z.object({ roots: z.array(z.string()), refused: z.array(z.string()) }),
  },
  "indus:fs:stat": {
    request: z.strictObject({ path: absPath, purpose: z.enum(["preview", "attach"]) }),
    response: z.object({
      path: z.string(),
      name: z.string(),
      kind: z.enum(["file", "dir", "other"]),
      size: z.number(),
      mimeType: z.string(),
    }),
  },
  "indus:fs:limits:get": { request: none, response: fsLimits },
  "indus:fs:limits:set": {
    request: z.strictObject({ dataUrlMaxMb: z.number().finite() }),
    response: fsLimits,
  },

  "indus:git:status": {
    request: z.strictObject({ repo: absPath }),
    response: z.object({
      /** The repository's top level (what every other call should pass as `repo`). */
      root: z.string(),
      branch: z.string().nullable(),
      upstream: z.string().nullable(),
      ahead: z.number().int(),
      behind: z.number().int(),
      /** HEAD's commit id; null before the first commit. */
      head: z.string().nullable(),
      files: z.array(gitFile),
      truncated: z.boolean(),
    }),
  },
  "indus:git:diff": {
    request: z.strictObject({
      repo: absPath,
      scope: gitScope,
      path: repoRelPath.optional(),
      base: commitId.optional(),
      staged: z.boolean().optional(),
    }),
    response: z.object({ diff: z.string(), readOnly: z.boolean(), truncated: z.boolean() }),
  },
  "indus:git:stage": {
    request: z.strictObject({ repo: absPath, paths: z.array(repoRelPath).max(2000) }),
    response: ok,
  },
  "indus:git:unstage": {
    request: z.strictObject({ repo: absPath, paths: z.array(repoRelPath).max(2000) }),
    response: ok,
  },
  "indus:git:revert": {
    request: z.strictObject({ repo: absPath, paths: z.array(repoRelPath).max(2000) }),
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
  "indus:git:changes": {
    request: z.strictObject({ repo: absPath, scope: gitScope, base: commitId.optional() }),
    response: z.object({
      files: z.array(gitFile),
      /** The commit the scope compares against (null: none found / uncommitted). */
      base: z.string().nullable(),
      readOnly: z.boolean(),
      truncated: z.boolean(),
    }),
  },
  "indus:git:commit-context": {
    request: z.strictObject({ repo: absPath }),
    response: z.object({
      diff: z.string(),
      untracked: z.array(z.string()),
      recent: z.array(z.string()),
    }),
  },

  "indus:terminal:start": {
    request: z.strictObject({
      cwd: absPath.optional(),
      cols: z.number().int().min(1).max(1000),
      rows: z.number().int().min(1).max(1000),
    }),
    response: z.object({
      terminalId: z.string(),
      pid: z.number().int(),
      /** The shell's name (`zsh`, `bash`, `pwsh`…). */
      shell: z.string(),
      cwd: z.string(),
    }),
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
    request: z.strictObject({ channel: updateChannel }),
    response: updateStatus,
  },
  "indus:updates:progress": { payload: updateStatus },
  "indus:updates:skew": { payload: updateSkew },

  "indus:hud:open": {
    request: z
      .strictObject({ sessionId: id.nullable().optional(), profile: profileName.optional() })
      .default({}),
    response: hudState,
  },
  "indus:hud:close": { request: none, response: hudState },
  "indus:hud:reset-layout": { request: none, response: z.object({ reset: z.boolean() }) },
  "indus:hud:drag": {
    request: z.strictObject({
      phase: z.enum(["start", "end"]),
      /** `move`, or the edge / corner being dragged. */
      mode: z.enum(["move", "n", "s", "e", "w", "ne", "nw", "se", "sw"]),
    }),
  },
  "indus:hud:changed": { payload: hudState },
  "indus:quick:submit": {
    request: z.strictObject({ text: z.string().max(100_000) }),
    response: z.union([
      z.object({ ok: z.literal(true), sessionId: z.string().nullable() }),
      z.object({
        ok: z.literal(false),
        /** `unconfirmed`: no answer yet — it may still arrive; never resend by itself. */
        code: z.enum(["empty", "no-target", "unconfirmed", "failed", "busy"]),
        message: z.string(),
      }),
    ]),
  },
  "indus:quick:dismiss": { request: none },
  "indus:quick:shown": { payload: z.object({}) },
  "indus:quick:deliver": {
    payload: z.object({
      requestId: z.string(),
      text: z.string(),
      /** The window's last-active session, null for a new chat. */
      sessionId: z.string().nullable(),
    }),
  },
  "indus:quick:ack": {
    request: z.strictObject({
      requestId: id,
      ok: z.boolean(),
      sessionId: id.nullable().optional(),
      error: z.string().max(512).optional(),
    }),
  },
  "indus:shortcuts:get": { request: none, response: shortcutsView },
  "indus:shortcuts:set": {
    request: z.strictObject({
      action: shortcutAction,
      accelerator: z.string().max(128).optional(),
      enabled: z.boolean().optional(),
    }),
    response: shortcutsView,
  },
  "indus:session:active": { request: z.strictObject({ sessionId: id.nullable() }) },

  "indus:ambient:get": { request: none, response: ambientState },
  "indus:ambient:set": {
    request: z.strictObject({
      minimizeToTray: z.boolean().optional(),
      keepAwake: keepAwakeMode.optional(),
      notifications: z.boolean().optional(),
      sounds: z.boolean().optional(),
    }),
    response: ambientState,
  },
  "indus:ambient:changed": { payload: ambientState },
  "indus:keep-awake:offer": { payload: z.object({}) },
  "indus:links:preview": {
    request: z.strictObject({ url: z.string().min(1).max(2048) }),
    response: z.object({ title: z.string().nullable(), favicon: z.string().nullable() }),
  },

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

  "indus:about:notices": {
    request: none,
    // null when the file is not beside the app (a development run) or cannot be read
    response: z.object({ text: z.string().nullable() }),
  },

  "indus:diagnostics:copy": {
    request: none,
    response: z.object({ copied: z.boolean(), bytes: z.number().int().min(0) }),
  },
  "indus:clipboard:read-text": { request: none, response: z.object({ text: z.string() }) },
  "indus:clipboard:write-text": {
    request: z.strictObject({ text: z.string().max(1024 * 1024) }),
    response: ok,
  },
  "indus:find:open": { payload: z.object({}) },

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

  "indus:prefs:get": { request: none, response: prefsSnapshot },
  "indus:prefs:set": { request: prefsPatch, response: prefsSnapshot },
  "indus:prefs:changed": { payload: prefsSnapshot },

  "indus:work:active": {
    request: z.strictObject({
      items: z.array(z.strictObject({ id, label: z.string().max(256) })).max(64),
    }),
  },

  "indus:pet:show": {
    request: z.strictObject({
      name: z.string().min(1).max(64),
      scale: z.number().min(0.25).max(3),
      revision: z.string().max(64),
      moods: z.record(z.string().regex(/^[a-z]{1,16}$/), moodPicture),
    }),
    response: z.object({ visible: z.boolean() }),
  },
  "indus:pet:hide": { request: none, response: z.object({ visible: z.boolean() }) },
  "indus:pet:say": { request: z.strictObject({ text: z.string().max(200) }) },
  "indus:pet:get": { request: none, response: petView },
  "indus:pet:state": { payload: petView },
  "indus:pet:pointer": { request: z.strictObject({ over: z.boolean() }) },

  "indus:wake:indicator": { request: z.strictObject({ state: wakeIndicatorState }) },
  "indus:wake:indicator-get": {
    request: none,
    response: z.object({ state: wakeIndicatorState }),
  },
  "indus:wake:indicator-state": { payload: z.object({ state: wakeIndicatorState }) },

  "indus:themes:search": {
    request: z.strictObject({ query: z.string().max(200) }),
    response: z.object({ items: z.array(marketplaceItem) }),
  },
  "indus:themes:download": {
    request: z.strictObject({
      publisher: marketplaceName,
      extension: marketplaceName,
      version: z.string().regex(/^[0-9A-Za-z.+-]{1,40}$/),
    }),
    /** The extension package (a zip), base64. */
    response: z.object({ vsixBase64: z.string() }),
  },

  "indus:plugins:list": { request: none, response: z.object({ files: z.array(pluginFile) }) },
  "indus:plugins:rescan": { request: none, response: z.object({ files: z.array(pluginFile) }) },
  "indus:plugins:changed": { payload: z.object({ files: z.array(pluginFile) }) },

  "indus:uninstall:summary": { request: none, response: uninstallSummary },
  "indus:uninstall:run": {
    request: z.strictObject({ level: uninstallLevel, typed: z.string().max(64).optional() }),
    response: z.union([
      z.object({ ok: z.literal(true) }),
      z.object({ ok: z.literal(false), code: z.string(), message: z.string() }),
    ]),
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
