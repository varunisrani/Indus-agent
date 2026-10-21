// The preload bridge, when there is one (a plain browser preview or a unit test has none).

import type { IndusDesktopBridge, IndusLaunchFacts, IndusWindowKind } from "../global.d.ts";

export function desktopBridge(): IndusDesktopBridge | null {
  const host = globalThis as { indusDesktop?: IndusDesktopBridge };
  return host.indusDesktop ?? null;
}

export const DEFAULT_FACTS: IndusLaunchFacts = {
  platform: "unknown",
  appVersion: "0.0.0",
  packaged: false,
  dev: true,
  windowKind: "main",
  watch: false,
  profile: null,
  homeDir: "",
  titleBarHeight: 0,
  deepLinkScheme: "indus",
};

export function launchFacts(): IndusLaunchFacts {
  return desktopBridge()?.facts ?? DEFAULT_FACTS;
}

export interface WindowQuery {
  readonly kind: IndusWindowKind;
  readonly watch: boolean;
  readonly profile: string | null;
}

const KINDS: readonly IndusWindowKind[] = ["secondary", "instance", "hud", "quick", "pet", "wake"];

/** The window kind and its options from the query string (`?win=secondary&watch=1&profile=x`). */
export function readWindowQuery(search: string): WindowQuery {
  const params = new URLSearchParams(search);
  const raw = params.get("win");
  const kind = KINDS.find((k) => k === raw) ?? "main";
  const profile = (params.get("profile") ?? "").trim();
  return { kind, watch: params.get("watch") === "1", profile: profile === "" ? null : profile };
}

/** Windows that get the full shell; the others have their own small roots (P20-35). */
export function hasShell(kind: IndusWindowKind): boolean {
  return kind === "main" || kind === "secondary" || kind === "instance";
}
