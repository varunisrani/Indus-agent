// electron-builder configuration for the desktop app (P20-38): unsigned builds on three OSes.
//
//   macOS    DMG (download) + ZIP (the update artefact); unsigned unless scripts/dist-mac.mjs
//            armed the release certificate (P23-15): then signed inside-out with the hardened
//            runtime, notarised and stapled by the hooks below
//   Windows  one NSIS installer (per user) + portable ZIP per arch (x64, arm64); Authenticode
//            through scripts/sign-win.mjs when the named secrets are set, else unsigned with one
//            line (P23-16, D-16.70; MSIX is P24-22, D-16.15)
//   Linux    AppImage + .deb, unsigned, with SHA-256 sums beside them (scripts/checksums.mjs)
//
// Identity and update feed follow the channel (scripts/channel-identity.cjs, P23-16): a canary
// gets its own app id, product name (install folder, userData, "Indus Canary.app"), Linux
// executable and feed.
//
// The packed folder is `build/app`, staged by scripts/stage-app.mjs (bundles + native modules
// only). After packing, the fuses are flipped (P20-04) and the archive is checked
// (scripts/check-asar.mjs); either failing fails the build. Nothing is published from here
// (`publish: null`; scripts/dist-unsigned.mjs also passes `--publish never`).
// @ts-check
"use strict";

const path = require("node:path");
const { pathToFileURL } = require("node:url");

const { channelIdentity, releaseRepository } = require("./scripts/channel-identity.cjs");

const IDENTITY = channelIdentity(process.env.INDUS_DESKTOP_CHANNEL ?? "");
const APP_ID = IDENTITY.appId;
const PRODUCT = IDENTITY.productName;
const DEEP_LINK_SCHEME = "indus";
const RELEASES = releaseRepository();
/** Windows installers: one per architecture, never a combined one (P23-16). */
const WINDOWS_ARCHES = ["x64", "arm64"];

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
  // Windows: every PE signed once, after the fuses changed the binary (P23-16). No-op elsewhere.
  await runHook("sign-win.mjs", "signPackedWindowsApp", context);
}

/**
 * After extraction, before the rename and the ASAR integrity resource (P23-15 / P23-16): the build
 * identity goes onto the pristine Electron binary through scripts/after-extract.mjs, whose STAMPERS
 * hold one stamper per platform (darwin: Info.plist keys; win32: stampWindowsExecutable).
 */
async function afterExtract(context) {
  await runHook("after-extract.mjs", "default", context);
}

/** Notarisation follows signing (P23-15); the hook returns at once off macOS. */
async function afterSign(context) {
  await runHook("notarize.mjs", "default", context);
}

/** electron-builder's hand-off of the NSIS uninstaller and installer to our signer. */
async function signWindowsFile(configuration) {
  await runHook("sign-win.mjs", "signWithIndus", configuration);
}

/** SHA-256 sums beside the Linux artefacts (returned so the build lists the file too). */
async function afterAllArtifactBuild(buildResult) {
  return runHook("checksums.mjs", "linuxChecksums", buildResult);
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
    // P25-17: the generated notices, the same file the Docker image and the repository carry.
    { from: "../../THIRD_PARTY_NOTICES.md", to: "THIRD_PARTY_NOTICES.md" },
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
  // Electron derives the userData folder from the packaged productName: a canary keeps its own.
  extraMetadata: { productName: PRODUCT },
  // The electron-updater feed per channel on GitHub Releases (latest*.yml / canary*.yml beside
  // the artefacts, app-update.yml in the app). Nothing is published from here: dist-unsigned.mjs
  // passes `--publish never`; the release workflow uploads the files.
  publish: [
    {
      provider: "github",
      owner: RELEASES.owner,
      repo: RELEASES.repo,
      channel: IDENTITY.feedChannel,
    },
  ],
  forceCodeSigning: false,
  afterExtract,
  afterPack,
  afterSign,
  afterAllArtifactBuild,

  mac: {
    target: ["dmg", "zip"],
    category: "public.app-category.developer-tools",
    icon: "resources/icons/icon.png",
    // P23-15. `null` keeps a build unsigned; dist-mac.mjs sets INDUS_MAC_SIGNING=1 and the
    // certificate as CSC_LINK only when the release secrets are complete (dist-unsigned.mjs strips
    // CSC_LINK), and electron-builder then finds the Developer ID itself. Our signer replaces
    // electron-builder's: nested code inside-out, ASAR integrity refreshed, the app last
    // (scripts/sign-mac.mjs). Notarisation and the Gatekeeper assessment run in the afterSign
    // hook, so both built-in switches stay off.
    identity: process.env.INDUS_MAC_SIGNING === "1" && process.env.CSC_LINK ? undefined : null,
    sign: (options) => runHook("sign-mac.mjs", "default", options),
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
    target: [
      { target: "nsis", arch: WINDOWS_ARCHES },
      { target: "zip", arch: WINDOWS_ARCHES },
    ],
    icon: "resources/icons/icon.png",
    // One signer (scripts/sign-win.mjs): the identity is stamped in afterExtract and every PE is
    // signed in afterPack, so electron-builder's own late resource edit and signing stay off;
    // it still hands the uninstaller and installer to signWindowsFile.
    signAndEditExecutable: false,
    signtoolOptions: {
      sign: signWindowsFile,
      signingHashAlgorithms: ["sha256"],
    },
    // The Windows build is updated by hand (the stamp's update owner) until a signed build's
    // publisher name is known; then this check turns on with electron-updater (P23-16 follow-up).
    verifyUpdateCodeSignature: false,
  },
  nsis: {
    oneClick: true,
    perMachine: false,
    buildUniversalInstaller: false,
    // biome-ignore lint/suspicious/noTemplateCurlyInString: electron-builder fills this macro
    artifactName: "Indus-Setup-${version}-${arch}.${ext}",
    deleteAppDataOnUninstall: false,
    differentialPackage: false,
  },

  linux: {
    target: ["AppImage", "deb"],
    category: "Development",
    icon: "resources/icons/png",
    // Never `indus`: the CLI of the same name lives on PATH (`indus-desktop-canary` for canary).
    executableName: IDENTITY.executableName,
    // The release checklist's placeholder (RM12) until the owner names a monitored address.
    maintainer: "Indus Agent maintainers <maintainers@indus.invalid>",
    synopsis: "Desktop app for Indus Agent",
    description: "Chat with Indus Agent, review its edits and run its tools from a desktop window.",
  },
  deb: {
    // The canary installs beside the stable package.
    packageName: IDENTITY.executableName,
  },
};

module.exports = config;
