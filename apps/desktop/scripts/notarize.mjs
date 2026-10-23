// electron-builder `afterSign` hook (P23-15): notarise the signed macOS app with Apple, staple the
// ticket and check that Gatekeeper accepts the result, before electron-builder builds the DMG and
// the ZIP (the update artefact) from it.
//
//   ditto (zip the .app into a private temp folder) → notarytool submit --wait → on anything but
//   "Accepted": notarytool log, fail → stapler staple (retried while Apple's ticket service catches
//   up) → stapler validate → spctl --assess --type execute
//
// The notary credentials come from the environment by name (sign-mac.mjs `NOTARY_ROUTES`); the API
// key route is used when complete, otherwise the Apple ID route. No value is printed or logged.
// The hook also fires after Windows signing (P23-16) and returns at once there.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { macSigningPlan, runTool } from "./sign-mac.mjs";

/** Waits between staple attempts, in milliseconds (a fresh ticket can take minutes to appear). */
export const STAPLE_WAITS = Object.freeze([15_000, 30_000, 60_000, 120_000]);
/** notarytool's own limit on `--wait`. */
export const NOTARY_WAIT = "2h";

/**
 * notarytool authentication arguments for `route`. An APPLE_API_KEY that is not a path to an
 * existing file is taken as the key text and written to a 0600 file inside `privateDir`.
 */
export function notaryAuth(route, env, privateDir) {
  if (route === "apple-id") {
    return [
      "--apple-id",
      String(env.APPLE_ID),
      "--team-id",
      String(env.APPLE_TEAM_ID),
      "--password",
      String(env.APPLE_APP_SPECIFIC_PASSWORD),
    ];
  }
  const given = String(env.APPLE_API_KEY).trim();
  let keyFile = given;
  if (!fs.existsSync(given)) {
    keyFile = path.join(privateDir, `AuthKey_${String(env.APPLE_API_KEY_ID).trim()}.p8`);
    fs.writeFileSync(keyFile, `${given}\n`, { mode: 0o600 });
  }
  return [
    "--key",
    keyFile,
    "--key-id",
    String(env.APPLE_API_KEY_ID),
    "--issuer",
    String(env.APPLE_API_ISSUER),
  ];
}

/** `{ id, status }` from notarytool's JSON output, or null. */
export function submissionOf(output) {
  try {
    const parsed = JSON.parse(String(output ?? "").trim());
    if (parsed && typeof parsed.id === "string" && typeof parsed.status === "string") {
      return { id: parsed.id, status: parsed.status };
    }
  } catch {
    // not JSON: handled by the caller
  }
  return null;
}

async function submit(zip, auth, run) {
  const args = ["notarytool", "submit", zip, "--wait", "--timeout", NOTARY_WAIT];
  args.push("--output-format", "json", ...auth);
  try {
    const { stdout } = await run("xcrun", args, { label: "notarytool submit" });
    return { submission: submissionOf(stdout), failure: null };
  } catch (error) {
    // notarytool can exit non-zero on a rejected submission and still print its JSON.
    return { submission: submissionOf(error?.stdout), failure: error };
  }
}

async function staple(app, run, log, sleep) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      await run("xcrun", ["stapler", "staple", app], { label: "stapler staple" });
      return;
    } catch (error) {
      const wait = STAPLE_WAITS[attempt];
      if (wait === undefined) throw error;
      log(`[notarize] ticket not stapled yet; trying again in ${wait / 1000}s`);
      await sleep(wait);
    }
  }
}

/**
 * Notarise, staple and assess `app` (a signed `.app`). `deps` (run, log, sleep, tempRoot) are
 * injectable for tests.
 */
export async function notarizeApp(app, env, route, deps = {}) {
  const run = deps.run ?? runTool;
  const log = deps.log ?? ((line) => process.stdout.write(`${line}\n`));
  const sleep = deps.sleep ?? delay;
  const privateDir = fs.mkdtempSync(path.join(deps.tempRoot ?? os.tmpdir(), "indus-notary-"));
  fs.chmodSync(privateDir, 0o700);
  try {
    const auth = notaryAuth(route, env, privateDir);
    const zip = path.join(privateDir, `${path.basename(app, ".app")}.zip`);
    await run("ditto", ["-c", "-k", "--sequesterRsrc", "--keepParent", app, zip], {
      label: "ditto",
    });
    log(`[notarize] submitted ${path.basename(app)} (${route}); waiting for Apple`);
    const { submission, failure } = await submit(zip, auth, run);
    if (submission === null) throw failure ?? new Error("notarytool gave no submission result");
    log(`[notarize] submission ${submission.id}: ${submission.status}`);
    if (submission.status !== "Accepted") {
      let details;
      try {
        const args = ["notarytool", "log", submission.id, ...auth];
        details = (await run("xcrun", args, { label: "notarytool log" })).stdout;
      } catch (error) {
        details = `(Apple's log could not be read: ${error instanceof Error ? error.message : error})`;
      }
      throw new Error(`notarisation ${submission.id} ended ${submission.status}\n${details}`);
    }
    await staple(app, run, log, sleep);
    await run("xcrun", ["stapler", "validate", app], { label: "stapler validate" });
    await run("spctl", ["--assess", "--type", "execute", "--verbose=2", app], {
      label: "Gatekeeper assessment",
    });
    log(`[notarize] ${path.basename(app)} notarised, stapled and accepted by Gatekeeper`);
  } finally {
    fs.rmSync(privateDir, { recursive: true, force: true });
  }
}

/** The hook. `env` and `deps` are for tests; electron-builder passes the context only. */
export default async function afterSign(context, env = process.env, deps = {}) {
  if (context.electronPlatformName !== "darwin") return;
  const plan = macSigningPlan(env);
  const log = deps.log ?? ((line) => process.stdout.write(`${line}\n`));
  if (!plan.signed) {
    log("[notarize] skipped: the build is not signed");
    return;
  }
  const app = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  if (!fs.existsSync(app)) throw new Error(`no app bundle to notarise at ${app}`);
  await notarizeApp(app, env, plan.notary, { ...deps, log });
}
