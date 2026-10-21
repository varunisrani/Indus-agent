// The desktop plugin SDK (P24-32): what `import … from "@indus/desktop-sdk"` gives a plugin, and
// the `PluginContext` its `register(ctx)` receives.
//
// A plugin never sees the preload bridge. The app builds one `PluginHost` from the bridge and the
// connection before any plugin runs; every door below is a wrapper over that host:
//
//   1. read-only views    `ctx.state.connection() / profile() / route() / theme()` + `subscribe`
//   2. curated actions    `ctx.toast`, `ctx.navigate` (app paths only), `ctx.log`,
//                         `ctx.restartGateway`, `ctx.onEvent`
//   3. JSON-RPC door      `ctx.rpc(method, params)` for the read methods in `PLUGIN_RPC_METHODS`
//   4. own namespace      `ctx.rest(path, init)` and `ctx.socket(path, onMessage)` under
//                         `/api/ext/<plugin id>/` (the socket goes through the ticketed helper)
//   5. UI kit             `ui` from the SDK module
//
// Everything a context takes out (contributions, event listeners, timers, DOM listeners, sockets,
// `onDispose` callbacks) is tracked and undone when the plugin unloads. Contributions added while
// `register` runs are staged and committed only if it returns (`registry.transaction`); ones added
// later (from a timer, an event) register at once.

import {
  AREAS,
  type Area,
  type Contribution,
  type ContributionRegistry,
  type Staging,
} from "./registry.ts";

export const SDK_VERSION = "1.0.0";

/** The gateway methods a plugin may call: reads only; changes go through curated actions. */
export const PLUGIN_RPC_METHODS: ReadonlySet<string> = new Set([
  "session.list",
  "session.info",
  "session.history",
  "session.usage",
  "session.most_recent",
  "model.options",
  "commands.catalog",
  "runtime.describe",
  "gateway.ping",
]);

export interface ConnectionView {
  readonly phase: string;
  readonly mode: "local" | "remote" | null;
  readonly codeVersion: string | null;
}

/** What the app hands the plugin system (built from the bridge; never given to a plugin). */
export interface PluginHost {
  state: {
    connection(): ConnectionView;
    profile(): string;
    route(): string;
    theme(): "light" | "dark";
    subscribe(listener: () => void): () => void;
  };
  toast(text: string, tone: "info" | "success" | "error"): void;
  navigate(path: string): void;
  log(level: "info" | "warn" | "error", text: string): void;
  restartGateway(): Promise<boolean>;
  onEvent(type: string, listener: (event: unknown) => void): () => void;
  rpc(method: string, params: unknown): Promise<unknown>;
  rest(path: string, init: PluginRestInit): Promise<unknown>;
  socket(path: string, onMessage: (data: unknown) => void): () => void;
  storage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
}

export interface PluginRestInit {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
}

/** A contribution as a plugin writes it: the host sets `source` and namespaces `id`. */
export type PluginContribution = Omit<Contribution, "source" | "id"> & {
  id: string;
  area: Area;
};

export interface PluginStorage {
  get<T>(key: string, fallback: T): T;
  set(key: string, value: unknown): void;
  remove(key: string): void;
}

export interface PluginContext {
  readonly id: string;
  readonly source: string;
  register(c: PluginContribution): () => void;
  registerMany(list: readonly PluginContribution[]): () => void;
  onDispose(fn: () => void): void;
  state: PluginHost["state"];
  toast(text: string, tone?: "info" | "success" | "error"): void;
  navigate(path: string): void;
  log(level: "info" | "warn" | "error", text: string): void;
  restartGateway(): Promise<boolean>;
  onEvent(type: string, listener: (event: unknown) => void): () => void;
  rpc(method: string, params?: unknown): Promise<unknown>;
  rest(path: string, init?: PluginRestInit): Promise<unknown>;
  socket(path: string, onMessage: (data: unknown) => void): () => void;
  setTimeout(fn: () => void, ms: number): () => void;
  setInterval(fn: () => void, ms: number): () => void;
  addEventListener(
    target: EventTarget,
    type: string,
    listener: EventListenerOrEventListenerObject,
    options?: boolean | AddEventListenerOptions,
  ): () => void;
  storage: PluginStorage;
}

export interface DesktopPlugin {
  id: string;
  name?: string;
  description?: string;
  /** False: listed but off until the person turns it on. */
  defaultEnabled?: boolean;
  register(ctx: PluginContext): void;
}

const PLUGIN_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export class PluginShapeError extends Error {}

/** Check and return a plugin definition (a plugin's `export default definePlugin({...})`). */
export function definePlugin(plugin: DesktopPlugin): DesktopPlugin {
  if (typeof plugin !== "object" || plugin === null) {
    throw new PluginShapeError("a plugin is an object");
  }
  if (typeof plugin.id !== "string" || !PLUGIN_ID.test(plugin.id)) {
    throw new PluginShapeError(`plugin id ${JSON.stringify(plugin.id)} is not a plugin id`);
  }
  if (typeof plugin.register !== "function") {
    throw new PluginShapeError(`${plugin.id}: register(ctx) is missing`);
  }
  return plugin;
}

/** The UI kit: plain-DOM-free helpers plugins use with React's `createElement`. */
export const ui = Object.freeze({
  /** Class names of our primitives (styles.css), so plugin markup matches the app. */
  classes: Object.freeze({
    button: "button",
    buttonPrimary: "button button-primary",
    buttonSecondary: "button button-secondary",
    row: "settings-row",
    muted: "muted",
  }),
});

export const version = SDK_VERSION;
export { AREAS };

/** Exactly the names `electron/plugins/sdkNames.ts` lists for the `sdk.js` shim. */
export const SDK_MODULE = Object.freeze({ AREAS, definePlugin, ui, version: SDK_VERSION });

/** Lifetime bookkeeping of one loaded plugin. */
export class PluginLifetime {
  private readonly cleanups = new Set<() => void>();
  private ended = false;

  get done(): boolean {
    return this.ended;
  }

  track(cleanup: () => void): () => void {
    if (this.ended) {
      cleanup();
      return () => undefined;
    }
    this.cleanups.add(cleanup);
    return () => {
      if (this.cleanups.delete(cleanup)) cleanup();
    };
  }

  /** Run every cleanup, each on its own (one that throws does not stop the rest). */
  end(onError: (error: unknown) => void): void {
    this.ended = true;
    const pending = [...this.cleanups];
    this.cleanups.clear();
    for (const cleanup of pending) {
      try {
        cleanup();
      } catch (error) {
        onError(error);
      }
    }
  }
}

function appPath(path: string): boolean {
  return /^\/(?!\/)[A-Za-z0-9/_\-?=&.%]*$/.test(path);
}

function namespaced(pluginId: string, path: string): string {
  if (!path.startsWith("/") || path.includes("..") || path.includes("://")) {
    throw new PluginShapeError("a plugin path is relative to its own namespace, like /items");
  }
  return `/api/ext/${pluginId}${path}`;
}

/**
 * The context for one load of `pluginId`. `stage` is the open transaction while `register` runs
 * (null afterwards: later contributions register at once).
 */
export function createPluginContext(options: {
  pluginId: string;
  host: PluginHost;
  registry: ContributionRegistry;
  lifetime: PluginLifetime;
  stage: () => Staging | null;
}): PluginContext {
  const { pluginId, host, registry, lifetime } = options;
  const source = `plugin:${pluginId}`;
  const scope = (c: PluginContribution): Contribution => ({
    ...c,
    id: `${pluginId}:${c.id}`,
    source,
  });
  const addOne = (c: PluginContribution): (() => void) => {
    const staging = options.stage();
    const dispose = staging !== null ? staging.add(scope(c)) : registry.register(scope(c));
    return lifetime.track(dispose);
  };
  const prefix = `indus.plugin.${pluginId}.`;
  const storage: PluginStorage = {
    get<T>(key: string, fallback: T): T {
      try {
        const raw = host.storage()?.getItem(prefix + key) ?? null;
        return raw === null ? fallback : (JSON.parse(raw) as T);
      } catch {
        return fallback;
      }
    },
    set(key, value) {
      try {
        host.storage()?.setItem(prefix + key, JSON.stringify(value));
      } catch {
        // storage full or unavailable: the value is not kept
      }
    },
    remove(key) {
      try {
        host.storage()?.removeItem(prefix + key);
      } catch {
        // nothing to remove
      }
    },
  };
  return {
    id: pluginId,
    source,
    register: addOne,
    registerMany: (list) => {
      const disposers = list.map(addOne);
      return () => {
        for (const dispose of disposers) dispose();
      };
    },
    onDispose: (fn) => {
      lifetime.track(fn);
    },
    state: host.state,
    toast: (text, tone = "info") => host.toast(String(text).slice(0, 500), tone),
    navigate: (path) => {
      if (!appPath(path)) throw new PluginShapeError("navigate takes an app path like /settings");
      host.navigate(path);
    },
    log: (level, text) => host.log(level, `[plugin ${pluginId}] ${String(text).slice(0, 2000)}`),
    restartGateway: () => host.restartGateway(),
    onEvent: (type, listener) => lifetime.track(host.onEvent(type, listener)),
    rpc: async (method, params = {}) => {
      if (!PLUGIN_RPC_METHODS.has(method)) {
        throw new PluginShapeError(`${method} is not open to plugins`);
      }
      return host.rpc(method, params);
    },
    rest: (path, init = {}) => host.rest(namespaced(pluginId, path), init),
    socket: (path, onMessage) => lifetime.track(host.socket(namespaced(pluginId, path), onMessage)),
    setTimeout: (fn, ms) => {
      let done: () => void = () => undefined;
      const handle = globalThis.setTimeout(() => {
        done();
        fn();
      }, ms);
      done = lifetime.track(() => globalThis.clearTimeout(handle));
      return done;
    },
    setInterval: (fn, ms) => {
      const handle = globalThis.setInterval(fn, ms);
      return lifetime.track(() => globalThis.clearInterval(handle));
    },
    addEventListener: (target, type, listener, opts) => {
      target.addEventListener(type, listener, opts);
      return lifetime.track(() => target.removeEventListener(type, listener, opts));
    },
    storage,
  };
}
