// VS Code Marketplace access for theme import (P24-33). The renderer never fetches the network
// itself: it asks main to search colour themes (`themes:search`) or to download one extension
// package (`themes:download`); the conversion happens in the renderer (`src/theme/vscodeImport.ts`).
//
// Rules: only https, only the hosts in `ALLOWED_HOSTS` (redirects are followed by hand, at most
// `MAX_REDIRECTS`, each hop checked), no cookies or credentials, a package of at most
// `MAX_VSIX_BYTES`, and a deadline per request. Search sorts by install count; an empty query lists
// the most installed themes.

import type { MainContext } from "../context.ts";
import { IpcError } from "../ipc/envelope.ts";
import type { ResponseOf } from "../ipc/schemas.ts";

export const GALLERY = "https://marketplace.visualstudio.com/_apis/public/gallery";
export const ALLOWED_HOSTS: readonly RegExp[] = [
  /^marketplace\.visualstudio\.com$/,
  /^[a-z0-9-]+\.gallery\.vsassets\.io$/,
  /^[a-z0-9-]+\.gallerycdn\.vsassets\.io$/,
];
export const MAX_VSIX_BYTES = 5 * 1024 * 1024;
export const MAX_REDIRECTS = 3;
export const REQUEST_TIMEOUT_MS = 20_000;
export const PAGE_SIZE = 30;

/** The Marketplace query API's numbers (filter types, sort, flags). */
const FILTER_TARGET = 8;
const FILTER_CATEGORY = 5;
const FILTER_TEXT = 10;
const SORT_INSTALLS = 4;
const FLAGS = 0x1 | 0x2 | 0x10 | 0x80 | 0x100;

export type MarketplaceItem = ResponseOf<"indus:themes:search">["items"][number];

export interface FetchResponse {
  status: number;
  headers: { get(name: string): string | null };
  arrayBuffer(): Promise<ArrayBuffer>;
  json(): Promise<unknown>;
}

export type Fetcher = (
  url: string,
  init: {
    method: string;
    headers: Record<string, string>;
    body?: string;
    redirect: "manual";
    signal: AbortSignal;
    credentials: "omit";
  },
) => Promise<FetchResponse>;

export function allowedUrl(raw: string): boolean {
  try {
    const url = new URL(raw);
    return (
      url.protocol === "https:" &&
      url.username === "" &&
      url.password === "" &&
      ALLOWED_HOSTS.some((host) => host.test(url.hostname))
    );
  } catch {
    return false;
  }
}

/** The search request body for `query` (empty: the most installed themes). */
export function searchBody(query: string): string {
  const criteria: { filterType: number; value: string }[] = [
    { filterType: FILTER_TARGET, value: "Microsoft.VisualStudio.Code" },
    { filterType: FILTER_CATEGORY, value: "Themes" },
  ];
  const text = query.trim();
  if (text !== "") criteria.push({ filterType: FILTER_TEXT, value: text });
  return JSON.stringify({
    filters: [
      { criteria, pageNumber: 1, pageSize: PAGE_SIZE, sortBy: SORT_INSTALLS, sortOrder: 0 },
    ],
    flags: FLAGS,
  });
}

const str = (value: unknown): string => (typeof value === "string" ? value : "");

/** The items of a search answer; anything malformed is skipped. */
export function parseSearch(body: unknown): MarketplaceItem[] {
  const results = (body as { results?: unknown } | null)?.results;
  const first = Array.isArray(results) ? (results[0] as { extensions?: unknown }) : undefined;
  const extensions = Array.isArray(first?.extensions) ? first.extensions : [];
  const items: MarketplaceItem[] = [];
  for (const raw of extensions) {
    const ext = raw as Record<string, unknown>;
    const publisher = str((ext.publisher as Record<string, unknown> | undefined)?.publisherName);
    const extension = str(ext.extensionName);
    const versions = Array.isArray(ext.versions) ? ext.versions : [];
    const version = str((versions[0] as Record<string, unknown> | undefined)?.version);
    if (publisher === "" || extension === "" || version === "") continue;
    const stats = Array.isArray(ext.statistics) ? ext.statistics : [];
    const install = stats.find((s) => (s as Record<string, unknown>).statisticName === "install") as
      | Record<string, unknown>
      | undefined;
    items.push({
      publisher,
      extension,
      version,
      displayName: str(ext.displayName) || extension,
      description: str(ext.shortDescription),
      installs: typeof install?.value === "number" ? install.value : 0,
    });
  }
  return items;
}

export function packageUrl(publisher: string, extension: string, version: string): string {
  const part = encodeURIComponent;
  return `${GALLERY}/publishers/${part(publisher)}/vsextensions/${part(extension)}/${part(version)}/vspackage`;
}

export class MarketplaceClient {
  private readonly fetcher: Fetcher;

  constructor(fetcher: Fetcher) {
    this.fetcher = fetcher;
  }

  private async request(
    url: string,
    init: { method: string; headers: Record<string, string>; body?: string },
  ): Promise<FetchResponse> {
    let target = url;
    for (let hop = 0; hop <= MAX_REDIRECTS; hop++) {
      if (!allowedUrl(target)) throw new IpcError("refused", "that address is not allowed");
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
      let answer: FetchResponse;
      try {
        answer = await this.fetcher(target, {
          ...init,
          redirect: "manual",
          signal: controller.signal,
          credentials: "omit",
        });
      } catch {
        throw new IpcError("unreachable", "the Marketplace did not answer");
      } finally {
        clearTimeout(timer);
      }
      if (answer.status >= 300 && answer.status < 400) {
        const next = answer.headers.get("location");
        if (next === null) throw new IpcError("unreachable", "a redirect without a location");
        target = new URL(next, target).toString();
        continue;
      }
      if (answer.status !== 200) {
        throw new IpcError("unreachable", `the Marketplace answered ${answer.status}`);
      }
      return answer;
    }
    throw new IpcError("unreachable", "too many redirects");
  }

  async search(query: string): Promise<MarketplaceItem[]> {
    const answer = await this.request(`${GALLERY}/extensionquery`, {
      method: "POST",
      headers: {
        accept: "application/json;api-version=3.0-preview.1",
        "content-type": "application/json",
      },
      body: searchBody(query),
    });
    return parseSearch(await answer.json());
  }

  async download(publisher: string, extension: string, version: string): Promise<string> {
    const answer = await this.request(packageUrl(publisher, extension, version), {
      method: "GET",
      headers: { accept: "application/octet-stream" },
    });
    const declared = Number(answer.headers.get("content-length") ?? "0");
    if (declared > MAX_VSIX_BYTES) throw new IpcError("too-large", "the package is over 5 MiB");
    const bytes = new Uint8Array(await answer.arrayBuffer());
    if (bytes.byteLength > MAX_VSIX_BYTES) {
      throw new IpcError("too-large", "the package is over 5 MiB");
    }
    return Buffer.from(bytes).toString("base64");
  }
}

/** Main's install step for the two Marketplace channels. */
export function installThemeMarketplace(
  context: MainContext,
  options: { fetch: Fetcher },
): MarketplaceClient {
  const client = new MarketplaceClient(options.fetch);
  const appWindow = (senderId: number) => {
    const kind = context.windows.kindOf(senderId);
    if (kind !== "main" && kind !== "secondary" && kind !== "instance") {
      throw new IpcError("sender-rejected", "only an app window may import themes");
    }
  };
  context.registry.handle("indus:themes:search", async ({ query }, { senderId }) => {
    appWindow(senderId);
    return { items: await client.search(query) };
  });
  context.registry.handle(
    "indus:themes:download",
    async ({ publisher, extension, version }, { senderId }) => {
      appWindow(senderId);
      return { vsixBase64: await client.download(publisher, extension, version) };
    },
  );
  return client;
}
