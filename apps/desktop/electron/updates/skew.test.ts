import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import {
  appBehindRuntime,
  BUILD_STAMP_FILE,
  mixedGenerations,
  readStamp,
  sanitizeStamp,
  stampLocations,
} from "./skew.ts";
import { compareVersions, isNewer, parseReleaseTag, parseVersion } from "./version.ts";

describe("versions", () => {
  it("orders stable, canary and source suffixes", () => {
    expect(compareVersions("1.2.3", "1.2.3")).toBe(0);
    expect(isNewer("1.2.4", "1.2.3")).toBe(true);
    expect(isNewer("1.10.0", "1.9.9")).toBe(true);
    // a canary sorts above the stable before it and below its own stable
    expect(isNewer("1.2.4-canary.20261001T000000Z", "1.2.3")).toBe(true);
    expect(isNewer("1.2.4", "1.2.4-canary.20261001T000000Z")).toBe(true);
    expect(isNewer("1.2.4-canary.20261002T000000Z", "1.2.4-canary.20261001T000000Z")).toBe(true);
    expect(isNewer("0.1.0", "0.1.0.dev3")).toBe(true);
    expect(compareVersions("x", "1.0.0")).toBeNull();
    expect(isNewer("garbage", "1.0.0")).toBe(false);
    expect(parseVersion("v2.0.1")).toEqual({ major: 2, minor: 0, patch: 1, pre: null });
  });

  it("release tags: only ours", () => {
    expect(parseReleaseTag("v1.0.0")?.canary).toBe(false);
    expect(parseReleaseTag("v1.0.1-canary.20261021T101010Z")?.canary).toBe(true);
    expect(parseReleaseTag("v1.0.1-nightly.1")).toBeNull();
    expect(parseReleaseTag("v1.0.1-canary.yesterday")).toBeNull();
  });
});

describe("build stamp and generation skew", () => {
  it("a stamp names its owner; anything else is no stamp", () => {
    expect(
      sanitizeStamp({
        version: "1.0.0",
        commit: "abc1234",
        channel: "canary",
        updateOwner: "manual",
      }),
    ).toEqual({ version: "1.0.0", commit: "abc1234", channel: "canary", updateOwner: "manual" });
    expect(sanitizeStamp({ version: "1.0.0", update_owner: "electron-updater" })).toMatchObject({
      channel: "stable",
      commit: null,
    });
    expect(sanitizeStamp({ version: "1.0.0", updateOwner: "store" })).toBeNull();
    expect(sanitizeStamp({ version: "one", updateOwner: "manual" })).toBeNull();
    expect(sanitizeStamp(null)).toBeNull();
  });

  it("finds the asar and unpacked copies", () => {
    expect(stampLocations("/opt/Indus/resources/app.asar")).toEqual({
      inside: path.join("/opt/Indus/resources/app.asar", BUILD_STAMP_FILE),
      unpacked: path.join("/opt/Indus/resources/app.asar.unpacked", BUILD_STAMP_FILE),
    });
    expect(stampLocations("/src/apps/desktop").unpacked).toBeNull();
  });

  it("mixed asar/unpacked generations after a broken update → offer reinstall", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "stamp-"));
    const app = path.join(dir, "app.asar");
    fs.mkdirSync(app);
    fs.mkdirSync(`${app}.unpacked`);
    const where = stampLocations(app);
    const write = (file: string, version: string, commit: string) =>
      fs.writeFileSync(file, JSON.stringify({ version, commit, updateOwner: "electron-updater" }));
    write(where.inside, "1.1.0", "bbbbbbb");
    write(where.unpacked as string, "1.0.0", "aaaaaaa");
    const skew = mixedGenerations(readStamp(where.inside), readStamp(where.unpacked as string));
    expect(skew?.kind).toBe("mixed-generations");
    write(where.unpacked as string, "1.1.0", "bbbbbbb");
    expect(
      mixedGenerations(readStamp(where.inside), readStamp(where.unpacked as string)),
    ).toBeNull();
    expect(mixedGenerations(readStamp(where.inside), null)).toBeNull();
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("an app behind the runtime it attached to → offer the app update", () => {
    expect(appBehindRuntime("1.0.0", "1.1.0")?.kind).toBe("app-behind-runtime");
    expect(appBehindRuntime("1.1.0", "1.1.0")).toBeNull();
    expect(appBehindRuntime("1.1.0", "unknown")).toBeNull();
  });
});
