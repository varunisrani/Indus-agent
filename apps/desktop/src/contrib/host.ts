// The app's `PluginHost` and start-up of desktop plugins (P24-32). The host is built here, from the
// bridge and the foreground connection, before any plugin runs; plugins only ever get the context
// `sdk.ts` makes from it — never the bridge itself.

import type { MethodName } from "@indus/protocol";
import type { DesktopPluginRow, DesktopPluginSource } from "../app/capabilities/tabs.tsx";
import type { GatewayConnection } from "../gateway/connectionStore.ts";
import type { IndusDesktopBridge } from "../global.d.ts";
import { installPluginHostGlobals, PluginLoader, type PluginRecord } from "./loader.ts";
import type { PluginHost, PluginRestInit } from "./sdk.ts";

export const PLUGIN_TOAST_EVENT = "indus-plugin-toast";

export interface HostDeps {
  bridge: IndusDesktopBridge;
  connection: () => GatewayConnection;
  profile: () => string;
}

export function createPluginHost(deps: HostDeps): PluginHost {
  const { bridge } = deps;
  return {
    state: {
      connection: () => {
        const connection = deps.connection();
        return {
          phase: connection.getState().phase,
          mode: connection.mode,
          codeVersion: connection.codeVersion,
        };
      },
      profile: () => deps.profile(),
      route: () => window.location.hash.replace(/^#/, "") || "/",
      theme: () => (document.documentElement.dataset.theme === "light" ? "light" : "dark"),
      subscribe: (listener) => {
        window.addEventListener("hashchange", listener);
        return () => window.removeEventListener("hashchange", listener);
      },
    },
    toast: (text, tone) =>
      window.dispatchEvent(new CustomEvent(PLUGIN_TOAST_EVENT, { detail: { text, tone } })),
    navigate: (path) => {
      window.location.hash = path;
    },
    log: (level, text) => {
      if (level === "info") return;
      bridge.logs.rendererError({ message: text, source: "renderer:plugin" });
    },
    restartGateway: async () => {
      deps.connection().reconnect();
      return true;
    },
    onEvent: (type, listener) =>
      deps.connection().onEvent((event) => {
        const kind = (event as { type?: unknown }).type;
        if (type === "*" || kind === type) listener(event);
      }),
    rpc: (method, params) =>
      deps.connection().request(method as MethodName, params as never) as Promise<unknown>,
    rest: async (path, init: PluginRestInit) => {
      const answer = await bridge.api.request({
        method: init.method ?? "GET",
        path,
        ...(init.body === undefined ? {} : { body: init.body }),
        profile: deps.profile(),
      });
      if (answer.kind === "not-found") throw new Error(`${path}: not found`);
      if (answer.status >= 400) throw new Error(`${path}: answered ${answer.status}`);
      return answer.body;
    },
    // Plugin sockets need a ticketed route to `/api/ext/<id>/` from the desktop; until main grows
    // one, a socket is a no-op (polling `rest` works everywhere).
    socket: () => () => undefined,
    storage: () => {
      try {
        return window.localStorage;
      } catch {
        return null;
      }
    },
  };
}

/** Whether the agent plugin `name` is enabled for this profile (`GET /api/plugins`). */
async function agentPluginEnabled(bridge: IndusDesktopBridge, name: string): Promise<boolean> {
  try {
    const answer = await bridge.api.request({ method: "GET", path: "/api/plugins" });
    if (answer.kind !== "response" || answer.status !== 200) return false;
    const list = (answer.body as { plugins?: Array<{ name?: unknown; enabled?: unknown }> })
      .plugins;
    return Array.isArray(list) && list.some((p) => p.name === name && p.enabled === true);
  } catch {
    return false;
  }
}

let running: PluginLoader | null = null;
const startListeners = new Set<() => void>();

/** The window's plugin loader once plugins started (Settings › Advanced › Plugins reads it). */
export function runningPluginLoader(): PluginLoader | null {
  return running;
}

/** Called when the window's loader starts (the inventory page may open first). */
export function onPluginLoaderStarted(listener: () => void): () => void {
  startListeners.add(listener);
  return () => {
    startListeners.delete(listener);
  };
}

/** The Capabilities page's rows for the loader's inventory: a plugin is on when it loaded. */
export function pluginRows(records: readonly PluginRecord[]): DesktopPluginRow[] {
  return records.map((record) => ({
    id: record.id,
    name: record.name ?? record.id,
    enabled: record.status === "loaded",
  }));
}

/**
 * The Capabilities page's Desktop column (P24-26) over a loader: its inventory, read when asked
 * (empty until the plugins started), and the on/off switch — the same stored choice Settings ›
 * Plugins applies (`PluginLoader.setEnabled`, renderer-local: no IPC). Never rejects: with no
 * loader yet the page's re-read simply shows the row unchanged.
 */
export function pluginSourceFor(
  loader: () => Pick<PluginLoader, "inventory" | "setEnabled"> | null,
): DesktopPluginSource {
  return {
    list: async (): Promise<DesktopPluginRow[]> => pluginRows(loader()?.inventory() ?? []),
    setEnabled: async (id, on) => {
      await loader()?.setEnabled(id, on);
    },
  };
}

/** The window's Desktop column source (the running loader). */
export const desktopPluginSource: DesktopPluginSource = pluginSourceFor(() => running);

/** Load the checked plugins main lists, and follow its changes (hot reload). */
export async function startDesktopPlugins(deps: HostDeps): Promise<PluginLoader> {
  installPluginHostGlobals();
  const loader = new PluginLoader({
    host: createPluginHost(deps),
    importModule: (url) => import(/* @vite-ignore */ url),
    packageEnabled: (name) => agentPluginEnabled(deps.bridge, name),
    storage: () => {
      try {
        return window.localStorage;
      } catch {
        return null;
      }
    },
    log: (text) => deps.bridge.logs.rendererError({ message: text, source: "renderer:plugins" }),
  });
  running = loader;
  for (const listener of [...startListeners]) listener();
  deps.bridge.plugins.onChanged(({ files }) => void loader.sync(files));
  try {
    const { files } = await deps.bridge.plugins.list();
    await loader.sync(files);
  } catch {
    // main has no plugin step (an older build): no plugins
  }
  return loader;
}
