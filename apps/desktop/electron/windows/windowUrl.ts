// Renderer URLs per window kind. The renderer uses a HashRouter, so the window kind and its
// options travel in the query string *before* the hash, and the route after it:
//
//   main       <base>/
//   secondary  <base>/?win=secondary[&watch=1][&profile=NAME]#/<sessionId>
//   instance   <base>/?win=instance[&profile=NAME]
//   hud        <base>/?win=hud[&profile=NAME][#/<sessionId>]
//   quick      <base>/?win=quick
//   pet        <base>/?win=pet          (P24-19)
//   wake       <base>/?win=wake         (P24-20)

import { type AppOrigin, appEntryUrl } from "../appOrigin.ts";
import type { WindowKind } from "./factory.ts";

export interface WindowUrlOptions {
  sessionId?: string;
  watch?: boolean;
  profile?: string | null;
}

export function windowQuery(kind: WindowKind, options: WindowUrlOptions = {}): string {
  if (kind === "main" || kind === "hidden") return "";
  const params = new URLSearchParams({ win: kind });
  if (kind === "secondary" && options.watch === true) params.set("watch", "1");
  const profile = (options.profile ?? "").trim();
  if (profile !== "" && kind !== "quick" && kind !== "pet" && kind !== "wake")
    params.set("profile", profile);
  return `?${params.toString()}`;
}

export function windowUrl(
  origin: AppOrigin,
  kind: WindowKind,
  options: WindowUrlOptions = {},
): string {
  const hash =
    (kind === "secondary" || kind === "hud") && options.sessionId
      ? `#/${encodeURIComponent(options.sessionId)}`
      : "";
  return appEntryUrl(origin, windowQuery(kind, options), hash);
}

/** Read the kind flags back from a URL (main reports them as launch facts). */
export function parseWindowQuery(url: string): {
  kind: WindowKind;
  watch: boolean;
  profile: string | null;
} {
  let search = "";
  try {
    search = new URL(url).search;
  } catch {
    search = "";
  }
  const params = new URLSearchParams(search);
  const raw = params.get("win");
  const kinds: WindowKind[] = ["secondary", "instance", "hud", "quick", "pet", "wake"];
  const kind = kinds.find((k) => k === raw) ?? "main";
  const profile = (params.get("profile") ?? "").trim();
  return { kind, watch: params.get("watch") === "1", profile: profile === "" ? null : profile };
}
