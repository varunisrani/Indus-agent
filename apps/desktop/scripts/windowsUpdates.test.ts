// Windows update signature check (W50-e): the NSIS feed carries the channel's configured
// publisher, verification stays on, and a signed build is refused while the publisher is the
// owner placeholder.
import fs from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
// @ts-expect-error -- plain JS module without types
import { IDENTITY_FILE, isPlaceholder, PLACEHOLDER } from "../../../scripts/release/msix.mjs";
// @ts-expect-error -- plain JS module without types
import { checkSignedPublisher } from "./dist-win.mjs";

const require = createRequire(import.meta.url);
type Publisher = { publisher: string; placeholder: boolean; file: string };
const identity = require("./channel-identity.cjs") as {
  OWNER_PLACEHOLDER: string;
  windowsPublisher: (channel: string, file?: string) => Publisher;
};
const CONFIG_PATH = require.resolve("../electron-builder.config.cjs");
const check = checkSignedPublisher as (
  kind: string,
  channel: string,
  lookup?: (channel: string) => Publisher,
) => string | null;

type WinConfig = {
  win: { verifyUpdateCodeSignature?: boolean; signtoolOptions: { publisherName?: unknown } };
};

function loadConfig(channel: string): WinConfig {
  const saved = process.env.INDUS_DESKTOP_CHANNEL;
  process.env.INDUS_DESKTOP_CHANNEL = channel;
  try {
    delete require.cache[CONFIG_PATH];
    return require(CONFIG_PATH) as WinConfig;
  } finally {
    if (saved === undefined) delete process.env.INDUS_DESKTOP_CHANNEL;
    else process.env.INDUS_DESKTOP_CHANNEL = saved;
    delete require.cache[CONFIG_PATH];
  }
}

const dirs: string[] = [];
afterAll(() => {
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
});

function identityFile(content: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-publisher-"));
  dirs.push(dir);
  const file = path.join(dir, "identity.json");
  fs.writeFileSync(file, content);
  return file;
}

/** A distinguished name built at run time (never a literal subject in the repository). */
const subject = (cn: string) => [`CN=${cn}`, "O=Example Org", "C=ZZ"].join(", ");

describe("Windows update publisher (W50-e)", () => {
  it("reads the same file and placeholder mark as the MSIX lane", () => {
    expect(identity.OWNER_PLACEHOLDER).toBe(PLACEHOLDER);
    expect(identity.windowsPublisher("stable").file).toBe(IDENTITY_FILE);
  });

  it("puts each channel's configured publisher into the feed and keeps verification on", () => {
    const table = JSON.parse(fs.readFileSync(IDENTITY_FILE, "utf8")) as Record<
      string,
      { publisher: string }
    >;
    for (const channel of ["stable", "canary"]) {
      const config = loadConfig(channel);
      expect(config.win.verifyUpdateCodeSignature).toBe(true);
      expect(config.win.signtoolOptions.publisherName).toEqual([table[channel]?.publisher]);
    }
  });

  it("electron-builder takes that publisher for app-update.yml (never a silent skip)", async () => {
    // app-update.yml gets `publisherName` only when the signing manager computes one; with none,
    // electron-updater would skip the check. Ours never falls back to certificate discovery.
    const builderDir = path.dirname(require.resolve("electron-builder/package.json"));
    const fromBuilder = createRequire(path.join(builderDir, "package.json"));
    const { WindowsSignToolManager } = fromBuilder(
      "app-builder-lib/out/codeSign/windowsSignToolManager.js",
    ) as {
      WindowsSignToolManager: new (
        packager: unknown,
      ) => {
        computedPublisherName: { value: Promise<string[] | null> };
      };
    };
    for (const channel of ["stable", "canary"]) {
      const config = loadConfig(channel);
      const manager = new WindowsSignToolManager({ platformSpecificBuildOptions: config.win });
      await expect(manager.computedPublisherName.value).resolves.toEqual([
        identity.windowsPublisher(channel).publisher,
      ]);
    }
  });

  it("flags the committed publisher exactly as the MSIX lane does", () => {
    for (const channel of ["stable", "canary"]) {
      const found = identity.windowsPublisher(channel);
      expect(found.placeholder).toBe(isPlaceholder(found.publisher));
    }
  });

  it("reads a filled-in subject and flags a placeholder", () => {
    const filled = subject("Indus Test Signer");
    const file = identityFile(
      JSON.stringify({
        stable: { publisher: filled },
        canary: { publisher: `CN=${PLACEHOLDER} subject` },
      }),
    );
    expect(identity.windowsPublisher("stable", file)).toEqual({
      publisher: filled,
      placeholder: false,
      file,
    });
    expect(identity.windowsPublisher("canary", file).placeholder).toBe(true);
  });

  it("fails without a publisher for the channel, or on an unreadable file, naming the file", () => {
    const empty = identityFile(JSON.stringify({ stable: { publisher: "  " } }));
    expect(() => identity.windowsPublisher("stable", empty)).toThrow(/stable\.publisher/);
    expect(() => identity.windowsPublisher("canary", empty)).toThrow(/canary\.publisher/);
    const broken = identityFile("{ not json");
    expect(() => identity.windowsPublisher("stable", broken)).toThrow(broken);
    expect(() => identity.windowsPublisher("nightly", empty)).toThrow(/stable, canary/);
  });

  it("refuses a signed build while the channel's publisher is a placeholder", () => {
    const placeholder = (channel: string) => ({
      publisher: `CN=${PLACEHOLDER}`,
      placeholder: true,
      file: `/x/identity.json#${channel}`,
    });
    for (const kind of ["pfx", "azure"]) {
      expect(() => check(kind, "stable", placeholder)).toThrow(
        /signed Windows build needs the certificate subject as stable\.publisher/,
      );
    }
    // an unlabelled build is canary, as everywhere else
    expect(() => check("pfx", "", placeholder)).toThrow(/canary\.publisher/);
    // the committed file: refused exactly while it holds the placeholder
    if (identity.windowsPublisher("stable").placeholder) {
      expect(() => check("pfx", "stable")).toThrow(/owner placeholder/);
    }
  });

  it("lets a signed build with a filled-in publisher and every unsigned build go on", () => {
    const filled = subject("Indus Test Signer");
    const lookup = () => ({ publisher: filled, placeholder: false, file: "/x/identity.json" });
    expect(check("pfx", "canary", lookup)).toBe(filled);
    expect(check("none", "stable")).toBeNull();
  });
});
