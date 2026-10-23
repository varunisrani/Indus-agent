// The running app's own name and Linux launcher id, per release channel (W50-e).
//
// Packaging gives a canary its own app id and product name (scripts/channel-identity.cjs, P23-16);
// the app must call itself the same at run time, or a canary would write its launcher entry over
// the stable one and group its windows under the stable app. The channel comes from the build
// stamp, the one record of what a build is (P20-37). A packaged app whose stamp cannot be read is
// treated as canary, the same rule packaging follows: an unlabelled build never takes the stable
// identity. An unpackaged (development) run keeps the stable names.
//
// The two base names repeat channel-identity.cjs's; identity.test.ts holds them equal.

import type { BuildStamp } from "./updates/skew.ts";

export type DesktopChannel = "stable" | "canary";

const BASE_APP_ID = "ai.indus.desktop";
const BASE_PRODUCT = "Indus";

export interface DesktopIdentity {
  channel: DesktopChannel;
  /** `app.setName`, window titles, the app menu, the launcher entry's Name and StartupWMClass. */
  appName: string;
  /** The launcher entry's file name and icon name on Linux (the build's app id). */
  launcherId: string;
}

export function identityForChannel(channel: DesktopChannel): DesktopIdentity {
  if (channel === "canary") {
    return { channel, appName: `${BASE_PRODUCT} Canary`, launcherId: `${BASE_APP_ID}.canary` };
  }
  return { channel, appName: BASE_PRODUCT, launcherId: BASE_APP_ID };
}

/** The identity of this run: the stamp's channel when packaged, else stable for development. */
export function runtimeIdentity(stamp: BuildStamp | null, packaged: boolean): DesktopIdentity {
  if (!packaged) return identityForChannel("stable");
  return identityForChannel(stamp?.channel ?? "canary");
}
