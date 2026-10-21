// What the preview pane shows (P20-28): a web page or a local file. The address field accepts a
// URL, a bare `host:port` (a dev server), a `file:` URL or an absolute path; anything else (a
// `javascript:`, `data:` or app-scheme URL) is refused.

/** Main's preview partition (`electron/security/navigation.ts`; a test keeps them equal). */
export const PREVIEW_PARTITION = "persist:indus-preview";
/** The guest preload's `sendToHost` channel (`electron/preview/guestPreload.ts`; tested equal). */
export const GUEST_LINK_CHANNEL = "indus-preview:open-link";

export type PreviewTarget = { kind: "url"; url: string } | { kind: "file"; path: string };

export type FileView = "html" | "image" | "text";

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|avif|bmp|ico|svg)$/i;
const HTML_EXT = /\.html?$/i;
const LOOPBACK = /^(localhost|127\.\d+\.\d+\.\d+|\[::1\]|0\.0\.0\.0)(:\d+)?(\/|$)/i;
const BARE_HOST = /^[a-z0-9.-]+(:\d+)?(\/\S*)?$/i;

/** True for an http(s) URL without user-info (what may load in the guest or open externally). */
export function isWebUrl(raw: string): boolean {
  try {
    const url = new URL(raw);
    return (
      (url.protocol === "http:" || url.protocol === "https:") &&
      url.username === "" &&
      url.password === ""
    );
  } catch {
    return false;
  }
}

function isAbsolutePath(raw: string): boolean {
  return raw.startsWith("/") || /^[a-zA-Z]:[\\/]/.test(raw) || raw.startsWith("\\\\");
}

/** A local path from a `file:` URL (null for a remote host or a bad URL). */
export function pathFromFileUrl(raw: string): string | null {
  try {
    const url = new URL(raw);
    if (url.protocol !== "file:" || (url.host !== "" && url.host !== "localhost")) return null;
    const decoded = decodeURIComponent(url.pathname);
    return /^\/[a-zA-Z]:\//.test(decoded) ? decoded.slice(1).replace(/\//g, "\\") : decoded;
  } catch {
    return null;
  }
}

/** The `file:` URL of a local path (for HTML files shown in the guest). */
export function fileUrlOf(path: string): string {
  const slashed = path.replace(/\\/g, "/");
  const withRoot = /^[a-zA-Z]:\//.test(slashed) ? `/${slashed}` : slashed;
  return `file://${withRoot.split("/").map(encodeURIComponent).join("/")}`;
}

/** Parse what the person typed into the address field. */
export function parseAddress(input: string): PreviewTarget | null {
  const raw = input.trim();
  if (raw === "") return null;
  if (/^file:/i.test(raw)) {
    const path = pathFromFileUrl(raw);
    return path === null ? null : { kind: "file", path };
  }
  if (isAbsolutePath(raw)) return { kind: "file", path: raw };
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(raw))
    return isWebUrl(raw) ? { kind: "url", url: new URL(raw).href } : null;
  if (LOOPBACK.test(raw)) return { kind: "url", url: new URL(`http://${raw}`).href };
  if (BARE_HOST.test(raw) && raw.includes(".") && !raw.includes(":/")) {
    const candidate = `https://${raw}`;
    return isWebUrl(candidate) ? { kind: "url", url: new URL(candidate).href } : null;
  }
  return null;
}

export function fileViewOf(path: string): FileView {
  if (HTML_EXT.test(path)) return "html";
  if (IMAGE_EXT.test(path)) return "image";
  return "text";
}

export function addressOf(target: PreviewTarget | null): string {
  if (target === null) return "";
  return target.kind === "url" ? target.url : target.path;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}
