// The renderer's routes (P20-15), as a pure classifier over the hash path.
//
//   /                 new chat (home)
//   /<sessionId>      a chat
//   /capabilities, /messaging, /artifacts, /bots   pages (P24-26..31)
//   /settings, /cron, /profiles, /agents,          overlays over the route they opened from
//   /webhooks, /command-center, /learning-map,     (W52-l-1: the management views are overlays,
//   /session-import                                 as in the reference; paths unchanged)
//   a contributed route (P24-32)                   a `route` contribution's page, when the caller
//                                                  passes the registry's route paths
//   anything else     → new chat
//
// Reserved names are never read as session ids. Overlays carry their background route in the
// navigation state and return to it on close; opened cold, they return home. A contributed route
// never shadows a built-in page or overlay, except the pages in `CONTRIBUTED_PAGES`, which are
// reserved here but served (and listed in the sidebar) by a bundled contribution: while that
// contribution is off, the path is no page at all.

export const NEW_CHAT_PATH = "/";
export const PAGES = ["capabilities", "messaging", "artifacts", "bots"] as const;
export const OVERLAYS = [
  "settings",
  "cron",
  "profiles",
  "agents",
  "webhooks",
  "command-center",
  "learning-map",
  "session-import",
] as const;
/** Reserved pages served by a bundled contribution (the Bots surface, P24-31). */
export const CONTRIBUTED_PAGES: readonly PageId[] = ["bots"];

export type PageId = (typeof PAGES)[number];
export type OverlayId = (typeof OVERLAYS)[number];

export type RouteView =
  | { readonly kind: "chat"; readonly sessionId: string | null }
  | { readonly kind: "page"; readonly page: PageId }
  | { readonly kind: "overlay"; readonly overlay: OverlayId }
  | { readonly kind: "contributed"; readonly path: string }
  | { readonly kind: "unknown" };

/** A contributed page whose contribution is off is no page. */
export function isContributedPage(page: PageId): boolean {
  return CONTRIBUTED_PAGES.includes(page);
}

/** A route path as the registry keys it: no query or fragment, no trailing slash. */
export function routeKey(target: string): string {
  const path = pathOf(target);
  return path.length > 1 ? path.replace(/\/+$/, "") : path;
}

const RESERVED: ReadonlySet<string> = new Set<string>([...PAGES, ...OVERLAYS]);

/** The path part of a router target (`/x?y#z` → `/x`). */
export function pathOf(target: string): string {
  const cut = target.search(/[?#]/);
  return cut < 0 ? target : target.slice(0, cut);
}

/**
 * What `target` shows. `contributed` holds the paths of the registry's `route` contributions
 * (`routeKey` form); without it no path is read as a contributed route.
 */
export function classifyPath(target: string, contributed?: ReadonlySet<string>): RouteView {
  const path = pathOf(target);
  if (path === "" || path === NEW_CHAT_PATH) return { kind: "chat", sessionId: null };
  if (!path.startsWith("/")) return { kind: "unknown" };
  const key = routeKey(path);
  const segment = path.slice(1).replace(/\/$/, "");
  const builtIn = (PAGES as readonly string[]).includes(segment)
    ? !isContributedPage(segment as PageId)
    : (OVERLAYS as readonly string[]).includes(segment);
  if (!builtIn && contributed?.has(key) === true) return { kind: "contributed", path: key };
  if (segment === "" || segment.includes("/")) return { kind: "unknown" };
  if ((PAGES as readonly string[]).includes(segment))
    return { kind: "page", page: segment as PageId };
  if ((OVERLAYS as readonly string[]).includes(segment)) {
    return { kind: "overlay", overlay: segment as OverlayId };
  }
  let sessionId: string;
  try {
    sessionId = decodeURIComponent(segment);
  } catch {
    return { kind: "unknown" };
  }
  if (sessionId.trim() === "" || RESERVED.has(sessionId)) return { kind: "unknown" };
  return { kind: "chat", sessionId };
}

export function sessionPath(sessionId: string): string {
  return `/${encodeURIComponent(sessionId)}`;
}

export function pagePath(page: PageId): string {
  return `/${page}`;
}

export function overlayPath(overlay: OverlayId): string {
  return `/${overlay}`;
}

/** Navigation state an overlay is opened with. */
export interface OverlayState {
  readonly background: string;
}

/** The overlay state for opening one from `current` (an overlay opened from another overlay
 * keeps the first background, so closing never lands on an overlay). */
export function overlayStateFrom(current: string, currentState: unknown): OverlayState {
  const existing = backgroundOf(currentState);
  if (classifyPath(current).kind === "overlay") return { background: existing ?? NEW_CHAT_PATH };
  return { background: current };
}

/** The background route an overlay was opened over, if any (a contributed route counts when
 * `contributed` names it). */
export function backgroundOf(state: unknown, contributed?: ReadonlySet<string>): string | null {
  const value = (state as { background?: unknown } | null)?.background;
  if (typeof value !== "string") return null;
  const view = classifyPath(value, contributed);
  return view.kind === "chat" || view.kind === "page" || view.kind === "contributed" ? value : null;
}

/** Where closing an overlay goes. */
export function closeTarget(state: unknown, contributed?: ReadonlySet<string>): string {
  return backgroundOf(state, contributed) ?? NEW_CHAT_PATH;
}
