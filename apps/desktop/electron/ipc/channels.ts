// The desktop's IPC surface: every channel between a renderer and main, in one table.
//
// This file is plain data (no zod) so the sandboxed preload can bundle it; the payload and answer
// schemas live beside it in `schemas.ts` (main and the type generator only), and a compile-time
// check there makes the two tables cover exactly the same names. `window.indusDesktop` is built
// from this table (`bridge.ts`), and `src/global.d.ts` is generated from both
// (`scripts/gen-bridge-types.mjs`).
//
// Kinds:
//   invoke  renderer asks, main answers (Promise)        `indusDesktop.<group>.<method>(payload)`
//   send    renderer tells main, no answer               `indusDesktop.<group>.<method>(payload)`
//   sync    launch facts read once when the preload runs `indusDesktop.<group>` (a plain object)
//   event   main pushes to a window                      `indusDesktop.<group>.<method>(listener)`
//
// `owner` names the task that registers the handler (or sends the event). Until it lands, main's
// registry answers a declared invoke channel with `not-available` (see `registry.seal`).

export type ChannelKind = "invoke" | "send" | "sync" | "event";

export interface ChannelDef {
  readonly name: `indus:${string}`;
  readonly kind: ChannelKind;
  readonly group: string;
  /** Bridge method name; `null` for a `sync` channel, which is the group's value itself. */
  readonly method: string | null;
  readonly owner: string;
}

export const CHANNELS = [
  // launch facts
  { name: "indus:facts", kind: "sync", group: "facts", method: null, owner: "P20-05" },

  // connection & boot
  { name: "indus:boot:get", kind: "invoke", group: "boot", method: "get", owner: "P20-12" },
  {
    name: "indus:boot:progress",
    kind: "event",
    group: "boot",
    method: "onProgress",
    owner: "P20-12",
  },
  { name: "indus:boot:retry", kind: "invoke", group: "boot", method: "retry", owner: "P20-12" },
  { name: "indus:boot:choose", kind: "invoke", group: "boot", method: "choose", owner: "P20-12" },
  {
    name: "indus:installer:event",
    kind: "event",
    group: "boot",
    method: "onInstaller",
    owner: "P20-09",
  },
  {
    name: "indus:installer:cancel",
    kind: "invoke",
    group: "boot",
    method: "cancelInstall",
    owner: "P20-09",
  },
  {
    name: "indus:backend:notice",
    kind: "event",
    group: "boot",
    method: "onNotice",
    owner: "P20-13",
  },
  {
    name: "indus:connection:get",
    kind: "invoke",
    group: "connection",
    method: "get",
    owner: "P20-10",
  },
  {
    name: "indus:connection:ticket",
    kind: "invoke",
    group: "connection",
    method: "ticket",
    owner: "P20-10",
  },
  {
    name: "indus:connection:changed",
    kind: "event",
    group: "connection",
    method: "onChanged",
    owner: "P20-10",
  },
  {
    name: "indus:remote:get",
    kind: "invoke",
    group: "connection",
    method: "getRemote",
    owner: "P20-11",
  },
  {
    name: "indus:remote:test",
    kind: "invoke",
    group: "connection",
    method: "testRemote",
    owner: "P20-11",
  },
  {
    name: "indus:remote:save",
    kind: "invoke",
    group: "connection",
    method: "saveRemote",
    owner: "P20-11",
  },
  {
    name: "indus:remote:forget",
    kind: "invoke",
    group: "connection",
    method: "forgetRemote",
    owner: "P20-11",
  },

  // REST proxy
  { name: "indus:api", kind: "invoke", group: "api", method: "request", owner: "P20-14" },

  // profile
  { name: "indus:profile:get", kind: "invoke", group: "profile", method: "get", owner: "P20-26" },
  { name: "indus:profile:set", kind: "invoke", group: "profile", method: "set", owner: "P20-26" },
  {
    name: "indus:profile:set-default",
    kind: "invoke",
    group: "profile",
    method: "setDefault",
    owner: "P20-26",
  },
  {
    name: "indus:profile:changed",
    kind: "event",
    group: "profile",
    method: "onChanged",
    owner: "P20-26",
  },

  // windows
  {
    name: "indus:window:control",
    kind: "send",
    group: "windows",
    method: "control",
    owner: "P20-06",
  },
  {
    name: "indus:window:painted",
    kind: "send",
    group: "windows",
    method: "painted",
    owner: "P20-06",
  },
  {
    name: "indus:window:theme",
    kind: "send",
    group: "windows",
    method: "setTheme",
    owner: "P20-06",
  },
  {
    name: "indus:window:state",
    kind: "event",
    group: "windows",
    method: "onState",
    owner: "P20-06",
  },
  {
    name: "indus:window:open-session",
    kind: "invoke",
    group: "windows",
    method: "openSession",
    owner: "P20-06",
  },
  {
    name: "indus:window:open-instance",
    kind: "invoke",
    group: "windows",
    method: "openInstance",
    owner: "P20-06",
  },

  // zoom
  { name: "indus:zoom:get", kind: "invoke", group: "zoom", method: "get", owner: "P20-06" },
  { name: "indus:zoom:set", kind: "invoke", group: "zoom", method: "set", owner: "P20-06" },
  { name: "indus:zoom:step", kind: "invoke", group: "zoom", method: "step", owner: "P20-06" },
  {
    name: "indus:zoom:changed",
    kind: "event",
    group: "zoom",
    method: "onChanged",
    owner: "P20-06",
  },

  // external links
  {
    name: "indus:external:open",
    kind: "invoke",
    group: "external",
    method: "open",
    owner: "P20-02",
  },

  // filesystem
  { name: "indus:fs:read-dir", kind: "invoke", group: "fs", method: "readDir", owner: "P20-29" },
  { name: "indus:fs:read-text", kind: "invoke", group: "fs", method: "readText", owner: "P20-29" },
  {
    name: "indus:fs:read-data-url",
    kind: "invoke",
    group: "fs",
    method: "readDataUrl",
    owner: "P20-29",
  },
  { name: "indus:fs:rename", kind: "invoke", group: "fs", method: "rename", owner: "P20-29" },
  { name: "indus:fs:trash", kind: "invoke", group: "fs", method: "trash", owner: "P20-29" },
  { name: "indus:fs:reveal", kind: "invoke", group: "fs", method: "reveal", owner: "P20-29" },
  {
    name: "indus:fs:select-paths",
    kind: "invoke",
    group: "fs",
    method: "selectPaths",
    owner: "P20-29",
  },
  { name: "indus:fs:watch", kind: "invoke", group: "fs", method: "watch", owner: "P20-29" },
  { name: "indus:fs:unwatch", kind: "invoke", group: "fs", method: "unwatch", owner: "P20-29" },
  { name: "indus:fs:changed", kind: "event", group: "fs", method: "onChanged", owner: "P20-29" },

  // git
  { name: "indus:git:status", kind: "invoke", group: "git", method: "status", owner: "P20-30" },
  { name: "indus:git:diff", kind: "invoke", group: "git", method: "diff", owner: "P20-30" },
  { name: "indus:git:stage", kind: "invoke", group: "git", method: "stage", owner: "P20-30" },
  { name: "indus:git:unstage", kind: "invoke", group: "git", method: "unstage", owner: "P20-30" },
  { name: "indus:git:revert", kind: "invoke", group: "git", method: "revert", owner: "P20-30" },
  { name: "indus:git:commit", kind: "invoke", group: "git", method: "commit", owner: "P20-30" },
  {
    name: "indus:git:create-pr",
    kind: "invoke",
    group: "git",
    method: "createPr",
    owner: "P20-30",
  },

  // terminal
  {
    name: "indus:terminal:start",
    kind: "invoke",
    group: "terminal",
    method: "start",
    owner: "P20-31",
  },
  {
    name: "indus:terminal:write",
    kind: "send",
    group: "terminal",
    method: "write",
    owner: "P20-31",
  },
  {
    name: "indus:terminal:resize",
    kind: "send",
    group: "terminal",
    method: "resize",
    owner: "P20-31",
  },
  {
    name: "indus:terminal:dispose",
    kind: "invoke",
    group: "terminal",
    method: "dispose",
    owner: "P20-31",
  },
  {
    name: "indus:terminal:data",
    kind: "event",
    group: "terminal",
    method: "onData",
    owner: "P20-31",
  },
  {
    name: "indus:terminal:exit",
    kind: "event",
    group: "terminal",
    method: "onExit",
    owner: "P20-31",
  },

  // preview
  {
    name: "indus:preview:capture",
    kind: "invoke",
    group: "preview",
    method: "capture",
    owner: "P20-28",
  },
  {
    name: "indus:preview:watch-file",
    kind: "invoke",
    group: "preview",
    method: "watchFile",
    owner: "P20-28",
  },
  {
    name: "indus:preview:unwatch-file",
    kind: "invoke",
    group: "preview",
    method: "unwatchFile",
    owner: "P20-28",
  },
  {
    name: "indus:preview:file-changed",
    kind: "event",
    group: "preview",
    method: "onFileChanged",
    owner: "P20-28",
  },
  {
    name: "indus:preview:close-requested",
    kind: "event",
    group: "preview",
    method: "onCloseRequested",
    owner: "P20-34",
  },

  // updates
  {
    name: "indus:updates:status",
    kind: "invoke",
    group: "updates",
    method: "status",
    owner: "P20-37",
  },
  {
    name: "indus:updates:check",
    kind: "invoke",
    group: "updates",
    method: "check",
    owner: "P20-37",
  },
  {
    name: "indus:updates:apply",
    kind: "invoke",
    group: "updates",
    method: "apply",
    owner: "P20-37",
  },
  {
    name: "indus:updates:set-channel",
    kind: "invoke",
    group: "updates",
    method: "setChannel",
    owner: "P20-37",
  },
  {
    name: "indus:updates:progress",
    kind: "event",
    group: "updates",
    method: "onProgress",
    owner: "P20-37",
  },

  // notifications
  { name: "indus:notify", kind: "invoke", group: "notifications", method: "show", owner: "P20-36" },
  {
    name: "indus:notification:action",
    kind: "event",
    group: "notifications",
    method: "onAction",
    owner: "P20-36",
  },

  // deep links
  { name: "indus:deep-link", kind: "event", group: "deepLinks", method: "onLink", owner: "P20-34" },
  {
    name: "indus:deep-link:ready",
    kind: "send",
    group: "deepLinks",
    method: "ready",
    owner: "P20-34",
  },

  // logs
  { name: "indus:logs:recent", kind: "invoke", group: "logs", method: "recent", owner: "P20-39" },
  { name: "indus:logs:reveal", kind: "invoke", group: "logs", method: "reveal", owner: "P20-39" },
  {
    name: "indus:logs:renderer-error",
    kind: "send",
    group: "logs",
    method: "rendererError",
    owner: "P20-39",
  },

  // secrets policy
  {
    name: "indus:secrets:policy:get",
    kind: "invoke",
    group: "secrets",
    method: "getPolicy",
    owner: "P20-07",
  },
  {
    name: "indus:secrets:policy:set",
    kind: "invoke",
    group: "secrets",
    method: "setPolicy",
    owner: "P20-07",
  },

  // prepared-submission journal
  { name: "indus:journal:list", kind: "invoke", group: "journal", method: "list", owner: "P20-20" },
  {
    name: "indus:journal:prepare",
    kind: "invoke",
    group: "journal",
    method: "prepare",
    owner: "P20-20",
  },
  {
    name: "indus:journal:retire",
    kind: "invoke",
    group: "journal",
    method: "retire",
    owner: "P20-20",
  },

  // app-owned work (quit guard)
  { name: "indus:work:active", kind: "send", group: "work", method: "report", owner: "P20-13" },
] as const satisfies readonly ChannelDef[];

export type Channel = (typeof CHANNELS)[number];
export type ChannelName = Channel["name"];
export type ChannelOfKind<K extends ChannelKind> = Extract<Channel, { kind: K }>;
export type InvokeChannel = ChannelOfKind<"invoke">["name"];
export type SendChannel = ChannelOfKind<"send">["name"];
export type SyncChannel = ChannelOfKind<"sync">["name"];
export type EventChannel = ChannelOfKind<"event">["name"];

const BY_NAME: ReadonlyMap<string, ChannelDef> = new Map(CHANNELS.map((c) => [c.name, c]));

export function channelDef(name: string): ChannelDef | undefined {
  return BY_NAME.get(name);
}

export function isChannelName(name: string): name is ChannelName {
  return BY_NAME.has(name);
}

/** The prefix every channel carries; the registry refuses any other name. */
export const CHANNEL_PREFIX = "indus:";
