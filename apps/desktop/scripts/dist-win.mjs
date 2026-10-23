// The Windows release build (P23-16), run by the release workflow's Windows job:
//
//   node scripts/dist-win.mjs [--dir] [--skip-build] [--channel stable|canary]
//
// It arms the Authenticode signer from the named secrets (sign-win.mjs `SIGNING_NAMES`: a complete
// Azure Trusted Signing group, else a PFX), removes them from the environment, then makes the build
// of dist-unsigned.mjs: one NSIS installer + ZIP per arch, the identity stamped before ASAR
// integrity and every PE signed once by the hooks. With no secrets the signer prints one line and
// the build is unsigned; a partial group fails before anything is built. Output: build/release/.

import path from "node:path";
import { fileURLToPath } from "node:url";
import { distUnsigned, parseFlags } from "./dist-unsigned.mjs";
import { armWindowsSigning } from "./sign-win.mjs";

export async function distWin(flags, env = process.env) {
  if (process.platform !== "win32") throw new Error("the Windows build runs on Windows only");
  const kind = armWindowsSigning(env);
  if (kind !== "none") process.stdout.write(`[sign-win] signed build (${kind})\n`);
  return distUnsigned(flags);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    await distWin(parseFlags(process.argv.slice(2)));
  } catch (error) {
    // Messages from this path name steps and missing names only, never a secret's value.
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exit(1);
  }
}
