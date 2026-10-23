// The macOS release build (P23-15), run by the release workflow's mac job:
//
//   node scripts/dist-mac.mjs [--dir] [--skip-build] [--channel stable|canary]
//
// With none of the signing names set (sign-mac.mjs `SIGNING_NAMES`) it prints one line and makes
// the unsigned build of dist-unsigned.mjs. With the certificate and one notary route set it makes
// the same build signed: the certificate goes to electron-builder under its own names (`CSC_LINK`,
// `CSC_KEY_PASSWORD`), `INDUS_MAC_SIGNING=1` turns the config's mac identity on, and the hooks do
// the rest (after-extract.mjs, sign-mac.mjs, notarize.mjs). A partial set fails before building.
// Output: build/release/ (DMG + ZIP). Nothing is published from here.

import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { buildBundles, distUnsigned, parseFlags, targetsFor } from "./dist-unsigned.mjs";
import { macSigningPlan } from "./sign-mac.mjs";
import { APP_DIR, stageApp } from "./stage-app.mjs";
import { STAMP_FILE, writeBuildStamp } from "./write-build-stamp.mjs";

const require = createRequire(import.meta.url);

/** Variables a signed mac build never needs: publishing tokens and the other OSes' signers. */
const NOT_FOR_MAC = Object.freeze(["GH_TOKEN", "GITHUB_TOKEN", "CSC_IDENTITY_AUTO_DISCOVERY"]);
const OTHER_SIGNER_PREFIXES = Object.freeze(["WIN_CSC_", "AZURE_", "WIN_CERTIFICATE"]);

/** The environment for a signed build: certificate under electron-builder's names, marker on. */
export function signedMacEnv(env) {
  const out = {};
  for (const [key, value] of Object.entries(env)) {
    if (NOT_FOR_MAC.includes(key)) continue;
    if (OTHER_SIGNER_PREFIXES.some((prefix) => key.startsWith(prefix))) continue;
    out[key] = value;
  }
  out.CSC_LINK = env.MAC_CERTIFICATE;
  out.CSC_KEY_PASSWORD = env.MAC_CERTIFICATE_PASSWORD;
  out.INDUS_MAC_SIGNING = "1";
  return out;
}

export async function distMac(flags, env = process.env) {
  if (process.platform !== "darwin") throw new Error("the macOS build runs on macOS only");
  const plan = macSigningPlan(env);
  if (!plan.signed) {
    process.stdout.write(`${plan.line}\n`);
    return distUnsigned(flags);
  }
  process.stdout.write(`[sign-mac] signed build; notarisation through the ${plan.notary} route\n`);
  if (!flags.skipBuild) await buildBundles();
  const stamp = writeBuildStamp(path.join(APP_DIR, "build", STAMP_FILE), {
    ...(flags.channel === undefined ? {} : { channel: flags.channel }),
  });
  const staged = stageApp({ stamp });
  process.stdout.write(`[stage] ${staged.outDir} (${stamp.version}, ${stamp.channel})\n`);
  fs.rmSync(path.join(APP_DIR, "build", "release"), { recursive: true, force: true });

  const signed = signedMacEnv(env);
  for (const key of Object.keys(process.env)) if (!(key in signed)) delete process.env[key];
  Object.assign(process.env, signed);
  const builder = require("electron-builder");
  const config = require(path.join(APP_DIR, "electron-builder.config.cjs"));
  const artefacts = await builder.build({
    projectDir: APP_DIR,
    config,
    publish: "never",
    ...targetsFor("darwin", { dir: flags.dir }),
  });
  for (const file of artefacts) process.stdout.write(`[artefact] ${file}\n`);
  return artefacts;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    await distMac(parseFlags(process.argv.slice(2)));
  } catch (error) {
    // Messages from this path name steps and missing names only, never a secret's value.
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exit(1);
  }
}
