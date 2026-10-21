// electron-builder configuration for the desktop app (P20-38): unsigned builds on three OSes.
//
//   macOS    DMG (download) + ZIP (the update artefact); hardened runtime, entitlements and the
//            Info.plist usage strings are ready for P23, but `identity: null` keeps it unsigned
//   Windows  NSIS installer (per user) + portable ZIP, unsigned (D-16.84; Authenticode P23, D-16.70;
//            MSIX deferred, D-16.15)
//   Linux    AppImage
//
// The packed folder is `build/app`, staged by scripts/stage-app.mjs (bundles + native modules
// only). After packing, the fuses are flipped (P20-04) and the archive is checked
// (scripts/check-asar.mjs); either failing fails the build. Nothing is published from here
// (`publish: null`; scripts/dist-unsigned.mjs also passes `--publish never`).
// @ts-check
"use strict";

const path = require("node:path");
const { pathToFileURL } = require("node:url");

const APP_ID = "ai.indus.desktop";
const PRODUCT = "Indus";
const DEEP_LINK_SCHEME = "indus";

const desktopPackage = require("./package.json");
const electronVersion = desktopPackage.devDependencies.electron;
if (!/^\d+\.\d+\.\d+$/.test(electronVersion)) {
  throw new Error(`electron must be pinned to an exact version, got ${electronVersion}`);
}

/** @param {string} script @param {string} name */
async function runHook(script, name, context) {
  const mod = await import(pathToFileURL(path.join(__dirname, "scripts", script)).href);
  return mod[name](context);
}

/** The resources folder inside one packed output folder. */
function resourcesDir(context) {
  if (context.electronPlatformName === "darwin" || context.electronPlatformName === "mas") {
    const app = `${context.packager.appInfo.productFilename}.app`;
    return path.join(context.appOutDir, app, "Contents", "Resources");
  }
  return path.join(context.appOutDir, "resources");
}

async function afterPack(context) {
  // Fuses first, on the pristine packed binary, before anything could sign it (P20-04).
  await runHook("after-pack-fuses.mjs", "default", context);
  // The stamp's second copy beside the archive's unpacked files: P20-37 compares the two to
  // notice a half-applied update (mixed generations, R08.14).
  const fs = require("node:fs");
  const unpacked = path.join(resourcesDir(context), "app.asar.unpacked");
  fs.mkdirSync(unpacked, { recursive: true });
  fs.copyFileSync(
    path.join(__dirname, "build", "desktop-build-stamp.json"),
    path.join(unpacked, "desktop-build-stamp.json"),
  );
  const { checkResources } = await import(
    pathToFileURL(path.join(__dirname, "scripts", "check-asar.mjs")).href
  );
  const { problems, summary } = checkResources(resourcesDir(context));
  if (problems.length > 0) {
    throw new Error(`packaged app check failed:\n  ${problems.join("\n  ")}`);
  }
  process.stdout.write(`[asar] ${summary}\n`);
}

/** Usage strings for what the app can ask the OS for (voice, capture, local servers, automation). */
const MAC_USAGE = {
  NSMicrophoneUsageDescription: `${PRODUCT} listens through the microphone only while you use voice input.`,
  NSCameraUsageDescription: `${PRODUCT} uses the camera only when a feature you turned on asks for it.`,
  NSScreenCaptureUsageDescription: `${PRODUCT} records the screen only when you ask the agent to capture it.`,
  NSLocalNetworkUsageDescription: `${PRODUCT} talks to Indus services you run on this computer or your network.`,
  NSAppleEventsUsageDescription: `${PRODUCT} controls other apps only when you ask the agent to.`,
};

/** @type {import("electron-builder").Configuration} */
const config = {
  appId: APP_ID,
  productName: PRODUCT,
  electronVersion,
  // Offline local builds can point at an unpacked Electron (`node_modules/electron/dist`).
  ...(process.env.INDUS_DESKTOP_ELECTRON_DIST
    ? { electronDist: process.env.INDUS_DESKTOP_ELECTRON_DIST }
    : {}),
  copyright: "Copyright © 2026 Indus Agent contributors",
  directories: {
    app: "build/app",
    output: "build/release",
    buildResources: "resources",
  },
  files: ["**/*", "!**/*.map", "!**/*.{ts,tsx,mts,cts}"],
  extraResources: [
    { from: "build/desktop-build-stamp.json", to: "desktop-build-stamp.json" },
    { from: "resources/icons/png/256x256.png", to: "icons/indus.png" },
  ],
  asar: true,
  asarUnpack: ["**/*.node", "**/prebuilds/**", "node_modules/node-pty/**"],
  // Native modules arrive staged and already built for the target (N-API bindings and prebuilds).
  npmRebuild: false,
  nodeGypRebuild: false,
  buildDependenciesFromSource: false,
  protocols: [{ name: `${PRODUCT} link`, schemes: [DEEP_LINK_SCHEME] }],
  // biome-ignore lint/suspicious/noTemplateCurlyInString: electron-builder fills this macro
  artifactName: "Indus-${version}-${os}-${arch}.${ext}",
  publish: null,
  forceCodeSigning: false,
  afterPack,

  mac: {
    target: ["dmg", "zip"],
    category: "public.app-category.developer-tools",
    icon: "resources/icons/icon.png",
    // Unsigned until P23: no identity, no notarisation, no Gatekeeper assessment.
    identity: null,
    notarize: false,
    gatekeeperAssess: false,
    hardenedRuntime: true,
    entitlements: "resources/entitlements.mac.plist",
    entitlementsInherit: "resources/entitlements.mac.plist",
    extendInfo: MAC_USAGE,
  },
  dmg: {
    // biome-ignore lint/suspicious/noTemplateCurlyInString: electron-builder fills this macro
    title: "Indus ${version}",
    writeUpdateInfo: false,
  },

  win: {
    target: ["nsis", "zip"],
    icon: "resources/icons/icon.png",
    // No certificate is configured anywhere; dist-unsigned.mjs also strips signing variables.
    verifyUpdateCodeSignature: false,
  },
  nsis: {
    oneClick: true,
    perMachine: false,
    // biome-ignore lint/suspicious/noTemplateCurlyInString: electron-builder fills this macro
    artifactName: "Indus-Setup-${version}-${arch}.${ext}",
    deleteAppDataOnUninstall: false,
    differentialPackage: false,
  },

  linux: {
    target: ["AppImage"],
    category: "Development",
    icon: "resources/icons/png",
    // Never `indus`: the CLI of the same name lives on PATH.
    executableName: "indus-desktop",
    synopsis: "Desktop app for Indus Agent",
    description: "Chat with Indus Agent, review its edits and run its tools from a desktop window.",
  },
};

module.exports = config;
