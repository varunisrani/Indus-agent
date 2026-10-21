// Read the fuse wire of each packaged binary and fail on any mismatch with the wanted table.
//
//   node scripts/verify-fuses.mjs <dist-dir | binary | App.app> [...]
//
// Exit 0: every binary found has the six fuses as wanted. Exit 1: a mismatch, an unreadable wire
// or no binary found. Exit 2: usage.

import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { findPackagedBinaries, fuseMismatches, readFuses, WANTED_FUSES } from "./fuses.mjs";

/** Expand the arguments into binaries: a folder is searched, anything else is taken as given. */
export function binariesFor(args) {
  const out = [];
  for (const arg of args) {
    const full = path.resolve(arg);
    if (fs.existsSync(full) && fs.statSync(full).isDirectory() && !full.endsWith(".app")) {
      out.push(...findPackagedBinaries(full));
    } else {
      out.push(full);
    }
  }
  return out;
}

/** Verify each binary; returns `{ ok, lines }`. */
export async function verify(binaries) {
  const lines = [];
  let ok = binaries.length > 0;
  if (binaries.length === 0) lines.push("no packaged Electron binary found");
  for (const binary of binaries) {
    let states;
    try {
      states = await readFuses(binary);
    } catch (error) {
      ok = false;
      lines.push(
        `${binary}: no fuse wire (${error instanceof Error ? error.message : String(error)})`,
      );
      continue;
    }
    const bad = fuseMismatches(states);
    if (bad.length > 0) ok = false;
    lines.push(`${binary}: ${bad.length === 0 ? "ok" : `MISMATCH (${bad.join(", ")})`}`);
    for (const [name, wanted] of Object.entries(WANTED_FUSES)) {
      const want = wanted ? "on" : "off";
      lines.push(`  ${name.padEnd(40)} ${states[name].padEnd(8)} want ${want}`);
    }
  }
  return { ok, lines };
}

async function main(argv) {
  if (argv.length === 0) {
    process.stderr.write("usage: verify-fuses.mjs <dist-dir | binary> [...]\n");
    return 2;
  }
  const { ok, lines } = await verify(binariesFor(argv));
  for (const line of lines) process.stdout.write(`${line}\n`);
  return ok ? 0 : 1;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  process.exitCode = await main(process.argv.slice(2));
}
