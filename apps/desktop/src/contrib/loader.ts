// The renderer half of desktop plugins (P24-32): load what main checked, keep an inventory.
//
// Main lists the plugin files (`plugins.list`, `plugins.onChanged`) with a status and, for an
// accepted file, the hash under which `indus-plugin://disk/<hash>.js` serves its checked text.
// `PluginLoader.sync(files)` makes the loaded set match: a plugin whose file went away or changed is
// unloaded (every disposer it took out runs, each on its own, after it is dropped from the loaded
// map so a broken disposer cannot wedge the next load), then new and changed ones are loaded:
//
//   import the module (deadline `LOAD_TIMEOUT_MS`) → `definePlugin` shape check (the id must match
//   the file's id) → is it on? (the person's choice, else `defaultEnabled`; a package half also
//   needs its agent plugin enabled) → `register(ctx)` inside a registry transaction.
//
// Any failure marks the plugin `error` with a message and leaves no contribution behind; the app
// never goes down for a plugin. `installPluginHostGlobals` must run first: the host shim modules
// read React and the SDK from `globalThis[Symbol.for(HOST_KEY)]`.

import * as React from "react";
import * as JsxRuntime from "react/jsx-runtime";
import type { IndusPluginFile } from "../global.d.ts";
import { type ContributionRegistry, contributions } from "./registry.ts";
import {
  createPluginContext,
  type DesktopPlugin,
  definePlugin,
  type PluginHost,
  PluginLifetime,
  SDK_MODULE,
} from "./sdk.ts";

export const LOAD_TIMEOUT_MS = 10_000;
export const HOST_KEY = "indus:desktop-plugin-host";
export const PLUGIN_SCHEME = "indus-plugin";
export const DECISIONS_KEY = "indus.desktop.plugins.decisions";

export type PluginStatus = "loaded" | "disabled" | "error" | "refused" | "loading";

export interface PluginRecord {
  id: string;
  kind: IndusPluginFile["kind"];
  file: string;
  status: PluginStatus;
  name?: string;
  description?: string;
  error?: string;
  packageName?: string;
}

/** Put React, the JSX runtime and the SDK where the host shims look for them. */
export function installPluginHostGlobals(target: object = globalThis): void {
  Object.defineProperty(target, Symbol.for(HOST_KEY), {
    value: Object.freeze({ react: React, jsx: JsxRuntime, sdk: SDK_MODULE }),
    configurable: true,
    enumerable: false,
    writable: false,
  });
}

export function moduleUrl(file: Pick<IndusPluginFile, "hash">): string {
  return `${PLUGIN_SCHEME}://disk/${file.hash}.js`;
}

interface Loaded {
  file: IndusPluginFile;
  lifetime: PluginLifetime;
}

type Listener = () => void;

export interface LoaderOptions {
  host: PluginHost;
  registry?: ContributionRegistry;
  /** `import(url)`; tests hand in a fake module table. */
  importModule: (url: string) => Promise<unknown>;
  /** Whether a package half's agent plugin is enabled (P13 consent). */
  packageEnabled?: (packageName: string) => Promise<boolean>;
  storage?: () => Pick<Storage, "getItem" | "setItem"> | null;
  timeoutMs?: number;
  log?: (text: string) => void;
}

export class PluginLoader {
  private readonly loaded = new Map<string, Loaded>();
  private readonly records = new Map<string, PluginRecord>();
  private readonly listeners = new Set<Listener>();
  private readonly registry: ContributionRegistry;
  private queue: Promise<void> = Promise.resolve();
  private lastFiles: readonly IndusPluginFile[] = [];
  private readonly options: LoaderOptions;

  constructor(options: LoaderOptions) {
    this.options = options;
    this.registry = options.registry ?? contributions;
  }

  inventory(): PluginRecord[] {
    return [...this.records.values()].map((r) => ({ ...r }));
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** Make the loaded set match `files` (calls are queued, never interleaved). */
  sync(files: readonly IndusPluginFile[]): Promise<void> {
    this.lastFiles = files;
    this.queue = this.queue.then(() => this.runSync(files)).catch(() => undefined);
    return this.queue;
  }

  /** The person's on/off choice for a plugin (stored, else its default). */
  choiceOf(id: string): boolean | undefined {
    return this.decisions()[id];
  }

  /** The person's on/off choice; applied at once (against the files main listed last). */
  setEnabled(
    id: string,
    enabled: boolean,
    files: readonly IndusPluginFile[] = this.lastFiles,
  ): Promise<void> {
    const decisions = this.decisions();
    decisions[id] = enabled;
    try {
      this.options.storage?.()?.setItem(DECISIONS_KEY, JSON.stringify(decisions));
    } catch {
      // not kept across restarts; still applied now
    }
    this.unload(id);
    return this.sync(files);
  }

  unload(id: string): void {
    const current = this.loaded.get(id);
    if (current === undefined) return;
    this.loaded.delete(id);
    current.lifetime.end((error) =>
      this.options.log?.(`[plugins] ${id}: a cleanup failed: ${String(error)}`),
    );
    this.registry.removeSource(`plugin:${id}`);
    const record = this.records.get(id);
    if (record !== undefined) this.records.set(id, { ...record, status: "disabled" });
    this.changed();
  }

  private decisions(): Record<string, boolean> {
    try {
      const raw = this.options.storage?.()?.getItem(DECISIONS_KEY) ?? null;
      const parsed: unknown = raw === null ? {} : JSON.parse(raw);
      return typeof parsed === "object" && parsed !== null
        ? (parsed as Record<string, boolean>)
        : {};
    } catch {
      return {};
    }
  }

  private async runSync(files: readonly IndusPluginFile[]): Promise<void> {
    const wanted = new Map(files.map((f) => [f.id, f]));
    for (const [id, current] of [...this.loaded]) {
      const next = wanted.get(id);
      if (next === undefined || next.hash !== current.file.hash || next.status !== "ready") {
        this.unload(id);
      }
    }
    for (const id of [...this.records.keys()]) {
      if (!wanted.has(id)) this.records.delete(id);
    }
    for (const file of files) {
      if (this.loaded.get(file.id)?.file.hash === file.hash) continue;
      await this.loadOne(file);
    }
    this.changed();
  }

  private record(file: IndusPluginFile, patch: Partial<PluginRecord>): void {
    const base: PluginRecord = {
      id: file.id,
      kind: file.kind,
      file: file.file,
      status: "loading",
      ...(file.packageName === undefined ? {} : { packageName: file.packageName }),
    };
    this.records.set(file.id, { ...base, ...patch });
  }

  private async importWithDeadline(url: string): Promise<unknown> {
    const limit = this.options.timeoutMs ?? LOAD_TIMEOUT_MS;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const deadline = new Promise<never>((_, reject) => {
      timer = setTimeout(
        () => reject(new Error(`the module did not load within ${limit} ms`)),
        limit,
      );
    });
    try {
      return await Promise.race([this.options.importModule(url), deadline]);
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
  }

  private async loadOne(file: IndusPluginFile): Promise<void> {
    if (file.status !== "ready") {
      this.record(file, { status: "refused", error: file.reason ?? "refused by the check" });
      return;
    }
    let plugin: DesktopPlugin;
    try {
      const module = (await this.importWithDeadline(moduleUrl(file))) as { default?: unknown };
      plugin = definePlugin(module.default as DesktopPlugin);
      if (plugin.id !== file.id) {
        throw new Error(`the file is ${file.id} but the plugin calls itself ${plugin.id}`);
      }
    } catch (error) {
      this.record(file, { status: "error", error: errorText(error) });
      return;
    }
    const meta = {
      ...(plugin.name === undefined ? {} : { name: plugin.name }),
      ...(plugin.description === undefined ? {} : { description: plugin.description }),
    };
    const choice = this.decisions()[file.id];
    const packageOn =
      file.kind !== "package" ||
      (file.packageName !== undefined &&
        (await (this.options.packageEnabled?.(file.packageName) ?? Promise.resolve(false))));
    // a package half follows its agent plugin and is off by default; a disk plugin is on unless
    // it says otherwise
    const fallback = file.kind === "package" ? packageOn : plugin.defaultEnabled !== false;
    const on = (choice ?? fallback) && packageOn;
    if (!on) {
      this.record(file, { ...meta, status: "disabled" });
      return;
    }
    const lifetime = new PluginLifetime();
    let open = true;
    try {
      const dispose = this.registry.transaction(`plugin:${file.id}`, (stage) => {
        const ctx = createPluginContext({
          pluginId: file.id,
          host: this.options.host,
          registry: this.registry,
          lifetime,
          stage: () => (open ? stage : null),
        });
        plugin.register(ctx);
      });
      open = false;
      lifetime.track(dispose);
    } catch (error) {
      open = false;
      lifetime.end(() => undefined);
      this.registry.removeSource(`plugin:${file.id}`);
      this.record(file, { ...meta, status: "error", error: errorText(error) });
      return;
    }
    this.loaded.set(file.id, { file, lifetime });
    this.record(file, { ...meta, status: "loaded" });
  }

  private changed(): void {
    for (const listener of [...this.listeners]) {
      try {
        listener();
      } catch {
        // ignore
      }
    }
  }
}

function errorText(error: unknown): string {
  return (error instanceof Error ? error.message : String(error)).slice(0, 500);
}
