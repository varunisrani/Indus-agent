// Who a desktop build is, per release channel (P23-16). A canary must install and run beside the
// stable app: its own app id (bundle id, Windows install GUID), product name (install folder and
// Electron's userData folder, which Electron derives from the packaged productName), Linux
// executable and .deb package, and its own update feed channel on GitHub Releases. The running app
// takes the same names from its build stamp's channel (electron/identity.ts, held equal by a test),
// and the Windows update check its publisher from packaging/msix/identity.json (windowsPublisher).
//
// The channel comes from INDUS_DESKTOP_CHANNEL (the release lanes pass admit's `kind`;
// dist-unsigned.mjs passes the build stamp's channel). Without it a build is canary, the same
// default as the stamp: an unlabelled build never takes the stable identity.
// @ts-check
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const BASE_APP_ID = "ai.indus.desktop";
const BASE_PRODUCT = "Indus";
const BASE_EXECUTABLE = "indus-desktop";
const CHANNELS = Object.freeze(["stable", "canary"]);
const DEFAULT_CHANNEL = "canary";

/** @param {string | undefined} raw */
function resolveChannel(raw) {
  const value = (raw ?? "").trim();
  if (value === "") return DEFAULT_CHANNEL;
  if (!CHANNELS.includes(value)) {
    throw new Error(`INDUS_DESKTOP_CHANNEL must be one of ${CHANNELS.join(", ")}, got ${value}`);
  }
  return value;
}

/**
 * The identity of a build on `channel`.
 * @param {string} channel
 */
function channelIdentity(channel) {
  const resolved = resolveChannel(channel);
  const canary = resolved === "canary";
  return Object.freeze({
    channel: resolved,
    appId: canary ? `${BASE_APP_ID}.canary` : BASE_APP_ID,
    productName: canary ? `${BASE_PRODUCT} Canary` : BASE_PRODUCT,
    executableName: canary ? `${BASE_EXECUTABLE}-canary` : BASE_EXECUTABLE,
    // electron-updater's file prefix: latest.yml / latest-linux.yml, canary.yml / canary-linux.yml
    feedChannel: canary ? "canary" : "latest",
  });
}

/**
 * `owner/repo` of RELEASE_REPOSITORY, read from the desktop's one copy (electron/releaseBase.ts,
 * held equal to src/indus/foundation/versions.py by tests/unit/packaging/test_release_base.py).
 * @param {string} [file]
 */
function releaseRepository(file = path.join(__dirname, "..", "electron", "releaseBase.ts")) {
  const text = fs.readFileSync(file, "utf8");
  const match = /export const RELEASE_REPOSITORY = "([\w.-]+)\/([\w.-]+)";/.exec(text);
  if (match === null) throw new Error(`no RELEASE_REPOSITORY in ${file}`);
  return { owner: String(match[1]), repo: String(match[2]) };
}

/** The mark an owner placeholder carries in packaging/msix/identity.json (msix.mjs reads the same). */
const OWNER_PLACEHOLDER = "OWNER-TO-FILL";
const PUBLISHER_FILE = path.join(__dirname, "..", "packaging", "msix", "identity.json");

/**
 * The Windows code-signing publisher of `channel`: the certificate subject recorded once, for the
 * MSIX lane and the NSIS update check alike, in packaging/msix/identity.json. `placeholder` is
 * true while the owner has not filled it in yet.
 * @param {string} channel
 * @param {string} [file]
 * @returns {{ publisher: string, placeholder: boolean, file: string }}
 */
function windowsPublisher(channel, file = PUBLISHER_FILE) {
  const resolved = resolveChannel(channel);
  /** @type {unknown} */
  let table;
  try {
    table = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (error) {
    const why = error instanceof Error ? error.message : String(error);
    throw new Error(`cannot read the Windows publisher from ${file}: ${why}`);
  }
  const entry =
    table !== null && typeof table === "object"
      ? /** @type {Record<string, unknown>} */ (table)[resolved]
      : undefined;
  const publisher =
    entry !== null && typeof entry === "object"
      ? /** @type {Record<string, unknown>} */ (entry).publisher
      : undefined;
  if (typeof publisher !== "string" || publisher.trim() === "") {
    throw new Error(`${file} has no ${resolved}.publisher (the signing certificate's subject)`);
  }
  return { publisher, placeholder: publisher.includes(OWNER_PLACEHOLDER), file };
}

module.exports = {
  BASE_APP_ID,
  CHANNELS,
  DEFAULT_CHANNEL,
  OWNER_PLACEHOLDER,
  channelIdentity,
  releaseRepository,
  resolveChannel,
  windowsPublisher,
};
