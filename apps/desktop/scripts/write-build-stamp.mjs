// The build stamp (P20-38): `desktop-build-stamp.json`, shipped beside the app archive in the
// packaged resources folder. It declares what this build is and who updates it; the runtime reads
// it and never infers the update owner from anything else (P20-37).
//
//   node scripts/write-build-stamp.mjs --out FILE [--channel stable|canary] [--platform P]
//        [--version V] [--commit SHA] [--update-owner electron-updater|manual]
//
// Fallbacks: INDUS_DESKTOP_CHANNEL; INDUS_DESKTOP_VERSION, else `__version__` in
// src/indus/__init__.py (the one version source since P23-01 made pyproject's version dynamic);
// INDUS_DESKTOP_COMMIT (the release workflow's admitted commit), else `git rev-parse HEAD`, else
// "unknown"; SOURCE_DATE_EPOCH for the build time. GITHUB_SHA is never read: in a release run it is
// the default branch's tip, not the commit being built (R10.24). When INDUS_DESKTOP_COMMIT is set
// and the checkout is another commit, the stamp is refused.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const STAMP_FILE = "desktop-build-stamp.json";
export const STAMP_SCHEMA = 1;
export const CHANNELS = Object.freeze(["stable", "canary"]);
export const UPDATE_OWNERS = Object.freeze(["electron-updater", "manual"]);
/** A build without an explicit channel is a test build, never stable by accident. */
export const DEFAULT_CHANNEL = "canary";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const REPO_ROOT = path.resolve(APP_DIR, "..", "..");

const SEMVER =
  /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/;
const COMMIT = /^(?:[0-9a-f]{7,64}|unknown)$/;

/**
 * Who updates a build on `platform`: electron-updater for macOS (ZIP) and Linux (AppImage); the
 * unsigned Windows build is updated by hand (signed NSIS feeds arrive with P23).
 */
export function updateOwnerFor(platform) {
  if (platform === "darwin" || platform === "linux") return "electron-updater";
  return "manual";
}

/** The `__version__ = "…"` of a Python module text (src/indus/__init__.py), or null. */
export function packageVersion(text) {
  for (const raw of text.split(/\r?\n/)) {
    const match = /^__version__\s*=\s*(["'])([^"']+)\1\s*(?:#.*)?$/.exec(raw.trim());
    if (match) return match[2] ?? null;
  }
  return null;
}

function gitCommit(cwd) {
  try {
    const out = spawnSync("git", ["rev-parse", "HEAD"], {
      cwd,
      encoding: "utf8",
      timeout: 5_000,
      windowsHide: true,
    });
    const sha = (out.stdout ?? "").trim().toLowerCase();
    return out.status === 0 && COMMIT.test(sha) ? sha : null;
  } catch {
    return null;
  }
}

/** Two hex SHAs name the same commit when one is a prefix of the other. */
function sameCommit(a, b) {
  return a.startsWith(b) || b.startsWith(a);
}

function fail(message) {
  throw new Error(`build stamp: ${message}`);
}

/** Validate a stamp object; returns it unchanged or throws naming the bad field. */
export function validateBuildStamp(stamp) {
  if (stamp === null || typeof stamp !== "object") fail("not an object");
  if (stamp.schema !== STAMP_SCHEMA) fail(`schema must be ${STAMP_SCHEMA}`);
  if (typeof stamp.version !== "string" || !SEMVER.test(stamp.version))
    fail(`version ${JSON.stringify(stamp.version)} is not SemVer`);
  if (typeof stamp.commit !== "string" || !COMMIT.test(stamp.commit))
    fail(`commit ${JSON.stringify(stamp.commit)} is not a hex SHA`);
  if (!CHANNELS.includes(stamp.channel))
    fail(`channel ${JSON.stringify(stamp.channel)} is not one of ${CHANNELS.join(", ")}`);
  if (!UPDATE_OWNERS.includes(stamp.updateOwner))
    fail(
      `updateOwner ${JSON.stringify(stamp.updateOwner)} is not one of ${UPDATE_OWNERS.join(", ")}`,
    );
  if (typeof stamp.platform !== "string" || stamp.platform === "") fail("platform missing");
  if (typeof stamp.builtAt !== "string" || Number.isNaN(Date.parse(stamp.builtAt)))
    fail("builtAt is not an ISO time");
  return stamp;
}

/**
 * Build the stamp from explicit options, then the environment, then the checkout.
 * `env`, `readText` and `commitOf` are injectable for tests.
 */
export function makeBuildStamp(options = {}, deps = {}) {
  const env = deps.env ?? process.env;
  const readText = deps.readText ?? ((file) => fs.readFileSync(file, "utf8"));
  const commitOf = deps.commitOf ?? gitCommit;
  const platform = options.platform ?? process.platform;

  let version = options.version ?? (env.INDUS_DESKTOP_VERSION || null);
  if (version === null) {
    try {
      version = packageVersion(readText(path.join(REPO_ROOT, "src", "indus", "__init__.py")));
    } catch {
      version = null;
    }
  }
  if (version === null) fail("no version (pass --version or set INDUS_DESKTOP_VERSION)");

  const admitted = (env.INDUS_DESKTOP_COMMIT || "").trim().toLowerCase() || null;
  let commit = options.commit?.toLowerCase() ?? null;
  if (commit === null) {
    const checkout = commitOf(REPO_ROOT)?.toLowerCase() ?? null;
    if (admitted !== null && checkout !== null && !sameCommit(admitted, checkout))
      fail(`the checkout is ${checkout}, not the admitted commit ${admitted}`);
    commit = admitted ?? checkout ?? "unknown";
  }

  const epoch = env.SOURCE_DATE_EPOCH;
  const builtAt =
    options.builtAt ??
    (epoch && /^\d+$/.test(epoch)
      ? new Date(Number(epoch) * 1000).toISOString()
      : new Date().toISOString());

  return validateBuildStamp({
    schema: STAMP_SCHEMA,
    version,
    commit,
    channel: options.channel ?? (env.INDUS_DESKTOP_CHANNEL || DEFAULT_CHANNEL),
    updateOwner: options.updateOwner ?? updateOwnerFor(platform),
    platform,
    builtAt,
  });
}

/** Write the stamp as pretty JSON (creating the folder); returns the stamp. */
export function writeBuildStamp(file, options = {}, deps = {}) {
  const stamp = makeBuildStamp(options, deps);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(stamp, null, 2)}\n`, "utf8");
  return stamp;
}

const FLAGS = {
  "--out": "out",
  "--channel": "channel",
  "--platform": "platform",
  "--version": "version",
  "--commit": "commit",
  "--update-owner": "updateOwner",
};

/** `--flag value` pairs → options; throws on an unknown flag or a missing value. */
export function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = FLAGS[argv[i]];
    const value = argv[i + 1];
    if (key === undefined) throw new Error(`unknown argument ${argv[i]}`);
    if (value === undefined || value.startsWith("--")) throw new Error(`${argv[i]} needs a value`);
    out[key] = value;
  }
  return out;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const { out, ...options } = parseArgs(process.argv.slice(2));
    if (out === undefined) throw new Error("--out FILE is required");
    const stamp = writeBuildStamp(path.resolve(out), options);
    process.stdout.write(`${JSON.stringify(stamp)}\n`);
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exit(2);
  }
}
