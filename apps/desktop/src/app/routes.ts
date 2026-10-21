// The renderer's routes (P20-15), as a pure classifier over the hash path.
//
//   /                 new chat (home)
//   /<sessionId>      a chat
//   /capabilities, /messaging, /artifacts          pages (empty "coming later" in 1.0)
//   /webhooks, /command-center, /learning-map, /bots  management pages (P24-27, P24-28, P24-31)
//   /settings, /cron, /profiles, /agents           overlays over the route they opened from
//   anything else     → new chat
//
// Reserved names are never read as session ids. Overlays carry their background route in the
// navigation state and return to it on close; opened cold, they return home.

export const NEW_CHAT_PATH = "/";
export const PAGES = [
  "capabilities",
  "messaging",
  "artifacts",
  "webhooks",
  "command-center",
  "learning-map",
  "bots",
] as const;
export const OVERLAYS = ["settings", "cron", "profiles", "agents"] as const;

export type PageId = (typeof PAGES)[number];
export type OverlayId = (typeof OVERLAYS)[number];

export type RouteView =
  | { readonly kind: "chat"; readonly sessionId: string | null }
  | { readonly kind: "page"; readonly page: PageId }
  | { readonly kind: "overlay"; readonly overlay: OverlayId }
  | { readonly kind: "unknown" };

const RESERVED: ReadonlySet<string> = new Set<string>([...PAGES, ...OVERLAYS]);

/** The path part of a router target (`/x?y#z` → `/x`). */
export function pathOf(target: string): string {
  const cut = target.search(/[?#]/);
  return cut < 0 ? target : target.slice(0, cut);
}

export function classifyPath(target: string): RouteView {
  const path = pathOf(target);
  if (path === "" || path === NEW_CHAT_PATH) return { kind: "chat", sessionId: null };
  if (!path.startsWith("/")) return { kind: "unknown" };
  const segment = path.slice(1).replace(/\/$/, "");
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

/** The background route an overlay was opened over, if any. */
export function backgroundOf(state: unknown): string | null {
  const value = (state as { background?: unknown } | null)?.background;
  if (typeof value !== "string") return null;
  const view = classifyPath(value);
  return view.kind === "chat" || view.kind === "page" ? value : null;
}

/** Where closing an overlay goes. */
export function closeTarget(state: unknown): string {
  return backgroundOf(state) ?? NEW_CHAT_PATH;
}
