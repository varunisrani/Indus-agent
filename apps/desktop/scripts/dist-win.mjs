// The Windows release build (P23-16), run by the release workflow's Windows job:
//
//   node scripts/dist-win.mjs [--dir] [--skip-build] [--channel stable|canary]
//
// It arms the Authenticode signer from the named secrets (sign-win.mjs `SIGNING_NAMES`: a complete
// Azure Trusted Signing group, else a PFX), removes them from the environment, then makes the build
// of dist-unsigned.mjs: one NSIS installer + ZIP per arch, the identity stamped before ASAR
// integrity and every PE signed once by the hooks. With no secrets the signer prints one line and
// the build is unsigned; a partial group fails before anything is built. A signed build also needs
// the channel's publisher (the certificate subject the update check compares, W50-e): while
// packaging/msix/identity.json still holds the owner placeholder, it is refused before building.
// Output: build/release/.

import path from "node:path";
import { fileURLToPath } from "node:url";
import identity from "./channel-identity.cjs";
import { distUnsigned, parseFlags } from "./dist-unsigned.mjs";
import { armWindowsSigning } from "./sign-win.mjs";

/**
 * Refuse a signed build whose update feed would name a placeholder publisher: every later update
 * would fail electron-updater's signer check. Unsigned builds pass (the placeholder fails closed).
 */
export function checkSignedPublisher(kind, channel, lookup = identity.windowsPublisher) {
  if (kind === "none") return null;
  const resolved = identity.resolveChannel(channel);
  const { publisher, placeholder, file } = lookup(resolved);
  if (placeholder) {
    throw new Error(
      `a signed Windows build needs the certificate subject as ${resolved}.publisher in ${file} ` +
        "(still the owner placeholder)",
    );
  }
  return publisher;
}

export async function distWin(flags, env = process.env) {
  if (process.platform !== "win32") throw new Error("the Windows build runs on Windows only");
  const kind = armWindowsSigning(env);
  checkSignedPublisher(kind, flags.channel ?? env.INDUS_DESKTOP_CHANNEL ?? "");
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
