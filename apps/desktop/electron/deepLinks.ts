// Deep links (P20-34). `indus://<kind>/<name>?a=b` (dev: `indus-dev://`) reaches main three
// ways — the cold-start argv, a second launch's argv and macOS `open-url` — and every one goes
// through `DeepLinkRouter.receive`. Main parses it into `{kind, name, params}` and:
//
//   close-preview  handled here: restore + show + focus the main window, leave full screen, then
//                  tell the page to close its preview (works while the page hears nothing);
//   open/skill/mcp delivered to the main window's renderer once it said it listens
//                  (`indus:deep-link:ready`); until then only the latest link is kept and it is
//                  flushed exactly once on that signal (R08.21).
//
// A delivery shows a tray-hidden main window without activating it: the person's other app keeps
// focus. A reload or crash of the page makes main wait for a fresh ready signal.

import type { MainContext } from "./context.ts";
import type { EventTarget } from "./ipc/registry.ts";
import { DEEP_LINK_MAX_LENGTH } from "./singleInstance.ts";

export const DEEP_LINK_KINDS = ["open", "skill", "mcp", "close-preview"] as const;
export type DeepLinkKind = (typeof DEEP_LINK_KINDS)[number];

export interface DeepLink {
  readonly kind: DeepLinkKind;
  readonly name: string;
  readonly params: Readonly<Record<string, string>>;
}

/** Longest name a link may carry (an app path, a skill identifier or an MCP server name). */
export const DEEP_LINK_NAME_MAX = 2048;
/** At most this many query parameters are kept; a link with more is refused. */
export const DEEP_LINK_MAX_PARAMS = 16;

function hasControl(text: string): boolean {
  for (const char of text) {
    const code = char.codePointAt(0) ?? 0;
    if (code < 0x20 || code === 0x7f) return true;
  }
  return false;
}

/** `raw` as a link on one of `schemes`, or null (wrong scheme, unknown kind, malformed, too long). */
export function parseDeepLink(raw: unknown, schemes: readonly string[]): DeepLink | null {
  if (typeof raw !== "string") return null;
  const text = raw.trim();
  if (text === "" || text.length > DEEP_LINK_MAX_LENGTH) return null;
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return null;
  }
  const scheme = url.protocol.replace(/:$/, "").toLowerCase();
  if (!schemes.map((s) => s.toLowerCase()).includes(scheme)) return null;
  if (url.username !== "" || url.password !== "" || url.port !== "") return null;
  const kind = url.hostname.toLowerCase();
  if (!(DEEP_LINK_KINDS as readonly string[]).includes(kind)) return null;
  let name: string;
  try {
    name = decodeURIComponent(url.pathname.replace(/^\/+/, ""));
  } catch {
    return null;
  }
  if (name.length > DEEP_LINK_NAME_MAX || hasControl(name)) return null;
  const params: Record<string, string> = {};
  let count = 0;
  for (const [key, value] of url.searchParams) {
    count += 1;
    if (count > DEEP_LINK_MAX_PARAMS) return null;
    if (key === "" || key.length > 64 || value.length > DEEP_LINK_NAME_MAX) return null;
    params[key] = value;
  }
  return { kind: kind as DeepLinkKind, name, params };
}

/** The parts of the main window a link needs. */
export interface LinkWindow {
  readonly webContents: EventTarget & {
    readonly id: number;
    on(event: string, listener: (...args: never[]) => void): unknown;
  };
  isDestroyed(): boolean;
  isMinimized(): boolean;
  isVisible(): boolean;
  isFullScreen(): boolean;
  restore(): void;
  show(): void;
  showInactive(): void;
  focus(): void;
  setFullScreen(flag: boolean): void;
}

export interface DeepLinkHost {
  mainWindow(): LinkWindow | null;
  /** Open the main window (no focus); its renderer's ready signal then flushes the link. */
  openMain(): void;
  sendLink(contents: LinkWindow["webContents"], link: DeepLink): boolean;
  sendClosePreview(contents: LinkWindow["webContents"]): boolean;
  log(line: string): void;
}

/** Collects links from every entry point; delivers them once main is ready and the page listens. */
export class DeepLinkRouter {
  private readonly schemes: readonly string[];
  private host: DeepLinkHost | null = null;
  private pending: DeepLink | null = null;
  private readyContents: number | null = null;
  private readonly watched = new Set<number>();
  private earlyClosePreview = false;

  constructor(scheme: string) {
    this.schemes = [scheme];
  }

  /** The link waiting for the renderer (tests and diagnostics). */
  waiting(): DeepLink | null {
    return this.pending;
  }

  /** Main is ready: deliver anything that arrived before. */
  attach(host: DeepLinkHost): void {
    this.host = host;
    if (this.earlyClosePreview) {
      this.earlyClosePreview = false;
      this.closePreview();
    }
    if (this.pending !== null) this.route(this.pending);
  }

  /** A link from argv, a second launch or `open-url`; anything else is dropped and logged. */
  receive(raw: unknown): void {
    const link = parseDeepLink(raw, this.schemes);
    if (link === null) {
      this.host?.log("[deep-link] ignored a link that is not a known indus:// link");
      return;
    }
    if (this.host === null) {
      if (link.kind === "close-preview") this.earlyClosePreview = true;
      else this.pending = link;
      return;
    }
    this.route(link);
  }

  /** `indus:deep-link:ready` from `contentsId`; only the main window's page counts. */
  rendererReady(contentsId: number): void {
    const host = this.host;
    if (host === null) return;
    const main = host.mainWindow();
    if (main === null || main.isDestroyed() || main.webContents.id !== contentsId) return;
    if (this.readyContents === contentsId) return; // a repeated signal from the same page load
    this.readyContents = contentsId;
    this.watch(main);
    if (this.pending !== null) this.deliver(main, this.pending);
  }

  /** The main page reloaded, crashed or closed: wait for a new ready signal. */
  rendererGone(contentsId: number): void {
    if (this.readyContents === contentsId) this.readyContents = null;
  }

  private watch(main: LinkWindow): void {
    const id = main.webContents.id;
    if (this.watched.has(id)) return;
    this.watched.add(id);
    const gone = () => this.rendererGone(id);
    main.webContents.on("did-navigate", gone);
    main.webContents.on("render-process-gone", gone);
    main.webContents.on("destroyed", () => {
      this.watched.delete(id);
      gone();
    });
  }

  private route(link: DeepLink): void {
    const host = this.host;
    if (host === null) return;
    if (link.kind === "close-preview") {
      this.closePreview();
      return;
    }
    const main = host.mainWindow();
    if (main === null || main.isDestroyed()) {
      this.pending = link;
      host.openMain();
      return;
    }
    if (this.readyContents !== main.webContents.id) {
      this.pending = link; // latest wins
      return;
    }
    this.deliver(main, link);
  }

  private deliver(main: LinkWindow, link: DeepLink): void {
    const host = this.host;
    if (host === null) return;
    // Un-hide without activating: a link never takes focus from the app the person is in.
    if (!main.isVisible()) main.showInactive();
    if (host.sendLink(main.webContents, link)) {
      this.pending = null;
      host.log(`[deep-link] delivered ${link.kind}`);
    } else {
      this.pending = link;
      host.log(`[deep-link] could not deliver ${link.kind}; kept for the next ready signal`);
    }
  }

  private closePreview(): void {
    const host = this.host;
    if (host === null) return;
    const main = host.mainWindow();
    if (main === null || main.isDestroyed()) {
      host.log("[deep-link] close-preview: no main window");
      return;
    }
    // The person asked to get out of a full-screen preview: bring the window back for real.
    if (main.isMinimized()) main.restore();
    if (!main.isVisible()) main.show();
    main.focus();
    if (main.isFullScreen()) main.setFullScreen(false);
    host.sendClosePreview(main.webContents);
    host.log("[deep-link] close-preview handled in main");
  }
}

/**
 * Registers `indus:deep-link:ready` (an install step, before the registry is sealed) and returns
 * `start`, which attaches the router to the app's windows once the main window exists.
 */
export function installDeepLinks(context: MainContext, router: DeepLinkRouter): () => void {
  const { registry, windows, log } = context;
  registry.on("indus:deep-link:ready", (_payload, { senderId }) => router.rendererReady(senderId));
  return () =>
    router.attach({
      mainWindow: () => windows.mainWindow() as unknown as LinkWindow | null,
      openMain: () => {
        windows.openMain();
      },
      sendLink: (contents, link) =>
        registry.emit(contents, "indus:deep-link", {
          kind: link.kind,
          name: link.name,
          params: { ...link.params },
        }),
      sendClosePreview: (contents) => registry.emit(contents, "indus:preview:close-requested", {}),
      log: (line) => log.write(line),
    });
}
