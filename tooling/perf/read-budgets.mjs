// Reader for tooling/perf/budgets.toml used by the TypeScript perf checks (P25-13).
// Parses the subset the file keeps to: `[table]` / `[table.sub]` headers, `key = number` and
// `key = "string"` lines, `#` comments. Anything else is an error, so the file cannot drift into
// syntax only the Python reader understands (tests/perf pins the subset too).

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

export const BUDGETS_PATH = fileURLToPath(new URL("./budgets.toml", import.meta.url));

const HEADER = /^\[([a-z_]+(?:\.[a-z0-9_]+)?)\]$/;
const PAIR = /^([a-z_0-9]+) = (?:(-?[0-9.]+)|"([^"\\]*)")$/;

export function parseBudgets(text) {
  const out = {};
  let table = null;
  text.split(/\r?\n/).forEach((raw, index) => {
    const line = raw.trim();
    if (line === "" || line.startsWith("#")) return;
    const header = HEADER.exec(line);
    if (header) {
      table = out;
      for (const part of header[1].split(".")) {
        table[part] ??= {};
        table = table[part];
      }
      return;
    }
    const pair = PAIR.exec(line);
    if (!pair || table === null) {
      throw new Error(`budgets.toml:${index + 1}: outside the supported subset: ${line}`);
    }
    table[pair[1]] = pair[2] !== undefined ? Number(pair[2]) : pair[3];
  });
  return out;
}

export function readBudgets(path = BUDGETS_PATH) {
  return parseBudgets(readFileSync(path, "utf8"));
}
