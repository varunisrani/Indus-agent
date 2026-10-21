// Dashboard plugins in the SPA (P24-37): the registry plugin bundles call, the loader that puts
// their stylesheets and scripts on the page, and the merge of plugin tabs into the route table.
//
// Order of events: `exposePluginSdk()` runs before React renders (main.tsx), so a bundle can find
// `window.__INDUS_PLUGINS__` and `window.__INDUS_DASHBOARD_SDK__` whenever it runs. The shell asks
// `GET /api/dashboard/plugins`, injects each plugin's CSS and then its script (with `integrity`
// and `crossorigin` when the manifest carries an SRI value, so a changed file never runs), and
// finishes its first render once every plugin registered or `FIRST_RENDER_WAIT_MS` after the
// scripts were added, whichever is first. A plugin that registers later still shows up: the
// registry notifies its subscribers. A script that fails or never registers only warns.

import type { ComponentType } from "react";
import { request } from "../lib/api.ts";
import { basePath } from "../lib/bootstrap.ts";
import type { PageRoute } from "../shell/routes.ts";

export const PLUGINS_LIST_PATH = "/api/dashboard/plugins";
export const ASSET_PREFIX = "/dashboard-plugins";
export const FIRST_RENDER_WAIT_MS = 2_000;
/** The page that manages plugins; no plugin may take it. */
export const PROTECTED_PATHS: ReadonlySet<string> = new Set(["/plugins"]);

export interface PluginTab {
  path: string;
  position?: string;
  override?: string;
  hidden?: boolean;
}

export interface PluginManifest {
  name: string;
  label: string;
  description?: string;
  icon?: string;
  version?: string;
  tab: PluginTab;
  slots?: string[];
  entry: string;
  css?: string | null;
  has_api?: boolean;
  source?: string;
  integrity?: string;
}

export type PluginComponent = ComponentType;
export type LoadError = "LOAD_FAILED" | "NO_REGISTER";

export interface SlotEntry {
  plugin: string;
  component: PluginComponent;
}

type Listener = () => void;

/** What plugin bundles reach on `window.__INDUS_PLUGINS__`. */
export interface PluginHostApi {
  register(name: string, component: PluginComponent): void;
  registerSlot(name: string, slot: string, component: PluginComponent): void;
}

export class PluginRegistry {
  private readonly components = new Map<string, PluginComponent>();
  private readonly errors = new Map<string, LoadError>();
  private readonly slots = new Map<string, SlotEntry[]>();
  private readonly listeners = new Set<Listener>();
  private version = 0;

  register(name: string, component: PluginComponent): void {
    if (typeof name !== "string" || typeof component !== "function") return;
    this.errors.delete(name);
    this.components.set(name, component);
    this.changed();
  }

  registerSlot(name: string, slot: string, component: PluginComponent): void {
    if (typeof name !== "string" || typeof slot !== "string" || typeof component !== "function")
      return;
    const kept = (this.slots.get(slot) ?? []).filter((entry) => entry.plugin !== name);
    kept.push({ plugin: name, component });
    this.slots.set(slot, kept);
    this.changed();
  }

  component(name: string): PluginComponent | undefined {
    return this.components.get(name);
  }

  loadError(name: string): LoadError | undefined {
    return this.errors.get(name);
  }

  setLoadError(name: string, error: LoadError): void {
    if (this.components.has(name)) return;
    this.errors.set(name, error);
    this.changed();
  }

  slotEntries(slot: string): SlotEntry[] {
    return [...(this.slots.get(slot) ?? [])];
  }

  /** A number that changes on every change (for `useSyncExternalStore`). */
  snapshot = (): number => this.version;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  clear(): void {
    this.components.clear();
    this.errors.clear();
    this.slots.clear();
    this.changed();
  }

  hostApi(): PluginHostApi {
    return {
      register: (name, component) => this.register(name, component),
      registerSlot: (name, slot, component) => this.registerSlot(name, slot, component),
    };
  }

  private changed(): void {
    this.version += 1;
    for (const listener of [...this.listeners]) {
      try {
        listener();
      } catch {
        // one subscriber's bug does not stop the others
      }
    }
  }
}

export const pluginRegistry = new PluginRegistry();

declare global {
  interface Window {
    __INDUS_PLUGINS__?: PluginHostApi;
    __INDUS_DASHBOARD_SDK__?: unknown;
  }
}

/** Put the registry and the SDK on `window` (call before React renders). */
export function exposePluginSdk(sdk: unknown, registry: PluginRegistry = pluginRegistry): void {
  window.__INDUS_PLUGINS__ = registry.hostApi();
  window.__INDUS_DASHBOARD_SDK__ = sdk;
}

/** The URL of one of a plugin's files. */
export function pluginAssetUrl(name: string, file: string): string {
  const parts = file.split("/").map(encodeURIComponent).join("/");
  return `${basePath()}${ASSET_PREFIX}/${encodeURIComponent(name)}/${parts}`;
}

export async function fetchManifests(): Promise<PluginManifest[]> {
  const body = await request<{ plugins?: PluginManifest[] }>(PLUGINS_LIST_PATH);
  return Array.isArray(body.plugins) ? body.plugins : [];
}

export interface LoadOptions {
  registry?: PluginRegistry;
  doc?: Document;
  waitMs?: number;
  warn?: (message: string) => void;
}

function defaultWarn(message: string): void {
  // biome-ignore lint/suspicious/noConsole: a plugin load failure only warns (P24-37)
  console.warn(message);
}

/**
 * Inject every plugin's CSS and script. Resolves when all registered, or `waitMs` after the
 * scripts were added; registrations after that still reach the registry.
 */
export function loadPlugins(manifests: PluginManifest[], options: LoadOptions = {}): Promise<void> {
  const registry = options.registry ?? pluginRegistry;
  const doc = options.doc ?? document;
  const warn = options.warn ?? defaultWarn;
  const waitMs = options.waitMs ?? FIRST_RENDER_WAIT_MS;
  const pending = new Set(manifests.map((m) => m.name));
  if (pending.size === 0) return Promise.resolve();
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      unsubscribe();
      clearTimeout(timer);
      resolve();
    };
    const check = () => {
      for (const name of [...pending]) {
        if (registry.component(name) !== undefined) pending.delete(name);
      }
      if (pending.size === 0) finish();
    };
    const unsubscribe = registry.subscribe(check);
    const timer = setTimeout(finish, waitMs);
    for (const manifest of manifests) {
      if (manifest.css) {
        const href = pluginAssetUrl(manifest.name, manifest.css);
        if (doc.querySelector(`link[data-indus-plugin="${manifest.name}"]`) === null) {
          const link = doc.createElement("link");
          link.rel = "stylesheet";
          link.href = href;
          link.setAttribute("data-indus-plugin", manifest.name);
          doc.head.appendChild(link);
        }
      }
      if (doc.querySelector(`script[data-indus-plugin="${manifest.name}"]`) !== null) continue;
      const script = doc.createElement("script");
      script.src = pluginAssetUrl(manifest.name, manifest.entry);
      script.async = true;
      script.setAttribute("data-indus-plugin", manifest.name);
      if (manifest.integrity) {
        script.integrity = manifest.integrity;
        script.crossOrigin = "anonymous";
      }
      script.onerror = () => {
        registry.setLoadError(manifest.name, "LOAD_FAILED");
        warn(`[plugins] ${manifest.name}: its script did not load (or failed its integrity check)`);
        pending.delete(manifest.name);
        if (pending.size === 0) finish();
      };
      script.onload = () => {
        queueMicrotask(() => {
          if (registry.component(manifest.name) === undefined && !manifest.tab.hidden) {
            registry.setLoadError(manifest.name, "NO_REGISTER");
            warn(`[plugins] ${manifest.name}: its script ran but registered nothing yet`);
          }
        });
      };
      doc.body.appendChild(script);
    }
    check();
  });
}

function segment(path: string): string {
  return path.replace(/^\/+/, "").split("/")[0] ?? "";
}

/**
 * The route table with plugin pages merged in: an `override` replaces that built-in page's
 * component (never `/plugins`); other tabs are inserted after/before the named segment, or at
 * the end; hidden plugins add no route. Plugins without a registered component add nothing yet.
 */
export function mergePluginRoutes(
  builtin: readonly PageRoute[],
  manifests: readonly PluginManifest[],
  component: (name: string) => PluginComponent | undefined,
): PageRoute[] {
  const routes = [...builtin];
  for (const manifest of manifests) {
    const page = component(manifest.name);
    if (page === undefined || manifest.tab.hidden) continue;
    const override = manifest.tab.override;
    if (override !== undefined) {
      if (PROTECTED_PATHS.has(override)) continue;
      const at = routes.findIndex((route) => route.path === override);
      const found = routes[at];
      if (found === undefined) continue;
      routes[at] = { ...found, page };
      continue;
    }
    const path = manifest.tab.path;
    if (PROTECTED_PATHS.has(path) || routes.some((route) => route.path === path)) continue;
    const route: PageRoute = { path, title: manifest.label || manifest.name, nav: "main", page };
    const position = manifest.tab.position ?? "end";
    const [kind, target] = position.split(":");
    const at = routes.findIndex((r) => segment(r.path) === target);
    if ((kind === "after" || kind === "before") && at >= 0) {
      routes.splice(kind === "after" ? at + 1 : at, 0, route);
    } else {
      routes.push(route);
    }
  }
  return routes;
}
