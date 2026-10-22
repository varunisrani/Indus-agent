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

  // connections registry (P24-29)
  {
    name: "indus:connections:list",
    kind: "invoke",
    group: "connections",
    method: "list",
    owner: "P24-29",
  },
  {
    name: "indus:connections:test",
    kind: "invoke",
    group: "connections",
    method: "test",
    owner: "P24-29",
  },
  {
    name: "indus:connections:save",
    kind: "invoke",
    group: "connections",
    method: "save",
    owner: "P24-29",
  },
  {
    name: "indus:connections:remove",
    kind: "invoke",
    group: "connections",
    method: "remove",
    owner: "P24-29",
  },
  {
    name: "indus:connections:set-primary",
    kind: "invoke",
    group: "connections",
    method: "setPrimary",
    owner: "P24-29",
  },
  {
    name: "indus:connections:set-launch-mode",
    kind: "invoke",
    group: "connections",
    method: "setLaunchMode",
    owner: "P24-29",
  },
  {
    name: "indus:connections:sign-in",
    kind: "invoke",
    group: "connections",
    method: "signIn",
    owner: "P24-29",
  },
  {
    name: "indus:connections:sign-out",
    kind: "invoke",
    group: "connections",
    method: "signOut",
    owner: "P24-29",
  },

  // artifacts (P24-28)
  {
    name: "indus:artifacts:download",
    kind: "invoke",
    group: "artifacts",
    method: "download",
    owner: "P24-28",
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
    name: "indus:profile:prepare-change",
    kind: "invoke",
    group: "profile",
    method: "prepareChange",
    owner: "P20-26",
  },
  {
    name: "indus:profile:commit-change",
    kind: "invoke",
    group: "profile",
    method: "commitChange",
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
  { name: "indus:fs:set-roots", kind: "invoke", group: "fs", method: "setRoots", owner: "P20-29" },
  { name: "indus:fs:stat", kind: "invoke", group: "fs", method: "stat", owner: "P20-29" },
  {
    name: "indus:fs:limits:get",
    kind: "invoke",
    group: "fs",
    method: "getLimits",
    owner: "P20-29",
  },
  {
    name: "indus:fs:limits:set",
    kind: "invoke",
    group: "fs",
    method: "setLimits",
    owner: "P20-29",
  },

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
  { name: "indus:git:changes", kind: "invoke", group: "git", method: "changes", owner: "P20-30" },
  {
    name: "indus:git:commit-context",
    kind: "invoke",
    group: "git",
    method: "commitContext",
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
  {
    name: "indus:updates:skew",
    kind: "event",
    group: "updates",
    method: "onSkew",
    owner: "P20-37",
  },

  // HUD, quick entry, global shortcuts, last-active session (P20-35)
  { name: "indus:hud:open", kind: "invoke", group: "hud", method: "open", owner: "P20-35" },
  { name: "indus:hud:close", kind: "invoke", group: "hud", method: "close", owner: "P20-35" },
  {
    name: "indus:hud:reset-layout",
    kind: "invoke",
    group: "hud",
    method: "resetLayout",
    owner: "P20-35",
  },
  { name: "indus:hud:drag", kind: "send", group: "hud", method: "drag", owner: "P20-35" },
  { name: "indus:hud:changed", kind: "event", group: "hud", method: "onChanged", owner: "P20-35" },
  { name: "indus:quick:submit", kind: "invoke", group: "quick", method: "submit", owner: "P20-35" },
  { name: "indus:quick:dismiss", kind: "send", group: "quick", method: "dismiss", owner: "P20-35" },
  { name: "indus:quick:shown", kind: "event", group: "quick", method: "onShown", owner: "P20-35" },
  {
    name: "indus:quick:deliver",
    kind: "event",
    group: "quick",
    method: "onDeliver",
    owner: "P20-35",
  },
  { name: "indus:quick:ack", kind: "send", group: "quick", method: "ack", owner: "P20-35" },
  {
    name: "indus:shortcuts:get",
    kind: "invoke",
    group: "shortcuts",
    method: "get",
    owner: "P20-35",
  },
  {
    name: "indus:shortcuts:set",
    kind: "invoke",
    group: "shortcuts",
    method: "set",
    owner: "P20-35",
  },
  {
    name: "indus:session:active",
    kind: "send",
    group: "windows",
    method: "reportSession",
    owner: "P20-35",
  },

  // tray, keep awake, notification settings, link previews (P20-36)
  { name: "indus:ambient:get", kind: "invoke", group: "ambient", method: "get", owner: "P20-36" },
  { name: "indus:ambient:set", kind: "invoke", group: "ambient", method: "set", owner: "P20-36" },
  {
    name: "indus:ambient:changed",
    kind: "event",
    group: "ambient",
    method: "onChanged",
    owner: "P20-36",
  },
  {
    name: "indus:keep-awake:offer",
    kind: "event",
    group: "ambient",
    method: "onKeepAwakeOffer",
    owner: "P20-36",
  },
  {
    name: "indus:links:preview",
    kind: "invoke",
    group: "links",
    method: "preview",
    owner: "P20-36",
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

  // about: the packaged third-party notices for the licences screen (W46, F-2)
  { name: "indus:about:notices", kind: "invoke", group: "about", method: "notices", owner: "W46" },

  // diagnostics, clipboard, find in page
  {
    name: "indus:diagnostics:copy",
    kind: "invoke",
    group: "diagnostics",
    method: "copy",
    owner: "P20-39",
  },
  {
    name: "indus:clipboard:read-text",
    kind: "invoke",
    group: "clipboard",
    method: "readText",
    owner: "P20-39",
  },
  {
    name: "indus:clipboard:write-text",
    kind: "invoke",
    group: "clipboard",
    method: "writeText",
    owner: "P20-39",
  },
  { name: "indus:find:open", kind: "event", group: "find", method: "onOpen", owner: "P20-39" },

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

  // desktop preferences (settings held by main)
  { name: "indus:prefs:get", kind: "invoke", group: "prefs", method: "get", owner: "P20-32" },
  { name: "indus:prefs:set", kind: "invoke", group: "prefs", method: "set", owner: "P20-32" },
  {
    name: "indus:prefs:changed",
    kind: "event",
    group: "prefs",
    method: "onChanged",
    owner: "P20-32",
  },

  // app-owned work (quit guard)
  { name: "indus:work:active", kind: "send", group: "work", method: "report", owner: "P20-13" },

  // desktop pet overlay (P24-19): app windows show / hide / speak; the pet window reads its state
  // and reports whether the pointer is over the figure (click-through elsewhere)
  { name: "indus:pet:show", kind: "invoke", group: "pet", method: "show", owner: "P24-19" },
  { name: "indus:pet:hide", kind: "invoke", group: "pet", method: "hide", owner: "P24-19" },
  { name: "indus:pet:say", kind: "send", group: "pet", method: "say", owner: "P24-19" },
  { name: "indus:pet:get", kind: "invoke", group: "pet", method: "get", owner: "P24-19" },
  { name: "indus:pet:state", kind: "event", group: "pet", method: "onState", owner: "P24-19" },
  { name: "indus:pet:pointer", kind: "send", group: "pet", method: "pointer", owner: "P24-19" },

  // wake-word indicator (P24-20)
  {
    name: "indus:wake:indicator",
    kind: "send",
    group: "wake",
    method: "setIndicator",
    owner: "P24-20",
  },
  {
    name: "indus:wake:indicator-get",
    kind: "invoke",
    group: "wake",
    method: "getIndicator",
    owner: "P24-20",
  },
  {
    name: "indus:wake:indicator-state",
    kind: "event",
    group: "wake",
    method: "onIndicator",
    owner: "P24-20",
  },

  // VS Code Marketplace themes (P24-33): search and download through main (allow-listed hosts)
  {
    name: "indus:themes:search",
    kind: "invoke",
    group: "themes",
    method: "searchMarketplace",
    owner: "P24-33",
  },
  {
    name: "indus:themes:download",
    kind: "invoke",
    group: "themes",
    method: "downloadMarketplace",
    owner: "P24-33",
  },

  // desktop plugins on disk (checked in main, served through indus-plugin:)
  { name: "indus:plugins:list", kind: "invoke", group: "plugins", method: "list", owner: "P24-32" },
  {
    name: "indus:plugins:rescan",
    kind: "invoke",
    group: "plugins",
    method: "rescan",
    owner: "P24-32",
  },
  {
    name: "indus:plugins:changed",
    kind: "event",
    group: "plugins",
    method: "onChanged",
    owner: "P24-32",
  },

  // uninstall from Settings → About (runs `indus uninstall` from a helper after the app quits)
  {
    name: "indus:uninstall:summary",
    kind: "invoke",
    group: "uninstall",
    method: "summary",
    owner: "P24-34",
  },
  {
    name: "indus:uninstall:run",
    kind: "invoke",
    group: "uninstall",
    method: "run",
    owner: "P24-34",
  },
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
