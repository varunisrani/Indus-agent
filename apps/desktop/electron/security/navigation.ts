// Navigation and popup policy for every web contents the app has (D-16.20, R08.8).
//
// - An app window's main frame may only show the app itself; any other navigation or redirect is
//   cancelled (and never "opened externally instead": that would let page content drive the OS
//   browser).
// - `window.open` / `target=_blank` is denied for every frame of every web contents — app frames,
//   iframes, webview guests. The handler has no side effect beyond one log line with the origin:
//   it opens nothing and creates no window. Even a sandboxed iframe without `allow-popups` (which
//   can still reach the handler) gets nothing.
// - A `<webview>` may attach only on the preview partition, only with our guest preload, with
//   Node off, isolation and sandbox on, and never with `allowpopups`.

import { type AppOrigin, isAppUrl } from "../appOrigin.ts";

/** The preview pane's persistent partition (plain name: R08.9). */
export const PREVIEW_PARTITION = "persist:indus-preview";

export interface PreventableEvent {
  preventDefault(): void;
  url?: string;
  isMainFrame?: boolean;
}

export interface WindowOpenDetails {
  url: string;
}

/** The parts of Electron's WebContents the guard uses. */
export interface GuardedContents {
  readonly id: number;
  getType?(): string;
  on(event: string, listener: (...args: never[]) => void): unknown;
  setWindowOpenHandler(handler: (details: WindowOpenDetails) => { action: "deny" }): void;
}

export interface NavigationGuardOptions {
  appOrigin: AppOrigin;
  /** Absolute path of the preview guest preload (P20-28); null until it exists. */
  guestPreloadPath: string | null;
  log: (line: string) => void;
}

/** The origin of `url` for a log line (never its path, query or fragment), or its scheme. */
export function describeUrl(url: string): string {
  try {
    const parsed = new URL(url);
    return parsed.origin === "null" ? parsed.protocol : parsed.origin;
  } catch {
    return "<not a url>";
  }
}

/** The window-open handler every web contents gets: always deny, log the origin, nothing else. */
export function denyAllWindowOpen(log: (line: string) => void, contentsId: number) {
  return (details: WindowOpenDetails): { action: "deny" } => {
    try {
      log(
        `[security] window.open denied (contents ${contentsId}, ${describeUrl(details?.url ?? "")})`,
      );
    } catch {
      // A failing logger cannot change the decision.
    }
    return { action: "deny" };
  };
}

function urlOf(event: PreventableEvent, fallback: unknown): string {
  if (typeof event?.url === "string") return event.url;
  return typeof fallback === "string" ? fallback : "";
}

/** Make the attach parameters of a `<webview>` safe, or cancel the attach. */
export function hardenWebviewAttach(
  event: PreventableEvent,
  webPreferences: Record<string, unknown>,
  params: Record<string, unknown>,
  options: NavigationGuardOptions,
): boolean {
  const partition = typeof params.partition === "string" ? params.partition : "";
  if (partition !== PREVIEW_PARTITION || options.guestPreloadPath === null) {
    event.preventDefault();
    options.log(`[security] webview refused (partition ${JSON.stringify(partition)})`);
    return false;
  }
  delete webPreferences.preloadURL;
  webPreferences.preload = options.guestPreloadPath;
  webPreferences.nodeIntegration = false;
  webPreferences.nodeIntegrationInSubFrames = false;
  webPreferences.contextIsolation = true;
  webPreferences.sandbox = true;
  webPreferences.webSecurity = true;
  webPreferences.allowRunningInsecureContent = false;
  delete params.allowpopups;
  return true;
}

/** Install the policy on one web contents. App windows are confined to the app's pages. */
export function hardenWebContents(
  contents: GuardedContents,
  options: NavigationGuardOptions,
): void {
  const { appOrigin, log } = options;
  contents.setWindowOpenHandler(denyAllWindowOpen(log, contents.id));
  const isGuest = contents.getType?.() === "webview";
  if (!isGuest) {
    const confine = (event: PreventableEvent, url: unknown, ...rest: unknown[]) => {
      // will-navigate / will-redirect: (details, url, isInPlace, isMainFrame, ...)
      const mainFrame =
        typeof event?.isMainFrame === "boolean"
          ? event.isMainFrame
          : typeof rest[1] === "boolean"
            ? rest[1]
            : true;
      if (!mainFrame) return;
      const target = urlOf(event, url);
      if (isAppUrl(target, appOrigin)) return;
      event.preventDefault();
      log(`[security] navigation cancelled (contents ${contents.id}, ${describeUrl(target)})`);
    };
    contents.on("will-navigate", confine as (...args: never[]) => void);
    contents.on("will-redirect", confine as (...args: never[]) => void);
  }
  contents.on("will-attach-webview", ((
    event: PreventableEvent,
    webPreferences: Record<string, unknown>,
    params: Record<string, unknown>,
  ) => {
    hardenWebviewAttach(event, webPreferences, params, options);
  }) as (...args: never[]) => void);
}
