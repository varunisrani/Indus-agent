// What a packaged app must (and must not) hold (P20-38). Run by electron-builder's afterPack hook on
// every build and by CI over the output folder:
//
//   - `app.asar` holds the entry files (package.json, dist/index.html, dist-electron/main.js and
//     preload.cjs) and no TypeScript source (.ts, .tsx, .mts, .cts) and no source map;
//   - every native module file (`.node`) is unpacked, and node-pty, when packed, is unpacked with
//     its compiled binding present in `app.asar.unpacked/`;
//   - the build stamp sits beside the archive and validates, with the same stamp inside the archive
//     and in `app.asar.unpacked/` (P20-37 compares those two);
//   - a build whose stamp names electron-updater carries it in the archive (loaded by name).
//
//   node scripts/check-asar.mjs <release-dir | resources-dir>...   exit 0 ok, 1 problem, 2 usage

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { listAsar, readAsarHeader } from "./asar.mjs";
import { STAMP_FILE, validateBuildStamp } from "./write-build-stamp.mjs";

export const REQUIRED_ENTRIES = Object.freeze([
  "package.json",
  "dist/index.html",
  "dist-electron/main.js",
  "dist-electron/preload.cjs",
]);
const SOURCE = /\.(?:ts|tsx|mts|cts)$/i;
const SOURCE_MAP = /\.map$/i;
const NODE_PTY = "node_modules/node-pty/";

/** Problems with one resources folder (empty = fine) plus a short summary. */
export function checkResources(resourcesDir) {
  const problems = [];
  const archive = path.join(resourcesDir, "app.asar");
  let entries = [];
  try {
    entries = listAsar(readAsarHeader(archive));
  } catch (error) {
    return {
      problems: [`${archive}: ${error instanceof Error ? error.message : String(error)}`],
      summary: "",
    };
  }
  const paths = new Set(entries.map((e) => e.path));
  for (const required of REQUIRED_ENTRIES) {
    if (!paths.has(required)) problems.push(`missing ${required} in app.asar`);
  }
  for (const entry of entries) {
    if (SOURCE.test(entry.path)) problems.push(`TypeScript source in app.asar: ${entry.path}`);
    if (SOURCE_MAP.test(entry.path)) problems.push(`source map in app.asar: ${entry.path}`);
    if (entry.path.endsWith(".node") && !entry.unpacked) {
      problems.push(`native module packed inside app.asar: ${entry.path}`);
    }
  }
  const pty = entries.filter((e) => e.path.startsWith(NODE_PTY));
  if (pty.length > 0) {
    const packed = pty.filter((e) => e.kind === "file" && !e.unpacked);
    for (const entry of packed) problems.push(`node-pty file not unpacked: ${entry.path}`);
    const bindings = pty.filter((e) => e.path.endsWith(".node"));
    if (bindings.length === 0) problems.push("node-pty is packed without a compiled .node binding");
    for (const entry of bindings) {
      const onDisk = path.join(resourcesDir, "app.asar.unpacked", ...entry.path.split("/"));
      if (!fs.existsSync(onDisk)) problems.push(`unpacked binding missing on disk: ${onDisk}`);
    }
  }
  const stampFile = path.join(resourcesDir, STAMP_FILE);
  let stamp = null;
  try {
    stamp = validateBuildStamp(JSON.parse(fs.readFileSync(stampFile, "utf8")));
  } catch (error) {
    problems.push(`${STAMP_FILE}: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (!paths.has(STAMP_FILE)) problems.push(`missing ${STAMP_FILE} in app.asar`);
  const unpackedStamp = path.join(resourcesDir, "app.asar.unpacked", STAMP_FILE);
  if (!fs.existsSync(unpackedStamp)) problems.push(`missing ${unpackedStamp}`);
  else if (stamp !== null) {
    try {
      const copy = JSON.parse(fs.readFileSync(unpackedStamp, "utf8"));
      if (copy.version !== stamp.version || copy.commit !== stamp.commit) {
        problems.push(`${unpackedStamp} names another build than ${STAMP_FILE}`);
      }
    } catch (error) {
      problems.push(`${unpackedStamp}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  if (
    stamp !== null &&
    stamp.updateOwner === "electron-updater" &&
    !paths.has("node_modules/electron-updater/package.json")
  ) {
    problems.push("the stamp names electron-updater but app.asar does not carry it");
  }
  const summary =
    `${entries.length} entries, node-pty ${pty.length > 0 ? "unpacked" : "absent"}` +
    (stamp === null ? "" : `, stamp ${stamp.version} ${stamp.channel} ${stamp.updateOwner}`);
  return { problems, summary };
}

/**
 * The resources folders under an electron-builder output folder: `*.app/Contents/Resources`
 * (macOS), `*-unpacked/resources` (Windows, Linux). A folder holding `app.asar` is taken as is.
 */
export function findResourcesDirs(dir) {
  if (fs.existsSync(path.join(dir, "app.asar"))) return [dir];
  const found = [];
  const walk = (current, depth) => {
    let items = [];
    try {
      items = fs.readdirSync(current, { withFileTypes: true });
    } catch {
      return;
    }
    for (const item of items) {
      if (!item.isDirectory()) continue;
      const full = path.join(current, item.name);
      if (item.name.endsWith(".app")) {
        const resources = path.join(full, "Contents", "Resources");
        if (fs.existsSync(path.join(resources, "app.asar"))) found.push(resources);
      } else if (item.name.endsWith("-unpacked")) {
        const resources = path.join(full, "resources");
        if (fs.existsSync(path.join(resources, "app.asar"))) found.push(resources);
      } else if (depth < 3) {
        walk(full, depth + 1);
      }
    }
  };
  walk(dir, 0);
  return found;
}

/** Check every resources folder found under `targets`; `{ ok, lines }`. */
export function checkTargets(targets) {
  const lines = [];
  let ok = true;
  const dirs = targets.flatMap((target) => findResourcesDirs(target));
  if (dirs.length === 0) {
    return { ok: false, lines: [`no packaged app (app.asar) under ${targets.join(", ")}`] };
  }
  for (const dir of dirs) {
    const { problems, summary } = checkResources(dir);
    if (problems.length === 0) {
      lines.push(`ok   ${dir}: ${summary}`);
    } else {
      ok = false;
      lines.push(`FAIL ${dir}`);
      for (const problem of problems) lines.push(`     ${problem}`);
    }
  }
  return { ok, lines };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const targets = process.argv.slice(2);
  if (targets.length === 0) {
    process.stderr.write("usage: check-asar.mjs <release-dir | resources-dir>...\n");
    process.exit(2);
  }
  const { ok, lines } = checkTargets(targets.map((t) => path.resolve(t)));
  process.stdout.write(`${lines.join("\n")}\n`);
  process.exit(ok ? 0 : 1);
}
