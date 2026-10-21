// The release lookup behind "check for updates" (P20-37). It asks the releases API (GitHub's
// shape: `[{ tag_name, draft, prerelease, html_url }]`) for the newest release of a channel:
// stable takes the newest `vX.Y.Z`; canary the newest of `vX.Y.Z-canary.<stamp>` and `vX.Y.Z`
// (a canary user is never stranded below a newer stable). Drafts and other tags are ignored.
//
// Token: the environment (`INDUS_DESKTOP_UPDATE_TOKEN`, then the vendor names `GH_TOKEN`,
// `GITHUB_TOKEN`), else the GitHub CLI's login (`gh auth token`), else none. A token the API
// rejects (401) is logged once by its source — never its value — and the lookup runs again
// without it. A token is only sent over https (or to a loopback test feed), and only to the
// configured API.

import { execFile } from "node:child_process";
import type { Timers } from "../ambient/hud.ts";
import { compareVersions, parseReleaseTag, plainVersion } from "./version.ts";

export const DEFAULT_RELEASES_API = "https://api.github.com/repos/varunisrani/indus-agent/releases";
export const FEED_TIMEOUT_MS = 15_000;
export const GH_CLI_TIMEOUT_MS = 3_000;
export const TOKEN_ENV_NAMES = ["INDUS_DESKTOP_UPDATE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"] as const;

export type UpdateChannel = "stable" | "canary";
export type TokenSource = (typeof TOKEN_ENV_NAMES)[number] | "gh-cli";

export interface ReleaseInfo {
  version: string;
  tag: string;
  url: string | null;
  canary: boolean;
}

export class FeedError extends Error {}

/** The newest release of `channel` among the API's answer, or null. */
export function pickRelease(releases: unknown, channel: UpdateChannel): ReleaseInfo | null {
  if (!Array.isArray(releases)) throw new FeedError("the releases feed answered something else");
  let best: ReleaseInfo | null = null;
  for (const entry of releases) {
    if (entry === null || typeof entry !== "object") continue;
    const record = entry as Record<string, unknown>;
    if (record.draft === true || typeof record.tag_name !== "string") continue;
    const parsed = parseReleaseTag(record.tag_name);
    if (parsed === null) continue;
    if (parsed.canary && channel === "stable") continue;
    const info: ReleaseInfo = {
      version: plainVersion(record.tag_name),
      tag: record.tag_name,
      url:
        typeof record.html_url === "string" && /^https:\/\//.test(record.html_url)
          ? record.html_url
          : null,
      canary: parsed.canary,
    };
    if (best === null || (compareVersions(info.version, best.version) ?? 0) > 0) best = info;
  }
  return best;
}

/** May a token go to `apiUrl`? https anywhere, plain http only to a loopback test feed. */
export function tokenAllowed(apiUrl: string): boolean {
  try {
    const url = new URL(apiUrl);
    if (url.protocol === "https:") return true;
    return url.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
  } catch {
    return false;
  }
}

/** `gh auth token` through the CLI (no shell, bounded), or null. */
export function ghCliToken(timeoutMs: number = GH_CLI_TIMEOUT_MS): Promise<string | null> {
  return new Promise((resolve) => {
    try {
      execFile(
        "gh",
        ["auth", "token"],
        { timeout: timeoutMs, windowsHide: true, encoding: "utf8" },
        (error, stdout) => {
          const token = error === null ? String(stdout).trim() : "";
          resolve(token === "" || /\s/.test(token) ? null : token);
        },
      );
    } catch {
      resolve(null);
    }
  });
}

type FetchLike = (
  url: string,
  init: { headers: Record<string, string>; signal: AbortSignal },
) => Promise<Response>;

export interface ReleaseFeedDeps {
  apiUrl: string;
  env: Readonly<Record<string, string | undefined>>;
  /** The GitHub CLI's token (or null); called at most once per process. */
  cliToken(): Promise<string | null>;
  fetch: FetchLike;
  timers: Timers;
  log(line: string): void;
}

export class ReleaseFeed {
  private readonly deps: ReleaseFeedDeps;
  private readonly rejected = new Set<TokenSource>();
  private cli: Promise<string | null> | null = null;

  constructor(deps: ReleaseFeedDeps) {
    this.deps = deps;
  }

  /** The newest release of `channel`, or null when the feed lists none. */
  async latest(channel: UpdateChannel): Promise<ReleaseInfo | null> {
    const credential = tokenAllowed(this.deps.apiUrl) ? await this.token() : null;
    let response = await this.get(credential?.token ?? null);
    if (credential !== null && response.status === 401) {
      this.rejected.add(credential.source);
      this.deps.log(
        `[updates] the token from ${credential.source} was rejected; checking without it`,
      );
      await response.body?.cancel().catch(() => {});
      response = await this.get(null);
    }
    if (!response.ok) {
      await response.body?.cancel().catch(() => {});
      throw new FeedError(`the releases feed answered ${response.status}`);
    }
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      throw new FeedError("the releases feed answered something that is not JSON");
    }
    return pickRelease(body, channel);
  }

  private async token(): Promise<{ token: string; source: TokenSource } | null> {
    for (const name of TOKEN_ENV_NAMES) {
      const value = (this.deps.env[name] ?? "").trim();
      if (value !== "" && !this.rejected.has(name)) return { token: value, source: name };
    }
    if (this.rejected.has("gh-cli")) return null;
    if (this.cli === null) this.cli = this.deps.cliToken().catch(() => null);
    const token = await this.cli;
    return token === null ? null : { token, source: "gh-cli" };
  }

  private async get(token: string | null): Promise<Response> {
    const url = new URL(this.deps.apiUrl);
    if (!url.searchParams.has("per_page")) url.searchParams.set("per_page", "30");
    const headers: Record<string, string> = { accept: "application/vnd.github+json" };
    if (token !== null) headers.authorization = `Bearer ${token}`;
    const controller = new AbortController();
    const timer = this.deps.timers.setTimeout(() => controller.abort(), FEED_TIMEOUT_MS);
    try {
      return await this.deps.fetch(url.toString(), { headers, signal: controller.signal });
    } catch {
      throw new FeedError("the releases feed could not be reached");
    } finally {
      this.deps.timers.clearTimeout(timer);
    }
  }
}
