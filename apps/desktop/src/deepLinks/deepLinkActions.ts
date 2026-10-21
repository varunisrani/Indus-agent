// What a deep link may do in the window (P20-34). Main has already parsed and filtered the link;
// here each kind becomes one planned action, checked again: `open` only names an app route (the
// route table decides), `skill` and `mcp` always wait for the person's confirmation and an `mcp`
// link may add only an https (or loopback http) server — a link never starts a program.

import { classifyPath } from "../app/routes.ts";
import type { MessageKey } from "../i18n/index.tsx";

export interface LinkPayload {
  readonly kind: "open" | "skill" | "mcp" | "close-preview";
  readonly name: string;
  readonly params: Readonly<Record<string, string>>;
}

export type LinkAction =
  | { readonly kind: "navigate"; readonly path: string; readonly overlay: boolean }
  | { readonly kind: "confirm-skill"; readonly identifier: string }
  | {
      readonly kind: "confirm-mcp";
      readonly name: string;
      readonly url: string;
      readonly auth: "none" | "oauth";
    }
  | { readonly kind: "ignore" }
  | { readonly kind: "refused"; readonly reason: MessageKey };

/** Query keys an `open` link may carry into the settings overlay. */
const SETTINGS_KEYS = ["view", "page", "field", "row"] as const;
const MCP_NAME = /^[A-Za-z0-9._-]{1,64}$/;
const SKILL_ID = /^[A-Za-z0-9@._:/-]{1,512}$/;
const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]"]);

/** An http(s) server URL a link may add: https anywhere, http on loopback only, no credentials. */
export function allowedServerUrl(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.username !== "" || url.password !== "") return null;
  if (url.protocol === "https:") return url.toString();
  if (url.protocol === "http:" && LOOPBACK.has(url.hostname)) return url.toString();
  return null;
}

export function planLink(link: LinkPayload): LinkAction {
  switch (link.kind) {
    case "close-preview":
      return { kind: "ignore" }; // main handled it
    case "open": {
      const name = link.name.replace(/^\/+/, "");
      if (/[?#\\]/.test(name)) return { kind: "refused", reason: "deepLink.refused.path" };
      const path = `/${name}`;
      const view = classifyPath(path);
      if (view.kind === "unknown") return { kind: "refused", reason: "deepLink.refused.path" };
      if (view.kind === "overlay" && view.overlay === "settings") {
        const query = new URLSearchParams();
        for (const key of SETTINGS_KEYS) {
          const value = link.params[key];
          if (value !== undefined && value !== "") query.set(key, value);
        }
        const text = query.toString();
        return { kind: "navigate", path: text === "" ? path : `${path}?${text}`, overlay: true };
      }
      return { kind: "navigate", path, overlay: view.kind === "overlay" };
    }
    case "skill": {
      const identifier = link.name.trim();
      if (!SKILL_ID.test(identifier) || identifier.includes(".."))
        return { kind: "refused", reason: "deepLink.refused.skill" };
      return { kind: "confirm-skill", identifier };
    }
    case "mcp": {
      if (!MCP_NAME.test(link.name)) return { kind: "refused", reason: "deepLink.refused.mcpName" };
      if (link.params.command !== undefined || link.params.args !== undefined)
        return { kind: "refused", reason: "deepLink.refused.mcpCommand" };
      const url = allowedServerUrl(link.params.url ?? "");
      if (url === null) return { kind: "refused", reason: "deepLink.refused.mcpUrl" };
      return {
        kind: "confirm-mcp",
        name: link.name,
        url,
        auth: link.params.auth === "oauth" ? "oauth" : "none",
      };
    }
  }
}
