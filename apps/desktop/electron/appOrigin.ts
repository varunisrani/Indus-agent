// Where the app's own pages live. A dev build loads the Vite dev server (loopback only); a
// packaged build loads one `file://` index. Navigation guards (P20-02), the IPC sender check
// (P20-03) and the CSP header (P20-04) all ask the same question through `isAppUrl`.

import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export type AppOrigin =
  | { kind: "dev"; origin: string }
  | { kind: "file"; indexPath: string; caseInsensitive: boolean };

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

/** A dev server URL we accept: http(s) on a loopback host with a port. Null otherwise. */
export function devServerOrigin(raw: string | undefined): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (!LOOPBACK_HOSTS.has(url.hostname) || url.port === "") return null;
    if (url.username || url.password) return null;
    return url.origin;
  } catch {
    return null;
  }
}

export function fileAppOrigin(indexPath: string, platform: string = process.platform): AppOrigin {
  return {
    kind: "file",
    indexPath: path.resolve(indexPath),
    caseInsensitive: platform === "win32",
  };
}

/** True when `url` is one of the app's own pages (any query or hash). */
export function isAppUrl(url: string, origin: AppOrigin): boolean {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  if (origin.kind === "dev") return parsed.origin === origin.origin;
  if (parsed.protocol !== "file:" || (parsed.host !== "" && parsed.host !== "localhost")) {
    return false;
  }
  let file: string;
  try {
    file = path.resolve(fileURLToPath(`file://${parsed.pathname}`));
  } catch {
    return false;
  }
  return origin.caseInsensitive
    ? file.toLowerCase() === origin.indexPath.toLowerCase()
    : file === origin.indexPath;
}

/** The app's entry URL with an optional query (window kind flags) and hash (route). */
export function appEntryUrl(origin: AppOrigin, query = "", hash = ""): string {
  const base = origin.kind === "dev" ? `${origin.origin}/` : pathToFileURL(origin.indexPath).href;
  const q = query === "" || query.startsWith("?") ? query : `?${query}`;
  const h = hash === "" || hash.startsWith("#") ? hash : `#${hash}`;
  return `${base}${q}${h}`;
}

/** URL patterns (for `webRequest` filters) that cover the app's pages. */
export function appUrlPatterns(origin: AppOrigin): string[] {
  return origin.kind === "dev" ? [`${origin.origin}/*`] : ["file:///*"];
}
