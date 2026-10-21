// Desktop plugins on disk (P24-32): find them, check them, serve them, reload them on save.
//
// Two doors, both under the Indus home:
//   - `<home>/desktop-plugins/<id>.js` (or `.mjs`): one ESM file per plugin (`kind: "disk"`);
//   - `<home>/plugins/<name>/desktop/plugin.js`: the desktop half of an agent plugin package
//     (`kind: "package"`); the renderer loads it only while the agent half is enabled (P13 consent).
//
// Every file is checked here, in main, before the renderer may evaluate it: the id must be a plugin
// id, the file at most `MAX_PLUGIN_BYTES` (a larger one is refused from its size alone, never read),
// valid UTF-8, and pass `scanPluginSource` (import allow-list, denied names, no JSX). An accepted
// file is kept as the checked text with its two allowed imports pointed at the host shims, under a
// content hash; the renderer imports it as `indus-plugin://disk/<hash>.js`, and the scheme serves
// that text and nothing else (no file path ever reaches the renderer's import).
//
// Hot reload: both folders are watched; changes within `RELOAD_DEBOUNCE_MS` of each other are one
// rescan, after which `onChange` gets the new list (the renderer then unloads changed or removed
// plugins and loads the new versions).

import { createHash } from "node:crypto";
import path from "node:path";
import type { MainContext } from "../context.ts";
import type { EventTarget as IpcEventTarget } from "../ipc/registry.ts";
import { PLUGIN_SCHEME } from "../security/csp.ts";
import { ALLOWED_IMPORTS, rewriteImports, scanPluginSource } from "./importScan.ts";
import { REACT_EXPORTS, SDK_EXPORTS } from "./sdkNames.ts";

export { PLUGIN_SCHEME };
export const MAX_PLUGIN_BYTES = 16 * 1024 * 1024;
export const RELOAD_DEBOUNCE_MS = 250;
export const DISK_DIR = "desktop-plugins";
export const PLUGIN_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;
/** The global key the renderer puts the host object under before it imports any plugin. */
export const HOST_KEY = "indus:desktop-plugin-host";

export type PluginKind = "disk" | "package";
export type FileStatus = "ready" | "refused";

export interface PluginFile {
  id: string;
  kind: PluginKind;
  /** The file on disk (shown in the inventory, for "reveal"). */
  file: string;
  size: number;
  /** sha256 of the checked, rewritten text; the module URL's name. "" when refused. */
  hash: string;
  status: FileStatus;
  reason?: string;
  /** The agent package this is the desktop half of (`kind: "package"`). */
  packageName?: string;
}

export interface PluginFs {
  readdir(dir: string): Promise<string[]>;
  stat(file: string): Promise<{ size: number; isFile(): boolean; isDirectory(): boolean }>;
  readFile(file: string): Promise<Uint8Array>;
}

export interface Watcher {
  close(): void;
}

export type WatchFn = (dir: string, onEvent: () => void) => Watcher | null;

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface DiskPluginsOptions {
  home: string;
  fs: PluginFs;
  watch?: WatchFn;
  timers?: Timers;
  log?: (line: string) => void;
  onChange?: (files: PluginFile[]) => void;
}

const SHIM_URLS: Readonly<Record<string, string>> = {
  "@indus/desktop-sdk": `${PLUGIN_SCHEME}://host/sdk.js`,
  react: `${PLUGIN_SCHEME}://host/react.js`,
  "react/jsx-runtime": `${PLUGIN_SCHEME}://host/jsx-runtime.js`,
};

function shimModule(member: "sdk" | "react" | "jsx", names: readonly string[]): string {
  const lines = [
    `const host = globalThis[Symbol.for(${JSON.stringify(HOST_KEY)})];`,
    'if (!host) throw new Error("the Indus plugin host is not ready");',
    `const value = host.${member};`,
    "export default value;",
  ];
  if (names.length > 0) lines.push(`export const { ${names.join(", ")} } = value;`);
  return `${lines.join("\n")}\n`;
}

const SHIMS: Readonly<Record<string, string>> = {
  "sdk.js": shimModule("sdk", SDK_EXPORTS),
  "react.js": shimModule("react", REACT_EXPORTS),
  "jsx-runtime.js": shimModule("jsx", ["jsx", "jsxs", "Fragment"]),
};

const UTF8 = new TextDecoder("utf-8", { fatal: true });

/** The plugin id of a disk plugin file name, or null when it is not one. */
export function diskPluginId(name: string): string | null {
  const match = /^(.+)\.(m?js)$/.exec(name);
  const id = match?.[1] ?? "";
  return PLUGIN_ID.test(id) ? id : null;
}

export class DiskPlugins {
  private files: PluginFile[] = [];
  private sources = new Map<string, string>();
  private watchers: Watcher[] = [];
  private pending: unknown = null;
  private scanning: Promise<PluginFile[]> | null = null;
  private again = false;
  private readonly timers: Timers;
  private readonly options: DiskPluginsOptions;

  constructor(options: DiskPluginsOptions) {
    this.options = options;
    this.timers = options.timers ?? {
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
    };
  }

  get diskDir(): string {
    return path.join(this.options.home, DISK_DIR);
  }

  get packagesDir(): string {
    return path.join(this.options.home, "plugins");
  }

  list(): PluginFile[] {
    return this.files.map((file) => ({ ...file }));
  }

  /** The checked module text for `hash`, or null. */
  source(hash: string): string | null {
    return this.sources.get(hash) ?? null;
  }

  /** Find and check every plugin file (one scan at a time; a request during one runs after). */
  scan(): Promise<PluginFile[]> {
    if (this.scanning !== null) {
      this.again = true;
      return this.scanning;
    }
    this.scanning = this.runScan().finally(() => {
      this.scanning = null;
      if (this.again) {
        this.again = false;
        void this.scan();
      }
    });
    return this.scanning;
  }

  private async candidates(): Promise<Array<{ id: string; kind: PluginKind; file: string }>> {
    const fs = this.options.fs;
    const found: Array<{ id: string; kind: PluginKind; file: string }> = [];
    for (const name of (await fs.readdir(this.diskDir).catch(() => [])).sort()) {
      const id = diskPluginId(name);
      if (id !== null) found.push({ id, kind: "disk", file: path.join(this.diskDir, name) });
    }
    for (const name of (await fs.readdir(this.packagesDir).catch(() => [])).sort()) {
      if (!PLUGIN_ID.test(name)) continue;
      const file = path.join(this.packagesDir, name, "desktop", "plugin.js");
      const isFile = await fs.stat(file).then(
        (s) => s.isFile(),
        () => false,
      );
      if (isFile) found.push({ id: name, kind: "package", file });
    }
    return found;
  }

  private async check(
    id: string,
    kind: PluginKind,
    file: string,
    sources: Map<string, string>,
  ): Promise<PluginFile> {
    const base: PluginFile = {
      id,
      kind,
      file,
      size: 0,
      hash: "",
      status: "refused",
      ...(kind === "package" ? { packageName: id } : {}),
    };
    let size: number;
    try {
      const info = await this.options.fs.stat(file);
      if (!info.isFile()) return { ...base, reason: "not a file" };
      size = info.size;
    } catch {
      return { ...base, reason: "could not be read" };
    }
    if (size > MAX_PLUGIN_BYTES) {
      return { ...base, size, reason: `larger than ${MAX_PLUGIN_BYTES} bytes` };
    }
    let text: string;
    try {
      const bytes = await this.options.fs.readFile(file);
      if (bytes.byteLength > MAX_PLUGIN_BYTES) {
        return { ...base, size: bytes.byteLength, reason: `larger than ${MAX_PLUGIN_BYTES} bytes` };
      }
      size = bytes.byteLength;
      text = UTF8.decode(bytes);
    } catch {
      return { ...base, size, reason: "not UTF-8 text" };
    }
    const scan = scanPluginSource(text);
    if (!scan.ok) return { ...base, size, reason: scan.reason };
    const served = rewriteImports(text, scan.imports, (spec) =>
      ALLOWED_IMPORTS.has(spec) ? (SHIM_URLS[spec] ?? spec) : spec,
    );
    const hash = createHash("sha256").update(served).digest("hex");
    sources.set(hash, served);
    return { ...base, size, hash, status: "ready" };
  }

  private async runScan(): Promise<PluginFile[]> {
    const next: PluginFile[] = [];
    const seen = new Set<string>();
    const sources = new Map<string, string>();
    for (const { id, kind, file } of await this.candidates()) {
      if (seen.has(id)) {
        next.push({ id, kind, file, size: 0, hash: "", status: "refused", reason: "duplicate-id" });
        continue;
      }
      seen.add(id);
      const checked = await this.check(id, kind, file, sources);
      if (checked.status === "refused") {
        this.options.log?.(`[plugins] ${id} refused: ${checked.reason ?? "?"}`);
      }
      next.push(checked);
    }
    this.files = next;
    this.sources = sources;
    return this.list();
  }

  /** Watch both folders; changes are debounced into one rescan, then `onChange`. */
  watch(): void {
    const watch = this.options.watch;
    if (watch === undefined || this.watchers.length > 0) return;
    for (const dir of [this.diskDir, this.packagesDir]) {
      const watcher = watch(dir, () => this.changed());
      if (watcher !== null) this.watchers.push(watcher);
    }
  }

  /** A file changed: rescan once the folder is quiet for `RELOAD_DEBOUNCE_MS`. */
  changed(): void {
    if (this.pending !== null) this.timers.clearTimeout(this.pending);
    this.pending = this.timers.setTimeout(() => {
      this.pending = null;
      void this.scan().then((files) => this.options.onChange?.(files));
    }, RELOAD_DEBOUNCE_MS);
  }

  stop(): void {
    if (this.pending !== null) this.timers.clearTimeout(this.pending);
    this.pending = null;
    for (const watcher of this.watchers) watcher.close();
    this.watchers = [];
  }

  /** The `indus-plugin:` handler: host shims and checked modules, nothing else. */
  async respond(url: string): Promise<{ status: number; body: string }> {
    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch {
      return { status: 404, body: "" };
    }
    const name = parsed.pathname.replace(/^\/+/, "");
    if (parsed.protocol !== `${PLUGIN_SCHEME}:` || parsed.search !== "" || parsed.hash !== "") {
      return { status: 404, body: "" };
    }
    if (parsed.host === "host") {
      const shim = SHIMS[name];
      return shim === undefined ? { status: 404, body: "" } : { status: 200, body: shim };
    }
    if (parsed.host === "disk") {
      const match = /^([0-9a-f]{64})\.js$/.exec(name);
      const text = match?.[1] === undefined ? null : this.source(match[1]);
      return text === null ? { status: 404, body: "" } : { status: 200, body: text };
    }
    return { status: 404, body: "" };
  }
}

/** The part of Electron's `protocol` this needs. */
export interface PluginProtocol {
  handle(scheme: string, handler: (request: Request) => Promise<Response>): void;
}

/** Register the IPC channels and the scheme handler; start watching. */
export function installDiskPlugins(
  context: MainContext,
  deps: { protocol: PluginProtocol; fs: PluginFs; watch?: WatchFn },
): DiskPlugins {
  const plugins: DiskPlugins = new DiskPlugins({
    home: context.paths.home,
    fs: deps.fs,
    ...(deps.watch === undefined ? {} : { watch: deps.watch }),
    log: (line) => context.log.write(line),
    onChange: (files) => {
      for (const win of context.windows.windows()) {
        context.registry.emit(win.webContents as IpcEventTarget, "indus:plugins:changed", {
          files,
        });
      }
    },
  });
  context.registry.handle("indus:plugins:list", async () => {
    if (plugins.list().length === 0) await plugins.scan();
    return { files: plugins.list() };
  });
  context.registry.handle("indus:plugins:rescan", async () => ({ files: await plugins.scan() }));
  deps.protocol.handle(PLUGIN_SCHEME, async (request) => {
    if (request.method !== "GET") return new Response("", { status: 405 });
    const { status, body } = await plugins.respond(request.url);
    return new Response(status === 200 ? body : "", {
      status,
      headers: {
        "Content-Type": "text/javascript; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      },
    });
  });
  void plugins.scan();
  plugins.watch();
  return plugins;
}
