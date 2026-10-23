// SHA-256 sums for the Linux desktop artefacts (P23-16). The AppImage and the .deb ship unsigned;
// the sums file next to them lets a person check a download with `sha256sum -c`.
//
//   node scripts/checksums.mjs write DIR    sums of every Linux artefact in DIR → DIR/SHA256SUMS-linux.txt
//   node scripts/checksums.mjs check FILE   verify FILE's entries against the files beside it
//
// electron-builder calls `linuxChecksums` from `afterAllArtifactBuild` (electron-builder.config.cjs)
// with the paths it built; it returns the sums file so the build lists it as an artefact too.

import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const SUMS_FILE = "SHA256SUMS-linux.txt";
export const LINUX_ARTEFACT = /\.(?:AppImage|deb)$/;

/** Hex SHA-256 of a file, streamed (AppImages are large). */
export function fileDigest(file) {
  const hash = createHash("sha256");
  const fd = fs.openSync(file, "r");
  try {
    const chunk = Buffer.alloc(1 << 20);
    for (;;) {
      const read = fs.readSync(fd, chunk, 0, chunk.length, null);
      if (read === 0) break;
      hash.update(chunk.subarray(0, read));
    }
  } finally {
    fs.closeSync(fd);
  }
  return hash.digest("hex");
}

/** `sha256sum` text format: `<hex>  <name>` per line, sorted by name, one trailing newline. */
export function formatSums(entries) {
  const lines = [...entries]
    .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
    .map(({ digest, name }) => `${digest}  ${name}`);
  return lines.length === 0 ? "" : `${lines.join("\n")}\n`;
}

/** Parse a sums file; throws on a line that is not `<64 hex>  <name>`. */
export function parseSums(text) {
  const out = [];
  for (const line of text.split("\n")) {
    if (line === "") continue;
    const match = /^([0-9a-f]{64}) [ *](.+)$/.exec(line);
    if (match === null) throw new Error(`checksums: bad line ${JSON.stringify(line)}`);
    out.push({ digest: match[1], name: match[2] });
  }
  return out;
}

/**
 * Write the sums of `files` (all in one folder) to `<folder>/SHA256SUMS-linux.txt`; returns its
 * path, or null when there is nothing to list. A missing file is an error.
 */
export function writeSums(files) {
  const listed = files.filter((file) => path.basename(file) !== SUMS_FILE);
  if (listed.length === 0) return null;
  const dirs = new Set(listed.map((file) => path.dirname(path.resolve(file))));
  if (dirs.size !== 1) throw new Error(`checksums: artefacts in ${dirs.size} folders`);
  const [dir] = dirs;
  const entries = listed.map((file) => {
    if (!fs.existsSync(file)) throw new Error(`checksums: ${file} does not exist`);
    return { digest: fileDigest(file), name: path.basename(file) };
  });
  const out = path.join(dir, SUMS_FILE);
  fs.writeFileSync(out, formatSums(entries), "utf8");
  return out;
}

/** The Linux artefacts among `paths` (AppImage and .deb). */
export function linuxArtefacts(paths) {
  return paths.filter((file) => LINUX_ARTEFACT.test(file));
}

/** electron-builder's afterAllArtifactBuild: sums next to the Linux artefacts, if any. */
export function linuxChecksums(buildResult) {
  const written = writeSums(linuxArtefacts(buildResult.artifactPaths ?? []));
  if (written === null) return [];
  process.stdout.write(`[checksums] ${written}\n`);
  return [written];
}

/** Files in `sumsFile` whose digest differs or which are missing (empty when all match). */
export function checkSums(sumsFile) {
  const dir = path.dirname(path.resolve(sumsFile));
  const problems = [];
  for (const { digest, name } of parseSums(fs.readFileSync(sumsFile, "utf8"))) {
    const file = path.join(dir, name);
    if (!fs.existsSync(file)) problems.push(`${name}: missing`);
    else if (fileDigest(file) !== digest) problems.push(`${name}: digest differs`);
  }
  return problems;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [command, target] = process.argv.slice(2);
  try {
    if (command === "write" && target) {
      const files = fs.readdirSync(target).map((name) => path.join(target, name));
      const written = writeSums(linuxArtefacts(files));
      process.stdout.write(`${written ?? "no Linux artefacts"}\n`);
    } else if (command === "check" && target) {
      const problems = checkSums(target);
      for (const problem of problems) process.stderr.write(`${problem}\n`);
      process.exit(problems.length === 0 ? 0 : 1);
    } else {
      process.stderr.write("usage: checksums.mjs write DIR | check FILE\n");
      process.exit(2);
    }
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exit(1);
  }
}
