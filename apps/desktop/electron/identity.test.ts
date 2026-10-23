import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";
import { identityForChannel, runtimeIdentity } from "./identity.ts";
import type { BuildStamp } from "./updates/skew.ts";

const packaging = createRequire(import.meta.url)("../scripts/channel-identity.cjs") as {
  CHANNELS: readonly string[];
  channelIdentity: (channel: string) => { appId: string; productName: string };
};

function stamp(channel: "stable" | "canary"): BuildStamp {
  return { version: "0.9.0", commit: null, channel, updateOwner: "electron-updater" };
}

describe("runtime identity (W50-e)", () => {
  it("names each channel exactly as packaging does (channel-identity.cjs)", () => {
    expect(packaging.CHANNELS).toEqual(["stable", "canary"]);
    for (const channel of packaging.CHANNELS) {
      const built = packaging.channelIdentity(channel);
      const running = identityForChannel(channel as "stable" | "canary");
      expect(running.appName).toBe(built.productName);
      expect(running.launcherId).toBe(built.appId);
    }
  });

  it("a packaged canary is Indus Canary with its own launcher id; stable keeps Indus", () => {
    expect(runtimeIdentity(stamp("canary"), true)).toEqual({
      channel: "canary",
      appName: "Indus Canary",
      launcherId: "ai.indus.desktop.canary",
    });
    expect(runtimeIdentity(stamp("stable"), true)).toEqual({
      channel: "stable",
      appName: "Indus",
      launcherId: "ai.indus.desktop",
    });
  });

  it("a packaged app without a readable stamp never takes the stable identity", () => {
    expect(runtimeIdentity(null, true).launcherId).toBe("ai.indus.desktop.canary");
  });

  it("an unpackaged (development) run keeps the stable names", () => {
    expect(runtimeIdentity(null, false).appName).toBe("Indus");
    expect(runtimeIdentity(stamp("canary"), false).appName).toBe("Indus");
  });
});
