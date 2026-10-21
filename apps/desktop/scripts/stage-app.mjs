// Stage the folder electron-builder packs (P20-38): only what the packaged app runs.
//
//   build/app/
//     package.json        generated: name, product name, version, main, native modules only
//     dist/               the renderer bundle (Vite), no source maps
//     dist-electron/      main.js + preload.cjs, no source maps
//     node_modules/<m>/   each native module the desktop declares (node-pty, P20-31) with its
//                         compiled binding / prebuilds for the target platform and its runtime
//                         dependencies; never its C++ sources. `spawn-helper` is made executable
//                         (npm tarballs can drop the bit; the PTY cannot start without it)
//     node_modules/electron-updater/ (+ its runtime dependencies) when the stamp names it the
//                         update owner (P20-37 loads it by name, it is not in the main bundle)
//     desktop-build-stamp.json  the stamp inside the archive (P20-37 compares it with the copy
//                         the afterPack hook puts in `app.asar.unpacked/`, R08.14)
//
// The renderer and main bundles already inline every other JavaScript dependency, so nothing else
// from the pnpm workspace's node_modules enters the archive. Every staged package's licence must be
// on the repository's allow list (`tooling/checks/licenses_allow.json`): build-only packages with
// other licences (electron-builder's tree) can never reach the packaged app.

import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
/** Packages with a compiled binding the main process loads at run time. */
export const NATIVE_MODULES = Object.freeze(["node-pty"]);
/** Plain JavaScript packages main loads by name at run time (kept out of the bundle). */
export const RUNTIME_MODULES = Object.freeze(["electron-updater"]);
export const STAMP_FILE = "desktop-build-stamp.json";
const REPO_ALLOW_FILE = path.resolve(
  APP_DIR,
  "..",
  "..",
  "tooling",
  "checks",
  "licenses_allow.json",
);
/** The Linux/Windows app id; also the desktop entry's file name on Linux. */
export const APP_ID = "ai.indus.desktop";
export const PACKAGE_NAME = "indus-desktop";

/** Parts of a native module's folder that never ship (sources, build scripts, typings). */
const SKIP_IN_MODULE = new Set([
  "src",
  "deps",
  "third_party",
  "scripts",
  "typings",
  "binding.gyp",
  "node_modules",
  "test",
  "tests",
]);

function copyTree(from, to, keep = () => true) {
  fs.mkdirSync(to, { recursive: true });
  for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
    const source = path.join(from, entry.name);
    const target = path.join(to, entry.name);
    if (!keep(source, entry)) continue;
    if (entry.isDirectory()) copyTree(source, target, keep);
    else if (entry.isFile()) {
      fs.copyFileSync(source, target);
      fs.chmodSync(target, fs.statSync(source).mode & 0o777);
    }
  }
}

const noMaps = (_source, entry) => !(entry.isFile() && entry.name.endsWith(".map"));

function listFiles(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...listFiles(full));
    else out.push(full);
  }
  return out;
}

/**
 * Copy one native module for `platform`: package.json, licence, readme, `lib/`, the files directly
 * in `build/Release/` (the binding and helpers such as `spawn-helper`), and `prebuilds/<platform>-*`.
 */
export function copyNativeModule(moduleDir, target, platform) {
  copyTree(moduleDir, target, (source, entry) => {
    const rel = path.relative(moduleDir, source).split(path.sep);
    const top = rel[0] ?? "";
    if (rel.length === 1 && SKIP_IN_MODULE.has(top)) return false;
    if (entry.isFile() && /\.(?:ts|tsx|mts|cts|map|cc|cpp|h|gyp|gypi)$/.test(entry.name)) {
      return false; // sources, typings and maps (`.d.ts` included)
    }
    if (entry.isFile() && /\.test\.js$/.test(entry.name)) return false;
    if (top === "build") {
      // build/ and build/Release/ only; the object files and makefiles under it never ship
      if (rel.length === 1) return entry.isDirectory();
      if (rel.length === 2) return rel[1] === "Release";
      if (rel.length === 3) return rel[1] === "Release" && entry.isFile();
      return false;
    }
    if (top === "prebuilds" && rel.length >= 2) return (rel[1] ?? "").startsWith(`${platform}-`);
    return true;
  });
  const bindings = listFiles(target).filter((file) => file.endsWith(".node"));
  if (bindings.length === 0) {
    throw new Error(
      `stage: ${path.basename(moduleDir)} has no compiled binding for ${platform} ` +
        "(build it first: pnpm install with a C++ toolchain, or add a prebuild)",
    );
  }
  return bindings;
}

/** A package's runtime dependencies resolved from its own folder (names → folders). */
function runtimeDependencies(moduleDir) {
  const pkg = JSON.parse(fs.readFileSync(path.join(moduleDir, "package.json"), "utf8"));
  const resolver = createRequire(path.join(moduleDir, "package.json"));
  const out = [];
  for (const name of Object.keys(pkg.dependencies ?? {})) {
    out.push([name, path.dirname(resolver.resolve(`${name}/package.json`))]);
  }
  return out;
}

/** Copy `name` and its runtime dependency closure (resolved from `moduleDir`) under `nodeModules`. */
function copyWithDependencies(name, moduleDir, nodeModules, seen) {
  const queue = [[name, moduleDir]];
  while (queue.length > 0) {
    const [dep, depDir] = queue.shift();
    if (seen.has(dep)) continue;
    seen.add(dep);
    copyTree(depDir, path.join(nodeModules, dep), (_s, entry) => {
      if (entry.isDirectory() && entry.name === "node_modules") return false;
      return !(entry.isFile() && /\.(?:map|d\.ts)$/.test(entry.name));
    });
    queue.push(...runtimeDependencies(depDir));
  }
}

/** True when an SPDX expression is satisfied by the allowed identifiers (OR: one side, AND: both). */
export function licenceAllowed(expression, allow) {
  if (typeof expression !== "string" || expression.trim() === "") return false;
  const text = expression.trim().replace(/^\((.*)\)$/, "$1");
  const or = text.split(/\s+OR\s+/i);
  if (or.length > 1) return or.some((part) => licenceAllowed(part, allow));
  const and = text.split(/\s+AND\s+/i);
  if (and.length > 1) return and.every((part) => licenceAllowed(part, allow));
  return allow.has(text.replace(/[()]/g, "").trim());
}

/** The licence a package.json declares (`license` string or the old `licenses` list). */
function declaredLicence(pkg) {
  if (typeof pkg.license === "string") return pkg.license;
  if (pkg.license !== null && typeof pkg.license === "object") return pkg.license.type ?? "";
  if (Array.isArray(pkg.licenses)) {
    return pkg.licenses.map((entry) => entry?.type ?? "").join(" OR ");
  }
  return "";
}

/** Every package staged under `outDir/node_modules` whose licence is not allowed. */
export function disallowedLicences(outDir, allow) {
  const found = [];
  const root = path.join(outDir, "node_modules");
  if (!fs.existsSync(root)) return found;
  const visit = (dir) => {
    const manifest = path.join(dir, "package.json");
    if (!fs.existsSync(manifest)) return;
    const pkg = JSON.parse(fs.readFileSync(manifest, "utf8"));
    const licence = declaredLicence(pkg);
    if (!licenceAllowed(licence, allow)) {
      found.push(`${pkg.name ?? path.basename(dir)}@${pkg.version ?? "?"} (${licence || "none"})`);
    }
  };
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const dir = path.join(root, entry.name);
    if (entry.name.startsWith("@")) {
      for (const inner of fs.readdirSync(dir, { withFileTypes: true })) {
        if (inner.isDirectory()) visit(path.join(dir, inner.name));
      }
    } else visit(dir);
  }
  return found;
}

function readAllow(file) {
  return new Set(JSON.parse(fs.readFileSync(file, "utf8")).allow ?? []);
}

/** `spawn-helper` (node-pty's macOS/Linux launcher) must stay executable in the package. */
function makeHelpersExecutable(dir) {
  if (!fs.existsSync(dir)) return;
  for (const file of listFiles(dir)) {
    if (path.basename(file) === "spawn-helper") fs.chmodSync(file, 0o755);
  }
}

/**
 * Stage `appDir`'s build into `outDir`. `stamp` gives the version; `platform` picks prebuilds.
 * Returns `{ outDir, packageJson, natives }`.
 */
export function stageApp({
  appDir = APP_DIR,
  outDir = path.join(APP_DIR, "build", "app"),
  stamp,
  platform = process.platform,
  allowFile = REPO_ALLOW_FILE,
}) {
  if (stamp === undefined || typeof stamp.version !== "string") {
    throw new Error("stage: a build stamp (with a version) is required");
  }
  const desktop = JSON.parse(fs.readFileSync(path.join(appDir, "package.json"), "utf8"));
  for (const built of ["dist/index.html", "dist-electron/main.js", "dist-electron/preload.cjs"]) {
    if (!fs.existsSync(path.join(appDir, built))) {
      throw new Error(`stage: ${built} is missing (run the renderer and main builds first)`);
    }
  }

  fs.rmSync(outDir, { recursive: true, force: true });
  fs.mkdirSync(outDir, { recursive: true });
  copyTree(path.join(appDir, "dist"), path.join(outDir, "dist"), noMaps);
  copyTree(path.join(appDir, "dist-electron"), path.join(outDir, "dist-electron"), noMaps);

  const resolver = createRequire(path.join(appDir, "package.json"));
  const declared = { ...(desktop.dependencies ?? {}), ...(desktop.optionalDependencies ?? {}) };
  const dependencies = {};
  const natives = [];
  for (const name of NATIVE_MODULES) {
    if (!(name in declared)) continue;
    const moduleDir = path.dirname(resolver.resolve(`${name}/package.json`));
    const version = JSON.parse(
      fs.readFileSync(path.join(moduleDir, "package.json"), "utf8"),
    ).version;
    dependencies[name] = version;
    const bindings = copyNativeModule(moduleDir, path.join(outDir, "node_modules", name), platform);
    natives.push({ name, version, bindings: bindings.map((b) => path.relative(outDir, b)) });
    const queue = runtimeDependencies(moduleDir);
    const seen = new Set([name]);
    while (queue.length > 0) {
      const [dep, depDir] = queue.shift();
      if (seen.has(dep)) continue;
      seen.add(dep);
      copyTree(depDir, path.join(outDir, "node_modules", dep), (_s, entry) => {
        return !(entry.isDirectory() && entry.name === "node_modules");
      });
      queue.push(...runtimeDependencies(depDir));
    }
  }
  makeHelpersExecutable(path.join(outDir, "node_modules", "node-pty"));

  // electron-updater ships only in builds it updates (macOS ZIP, Linux AppImage)
  const runtimeSeen = new Set(NATIVE_MODULES);
  for (const name of RUNTIME_MODULES) {
    if (!(name in declared) || stamp.updateOwner !== "electron-updater") continue;
    const moduleDir = path.dirname(resolver.resolve(`${name}/package.json`));
    dependencies[name] = JSON.parse(
      fs.readFileSync(path.join(moduleDir, "package.json"), "utf8"),
    ).version;
    copyWithDependencies(name, moduleDir, path.join(outDir, "node_modules"), runtimeSeen);
  }

  const refused = disallowedLicences(outDir, readAllow(allowFile));
  if (refused.length > 0) {
    throw new Error(
      `stage: packages with a licence not on the allow list would ship: ${refused.join(", ")}`,
    );
  }
  fs.writeFileSync(path.join(outDir, STAMP_FILE), `${JSON.stringify(stamp, null, 2)}\n`, "utf8");

  const packageJson = {
    name: PACKAGE_NAME,
    productName: desktop.productName ?? "Indus",
    version: stamp.version,
    description: desktop.description ?? "Indus Agent desktop app",
    author: { name: "Indus Agent contributors" },
    license: "MIT",
    type: "module",
    main: "dist-electron/main.js",
    desktopName: `${APP_ID}.desktop`,
    dependencies,
  };
  fs.writeFileSync(
    path.join(outDir, "package.json"),
    `${JSON.stringify(packageJson, null, 2)}\n`,
    "utf8",
  );
  return { outDir, packageJson, natives };
}
