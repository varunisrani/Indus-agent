// Link titles and favicons (P20-36), fetched by main so no page code ever runs in an app window.
// First a plain, capped GET of the page for its `<title>` and icon link; only when that finds no
// title, a hidden hardened window (no preload, sandbox, isolation, in-memory partition, muted,
// off-screen, unfocusable) loads the page for at most 8 s and is destroyed. Favicons come back as
// small image data URLs. Only http/https, no user-info, no loopback or private-network hosts.

import type { Timers } from "./hud.ts";

export const LINK_FETCH_TIMEOUT_MS = 6_000;
export const LINK_FETCH_MAX_BYTES = 512 * 1024;
export const LINK_WINDOW_TIMEOUT_MS = 8_000;
export const FAVICON_MAX_BYTES = 64 * 1024;
export const LINK_CACHE_MAX = 200;
export const LINK_CACHE_TTL_MS = 30 * 60_000;
export const LINK_MAX_WINDOWS = 2;
export const LINK_URL_MAX = 2_048;
export const LINK_TITLE_MAX = 300;
/** The hidden window's partition: no `persist:` prefix, so nothing reaches the disk. */
export const LINK_PARTITION = "indus-links";

export interface LinkPreview {
  title: string | null;
  favicon: string | null;
}

const PRIVATE_V4 = [
  /^127\./,
  /^10\./,
  /^192\.168\./,
  /^169\.254\./,
  /^172\.(1[6-9]|2\d|3[01])\./,
  /^0\./,
  /^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./,
];

/** A URL main may fetch for a preview, or null. */
export function checkLinkUrl(raw: string): URL | null {
  if (raw.length > LINK_URL_MAX) return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (url.username !== "" || url.password !== "") return null;
  const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (host === "" || host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local"))
    return null;
  if (PRIVATE_V4.some((pattern) => pattern.test(host))) return null;
  if (host.includes(":")) {
    // IPv6 literal: loopback, unspecified, unique-local, link-local, mapped v4.
    if (host === "::1" || host === "::" || /^f[cd]/.test(host) || /^fe[89ab]/.test(host))
      return null;
    if (host.startsWith("::ffff:")) return null;
  }
  return url;
}

const ENTITIES: Readonly<Record<string, string>> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: " ",
};

export function decodeEntities(text: string): string {
  return text.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, name: string) => {
    if (name.startsWith("#x") || name.startsWith("#X")) {
      const code = Number.parseInt(name.slice(2), 16);
      return Number.isFinite(code) && code > 0 && code <= 0x10ffff
        ? String.fromCodePoint(code)
        : whole;
    }
    if (name.startsWith("#")) {
      const code = Number.parseInt(name.slice(1), 10);
      return Number.isFinite(code) && code > 0 && code <= 0x10ffff
        ? String.fromCodePoint(code)
        : whole;
    }
    return ENTITIES[name.toLowerCase()] ?? whole;
  });
}

function tidy(text: string): string | null {
  const clean = decodeEntities(text).replace(/\s+/g, " ").trim();
  if (clean === "") return null;
  return clean.length > LINK_TITLE_MAX ? `${clean.slice(0, LINK_TITLE_MAX - 1)}…` : clean;
}

function attribute(tag: string, name: string): string | null {
  const match = new RegExp(`\\b${name}\\s*=\\s*("([^"]*)"|'([^']*)'|([^\\s>]+))`, "i").exec(tag);
  if (match === null) return null;
  return match[2] ?? match[3] ?? match[4] ?? null;
}

/** The page title: `<title>`, else `og:title`. */
export function extractTitle(html: string): string | null {
  const title = /<title\b[^>]*>([\s\S]*?)<\/title>/i.exec(html);
  if (title !== null) {
    const text = tidy(title[1] ?? "");
    if (text !== null) return text;
  }
  for (const tag of html.match(/<meta\b[^>]*>/gi) ?? []) {
    const property = attribute(tag, "property") ?? attribute(tag, "name");
    if (property?.toLowerCase() === "og:title") {
      const content = attribute(tag, "content");
      if (content !== null) return tidy(content);
    }
  }
  return null;
}

/** The icon the page names (resolved against `base`), else `/favicon.ico`. */
export function extractIconHref(html: string, base: URL): string {
  let best: string | null = null;
  for (const tag of html.match(/<link\b[^>]*>/gi) ?? []) {
    const rel = (attribute(tag, "rel") ?? "").toLowerCase().split(/\s+/);
    if (!rel.includes("icon") && !rel.includes("apple-touch-icon")) continue;
    const href = attribute(tag, "href");
    if (href === null || href.trim() === "") continue;
    best = decodeEntities(href.trim());
    if (rel.includes("icon")) break;
  }
  try {
    return new URL(best ?? "/favicon.ico", base).toString();
  } catch {
    return new URL("/favicon.ico", base).toString();
  }
}

/** Options for the hidden window (exported for the test). No preload, ever. */
export function linkWindowOptions(): Record<string, unknown> {
  return {
    show: false,
    x: -20_000,
    y: -20_000,
    width: 1024,
    height: 768,
    skipTaskbar: true,
    focusable: false,
    frame: false,
    webPreferences: {
      partition: LINK_PARTITION,
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      nodeIntegrationInSubFrames: false,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      javascript: true,
      images: false,
      spellcheck: false,
      devTools: false,
      autoplayPolicy: "document-user-activation-required",
    },
  };
}

export interface LinkWindowLike {
  readonly webContents: {
    setAudioMuted(muted: boolean): void;
    getTitle(): string;
    on(event: string, listener: (...args: never[]) => void): unknown;
    setWindowOpenHandler(handler: () => { action: "deny" }): void;
  };
  loadURL(url: string): Promise<unknown>;
  destroy(): void;
  isDestroyed(): boolean;
}

type FetchLike = (
  url: string,
  init: { signal: AbortSignal; redirect: "follow"; headers: Record<string, string> },
) => Promise<Response>;

export interface LinkPreviewDeps {
  fetch: FetchLike;
  createWindow(options: Record<string, unknown>): LinkWindowLike;
  timers: Timers;
  now(): number;
  log(line: string): void;
}

async function readCapped(response: Response, max: number): Promise<Uint8Array | null> {
  const reader = response.body?.getReader();
  if (reader === undefined) return null;
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done || value === undefined) break;
    const room = max - total;
    if (value.byteLength >= room) {
      chunks.push(value.subarray(0, room));
      total += room;
      await reader.cancel().catch(() => {});
      break;
    }
    chunks.push(value);
    total += value.byteLength;
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const chunk of chunks) {
    out.set(chunk, at);
    at += chunk.byteLength;
  }
  return out;
}

export class LinkPreviewer {
  private readonly deps: LinkPreviewDeps;
  private readonly cache = new Map<string, { at: number; value: LinkPreview }>();
  private readonly inflight = new Map<string, Promise<LinkPreview>>();
  private windows = 0;

  constructor(deps: LinkPreviewDeps) {
    this.deps = deps;
  }

  preview(raw: string): Promise<LinkPreview> {
    const url = checkLinkUrl(raw);
    if (url === null) return Promise.resolve({ title: null, favicon: null });
    const key = url.toString();
    const cached = this.cache.get(key);
    if (cached !== undefined && this.deps.now() - cached.at < LINK_CACHE_TTL_MS) {
      return Promise.resolve({ ...cached.value });
    }
    const running = this.inflight.get(key);
    if (running !== undefined) return running;
    const work = this.resolve(url)
      .catch(() => ({ title: null, favicon: null }))
      .then((value) => {
        this.inflight.delete(key);
        this.cache.delete(key);
        this.cache.set(key, { at: this.deps.now(), value });
        while (this.cache.size > LINK_CACHE_MAX) {
          const oldest = this.cache.keys().next().value;
          if (oldest === undefined) break;
          this.cache.delete(oldest);
        }
        return { ...value };
      });
    this.inflight.set(key, work);
    return work;
  }

  private async get(url: string, accept: string): Promise<Response | null> {
    const controller = new AbortController();
    const timer = this.deps.timers.setTimeout(() => controller.abort(), LINK_FETCH_TIMEOUT_MS);
    try {
      return await this.deps.fetch(url, {
        signal: controller.signal,
        redirect: "follow",
        headers: { accept },
      });
    } catch {
      return null;
    } finally {
      this.deps.timers.clearTimeout(timer);
    }
  }

  private async resolve(url: URL): Promise<LinkPreview> {
    let title: string | null = null;
    let favicon: string | null = null;
    let finalUrl = url;
    const response = await this.get(url.toString(), "text/html,application/xhtml+xml");
    if (response !== null) {
      const landed = response.url ? checkLinkUrl(response.url) : url;
      if (landed === null) {
        await response.body?.cancel().catch(() => {});
        return { title: null, favicon: null }; // redirected somewhere we do not go
      }
      finalUrl = landed;
      const type = (response.headers.get("content-type") ?? "").toLowerCase();
      if (response.ok && (type.includes("html") || type === "")) {
        const bytes = await readCapped(response, LINK_FETCH_MAX_BYTES);
        const html = bytes === null ? "" : new TextDecoder().decode(bytes);
        title = extractTitle(html);
        favicon = await this.favicon(extractIconHref(html, finalUrl));
      } else {
        await response.body?.cancel().catch(() => {});
      }
    }
    if (title === null && this.windows < LINK_MAX_WINDOWS) {
      const viaWindow = await this.viaHiddenWindow(finalUrl);
      title = viaWindow.title;
      if (favicon === null && viaWindow.icon !== null) favicon = await this.favicon(viaWindow.icon);
    }
    return { title, favicon };
  }

  private async favicon(href: string): Promise<string | null> {
    const url = checkLinkUrl(href);
    if (url === null) return null;
    const response = await this.get(url.toString(), "image/*");
    if (response === null) return null;
    const type = (response.headers.get("content-type") ?? "").split(";")[0]?.trim().toLowerCase();
    const length = Number(response.headers.get("content-length") ?? "0");
    if (
      !response.ok ||
      type === undefined ||
      !type.startsWith("image/") ||
      length > FAVICON_MAX_BYTES
    ) {
      await response.body?.cancel().catch(() => {});
      return null;
    }
    const bytes = await readCapped(response, FAVICON_MAX_BYTES + 1);
    if (bytes === null || bytes.byteLength === 0 || bytes.byteLength > FAVICON_MAX_BYTES)
      return null;
    return `data:${type};base64,${Buffer.from(bytes).toString("base64")}`;
  }

  private viaHiddenWindow(url: URL): Promise<{ title: string | null; icon: string | null }> {
    this.windows += 1;
    let win: LinkWindowLike;
    try {
      win = this.deps.createWindow(linkWindowOptions());
    } catch {
      this.windows -= 1;
      return Promise.resolve({ title: null, icon: null });
    }
    return new Promise((resolve) => {
      let icon: string | null = null;
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        this.deps.timers.clearTimeout(timer);
        let title: string | null = null;
        try {
          if (!win.isDestroyed()) title = tidy(win.webContents.getTitle());
        } catch {
          title = null;
        }
        // A page without a <title> reports its URL as the title: that is no title.
        if (title !== null && (title === url.toString() || title === url.host)) title = null;
        try {
          if (!win.isDestroyed()) win.destroy();
        } catch {
          // Already gone.
        }
        this.windows -= 1;
        resolve({ title, icon });
      };
      const timer = this.deps.timers.setTimeout(finish, LINK_WINDOW_TIMEOUT_MS);
      try {
        win.webContents.setAudioMuted(true);
        win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      } catch {
        // Best effort: the window is destroyed within seconds either way.
      }
      win.webContents.on("page-favicon-updated", ((_event: unknown, favicons: unknown) => {
        if (Array.isArray(favicons) && typeof favicons[0] === "string") icon = favicons[0];
      }) as (...args: never[]) => void);
      win.webContents.on("did-finish-load", (() => finish()) as (...args: never[]) => void);
      win.webContents.on("did-fail-load", (() => finish()) as (...args: never[]) => void);
      win.loadURL(url.toString()).catch(() => finish());
    });
  }
}
