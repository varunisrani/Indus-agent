#!/usr/bin/env node
// Writes the offline slash catalogue (`src/chat/slashCatalog.json`) from the Python command
// registry (`src/indus/service/commands/registry.py`): every built-in with its aliases, one-line
// description, category, argument hint and whether the registry offers it on the desktop surface.
// The desktop uses it when the daemon cannot be reached, and its `desktop: false` rows are the
// known list the slash curation hides (P20-24).
//
//   node scripts/dump-slash-catalog.mjs           regenerate
//   node scripts/dump-slash-catalog.mjs --check   exit 1 when the committed file is stale

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const desktop = path.resolve(here, "..");
const repo = path.resolve(desktop, "..", "..");
const target = path.join(desktop, "src", "chat", "slashCatalog.json");

const PROGRAM = `
import json
from indus.service.commands.registry import REGISTRY
rows = [
    {
        "name": c.name,
        "aliases": list(c.aliases),
        "description": c.description,
        "category": c.category,
        "argsHint": c.args_hint,
        "desktop": c.offered_on("desktop"),
    }
    for c in REGISTRY
]
print(json.dumps(rows))
`;

/** Pretty JSON exactly as the repository formatter (Biome) leaves it. */
export function renderCatalog(rows) {
  const body = {
    source: "src/indus/service/commands/registry.py via scripts/dump-slash-catalog.mjs",
    commands: rows,
  };
  // Biome keeps arrays of plain strings on one line when they fit.
  return `${JSON.stringify(body, null, 2).replace(
    /\[\n\s*((?:"[^"\n]*",?\s*)*)\]/g,
    (_, items) => `[${items.trim().split(/,\s*/).filter(Boolean).join(", ")}]`,
  )}\n`;
}

function readRegistry() {
  const out = execFileSync("uv", ["run", "--frozen", "--quiet", "python", "-c", PROGRAM], {
    cwd: repo,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "inherit"],
  });
  return JSON.parse(out.trim().split("\n").at(-1) ?? "[]");
}

function main() {
  const text = renderCatalog(readRegistry());
  if (process.argv.includes("--check")) {
    const current = fs.existsSync(target) ? fs.readFileSync(target, "utf8") : "";
    if (current !== text) {
      process.stderr.write(
        `${path.relative(repo, target)} is stale: run node apps/desktop/scripts/dump-slash-catalog.mjs\n`,
      );
      process.exit(1);
    }
    process.stdout.write("slash catalogue up to date\n");
    return;
  }
  fs.writeFileSync(target, text);
  process.stdout.write(`wrote ${path.relative(repo, target)}\n`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
