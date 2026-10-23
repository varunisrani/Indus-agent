// The macOS signer (P23-15), electron-builder's `mac.sign` hook. It only runs when electron-builder
// found our Developer ID, which happens only through dist-mac.mjs with the release secrets set.
//
//   1. every nested piece of code — Mach-O files (native modules, helpers, dylibs) and nested
//      bundles (.app, .framework, .xpc, .appex) — deepest first, so a bundle is always sealed after
//      what it contains; hardened runtime, secure timestamp, the shared entitlements;
//   2. the digests the outer signature seals: each archive's `ElectronAsarIntegrity` hash in
//      Info.plist, recomputed from the archive header on disk and rewritten only when stale;
//   3. the app bundle itself, last, then `codesign --verify --deep --strict`.
//
// Notarisation and stapling follow in the afterSign hook (notarize.mjs). The secrets are read by
// name only (macSigningPlan); their values are never printed, logged or put into an error.

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { readAsarHeaderText } from "./asar.mjs";
import { withIntegrityHash } from "./plist-text.mjs";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
/** The one entitlements file, for the app and for its nested code (W49-a-5). */
export const ENTITLEMENTS = path.join(APP_DIR, "resources", "entitlements.mac.plist");

/** The Developer ID certificate (base64 .p12) and its password. */
export const CERTIFICATE_NAMES = Object.freeze(["MAC_CERTIFICATE", "MAC_CERTIFICATE_PASSWORD"]);
/** Two ways to authenticate with Apple's notary service; the API key is preferred. */
export const NOTARY_ROUTES = Object.freeze({
  "api-key": Object.freeze(["APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER"]),
  "apple-id": Object.freeze(["APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_SPECIFIC_PASSWORD"]),
});
/** Every name the signing path reads. */
export const SIGNING_NAMES = Object.freeze([
  ...CERTIFICATE_NAMES,
  ...NOTARY_ROUTES["apple-id"],
  ...NOTARY_ROUTES["api-key"],
]);

/** A half-configured signing setup: names what is missing, never a value. */
export class SigningSetupError extends Error {}

/** GitHub hands an unset secret over as an empty string: blank counts as absent. */
function isSet(env, name) {
  const value = env[name];
  return typeof value === "string" && value.trim() !== "";
}

/**
 * What this build does about signing, from the environment's names alone:
 * `{ signed: false, line }` when none is set (an unsigned build, one line to print);
 * `{ signed: true, notary: "api-key" | "apple-id" }` when the certificate and one notary route are
 * complete. Anything in between throws SigningSetupError.
 */
export function macSigningPlan(env) {
  if (!SIGNING_NAMES.some((name) => isSet(env, name))) {
    return {
      signed: false,
      line: `[sign-mac] unsigned build: none of ${SIGNING_NAMES.join(", ")} is set`,
    };
  }
  const gaps = [];
  const certificateGap = CERTIFICATE_NAMES.filter((name) => !isSet(env, name));
  if (certificateGap.length > 0) gaps.push(`certificate needs ${certificateGap.join(", ")}`);
  const routes = Object.entries(NOTARY_ROUTES);
  const ready = routes.find(([, names]) => names.every((name) => isSet(env, name)));
  if (ready === undefined) {
    for (const [route, names] of routes) {
      gaps.push(`notarisation (${route}) needs ${names.filter((n) => !isSet(env, n)).join(", ")}`);
    }
  }
  if (gaps.length > 0) {
    throw new SigningSetupError(
      `macOS signing is only partly configured: ${gaps.join("; ")}. Set them all, or none for an unsigned build.`,
    );
  }
  return { signed: true, notary: /** @type {"api-key" | "apple-id"} */ (ready?.[0]) };
}

/** A failed tool run. The message names the step, never the argument list. */
export class ToolError extends Error {
  constructor(label, code, stdout, stderr) {
    const detail = String(stderr || stdout || "")
      .trim()
      .split("\n")
      .slice(-6)
      .join("\n");
    super(`${label} failed (exit ${code})${detail ? `:\n${detail}` : ""}`);
    this.code = code;
    this.stdout = stdout;
    this.stderr = stderr;
  }
}

/**
 * Run `command args` without a shell. Resolves `{ stdout, stderr }` on exit 0, rejects with a
 * ToolError labelled `label` otherwise.
 */
export function runTool(command, args, { label = command } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => reject(new ToolError(label, error.code ?? "spawn", "", "")));
    child.on("close", (code) => {
      if (code === 0) resolve({ stdout, stderr });
      else reject(new ToolError(label, code, stdout, stderr));
    });
  });
}

const BUNDLE_SUFFIXES = Object.freeze([".app", ".framework", ".xpc", ".appex"]);
const THIN_MAGICS = new Set([0xfeedface, 0xfeedfacf, 0xcefaedfe, 0xcffaedfe]);
const FAT_MAGICS = new Set([0xcafebabe, 0xcafebabf]);
/** A fat header lists a handful of architectures; a Java class file has its version here. */
const MAX_FAT_ARCHS = 20;

/** Whether the first bytes of a file are a Mach-O header (thin or universal). */
export function isMachO(head) {
  if (head.length < 8) return false;
  const magic = head.readUInt32BE(0);
  if (THIN_MAGICS.has(magic)) return true;
  if (FAT_MAGICS.has(magic)) {
    const archs = head.readUInt32BE(4);
    return archs > 0 && archs <= MAX_FAT_ARCHS;
  }
  return false;
}

function readHead(file) {
  const fd = fs.openSync(file, "r");
  try {
    const head = Buffer.alloc(8);
    const got = fs.readSync(fd, head, 0, 8, 0);
    return head.subarray(0, got);
  } finally {
    fs.closeSync(fd);
  }
}

function isBundleName(name) {
  return BUNDLE_SUFFIXES.some((suffix) => name.endsWith(suffix));
}

/**
 * Everything inside `app` that needs its own signature, in signing order: deepest first, so each
 * bundle comes after its contents. The app itself is not listed. Symlinks are not followed (a
 * framework's `Versions/Current` would otherwise be signed twice), and an executable directly in a
 * bundle's `Contents/MacOS` is left to its bundle's signature.
 */
export function nestedSignTargets(app) {
  const found = [];
  const walk = (dir, segments, parents) => {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue;
      const full = path.join(dir, entry.name);
      const here = [...segments, entry.name];
      if (entry.isDirectory()) {
        const bundle = isBundleName(entry.name);
        if (bundle) found.push({ path: full, kind: "bundle", depth: here.length });
        walk(full, here, bundle ? [...parents, here.length] : parents);
      } else if (entry.isFile()) {
        const inMacOS =
          here.length >= 3 &&
          here[here.length - 2] === "MacOS" &&
          here[here.length - 3] === "Contents" &&
          (parents.at(-1) ?? 0) === here.length - 3;
        if (inMacOS) continue;
        if (isMachO(readHead(full))) found.push({ path: full, kind: "file", depth: here.length });
      }
    }
  };
  walk(app, [], [0]);
  return found.sort(
    (a, b) => b.depth - a.depth || (a.path < b.path ? -1 : a.path > b.path ? 1 : 0),
  );
}

/** codesign arguments for one target. */
export function codesignArgs(target, { identity, keychain, entitlements }) {
  const args = ["--force", "--timestamp", "--options", "runtime"];
  args.push("--entitlements", entitlements, "--sign", identity);
  if (keychain) args.push("--keychain", keychain);
  args.push(target);
  return args;
}

/**
 * Recompute each archive's `ElectronAsarIntegrity` hash (SHA-256 of its header text, as Electron
 * checks it) and rewrite Info.plist when one is stale. Throws when an archive has no entry: with
 * the integrity fuse on, such an app would refuse to start.
 */
export function refreshAsarIntegrity(app) {
  const resources = path.join(app, "Contents", "Resources");
  const plistFile = path.join(app, "Contents", "Info.plist");
  const archives = fs
    .readdirSync(resources)
    .filter((name) => name.endsWith(".asar"))
    .sort();
  let text = fs.readFileSync(plistFile, "utf8");
  const rewritten = [];
  for (const name of archives) {
    const key = `Resources/${name}`;
    const digest = createHash("sha256")
      .update(readAsarHeaderText(path.join(resources, name)))
      .digest("hex");
    const next = withIntegrityHash(text, key, digest);
    if (next === null) throw new Error(`Info.plist has no ElectronAsarIntegrity entry for ${key}`);
    if (next !== text) rewritten.push(key);
    text = next;
  }
  if (rewritten.length > 0) fs.writeFileSync(plistFile, text);
  return { archives, rewritten };
}

/**
 * Sign `app` (an `.app` folder) inside-out with `identity`. `run` and `log` are injectable; the
 * default runs the real tools.
 */
export async function signMacApp(app, options, { run = runTool, log = defaultLog } = {}) {
  const { identity, keychain, entitlements = ENTITLEMENTS } = options;
  if (!identity) throw new Error("no signing identity: electron-builder found no Developer ID");
  const nested = nestedSignTargets(app);
  for (const target of nested) {
    const args = codesignArgs(target.path, { identity, keychain, entitlements });
    await run("codesign", args, { label: `codesign ${path.relative(app, target.path)}` });
  }
  log(`[sign-mac] signed ${nested.length} nested targets inside ${path.basename(app)}`);
  const digests = refreshAsarIntegrity(app);
  log(
    `[sign-mac] ASAR integrity for ${digests.archives.length} archive(s): ` +
      (digests.rewritten.length > 0 ? `refreshed ${digests.rewritten.join(", ")}` : "current"),
  );
  await run("codesign", codesignArgs(app, { identity, keychain, entitlements }), {
    label: `codesign ${path.basename(app)}`,
  });
  await run("codesign", ["--verify", "--deep", "--strict", "--verbose=2", app], {
    label: "codesign --verify",
  });
  log(`[sign-mac] signed and verified ${path.basename(app)}`);
}

function defaultLog(line) {
  process.stdout.write(`${line}\n`);
}

/** electron-builder's custom `mac.sign`: `opts` carries the app path, identity and keychain. */
export default async function signMac(opts) {
  if (opts.platform === "mas") throw new Error("Mac App Store builds are not supported");
  await signMacApp(opts.app, { identity: opts.identity, keychain: opts.keychain });
}
