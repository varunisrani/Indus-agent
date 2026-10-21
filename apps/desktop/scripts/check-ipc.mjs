// Static check (P20-03, D-16.18): no IPC handler may bypass the registry. Every `.ts` / `.mjs`
// file under `electron/` except the registry itself and tests is scanned for direct listener
// registration on `ipcMain` or on a web contents' own `ipc` object, and for `ipcMain` being passed
// around under another name. Also checks every name in the channel table starts with `indus:`.
//
//   node scripts/check-ipc.mjs [--root DIR]     exit 0 clean, 1 with one line per finding

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const REGISTRY = path.join("electron", "ipc", "registry.ts");
/** main.ts may name `ipcMain` only to import it and hand it to the registry. */
const MAIN = path.join("electron", "main.ts");

const LISTEN_METHODS =
  "handle|handleOnce|on|once|addListener|prependListener|prependOnceListener|removeHandler";
const RULES = [
  {
    pattern: new RegExp(`\\bipcMain\\s*(?:\\?\\.|\\.)\\s*(?:${LISTEN_METHODS})\\b`),
    why: "raw ipcMain listener",
  },
  { pattern: /\bipcMain\s*\[/, why: "ipcMain accessed by index" },
  {
    pattern: /\{[^}]*\b(?:handle|on|once|handleOnce)\b[^}]*\}\s*=\s*ipcMain\b/,
    why: "ipcMain destructured",
  },
  {
    pattern: new RegExp(`\\.ipc\\s*(?:\\?\\.|\\.)\\s*(?:${LISTEN_METHODS})\\b`),
    why: "raw webContents.ipc listener",
  },
];

function isScanned(rel) {
  if (!/\.(?:ts|mts|mjs|js|cjs)$/.test(rel)) return false;
  if (/\.test\.[cm]?[jt]s$/.test(rel)) return false;
  return rel !== REGISTRY;
}

function walk(dir, out) {
  let entries = [];
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const entry of entries) {
    if (entry.name === "node_modules") continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, out);
    else out.push(full);
  }
  return out;
}

/** Strip `//` and block comments so prose about ipcMain does not count. */
function code(text) {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "))
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
}

/** Findings for the app at `root` (default: this app). */
export function scan(root = APP_DIR) {
  const findings = [];
  for (const file of walk(path.join(root, "electron"), [])) {
    const rel = path.relative(root, file);
    if (!isScanned(rel)) continue;
    const lines = code(fs.readFileSync(file, "utf8")).split("\n");
    lines.forEach((line, index) => {
      for (const rule of RULES) {
        if (rule.pattern.test(line)) findings.push(`${rel}:${index + 1}: ${rule.why}`);
      }
      if (rel !== MAIN && /\bipcMain\b/.test(line) && !/^\s*import\s+type\b/.test(line)) {
        if (!findings.some((f) => f.startsWith(`${rel}:${index + 1}:`))) {
          findings.push(`${rel}:${index + 1}: ipcMain used outside the registry`);
        }
      }
    });
  }
  const table = path.join(root, "electron", "ipc", "channels.ts");
  if (fs.existsSync(table)) {
    const names = [...fs.readFileSync(table, "utf8").matchAll(/name:\s*"([^"]+)"/g)].map(
      (m) => m[1],
    );
    for (const name of names)
      if (!name.startsWith("indus:")) findings.push(`channels.ts: ${name}: must start with indus:`);
  }
  return findings;
}

function main(argv) {
  const at = argv.indexOf("--root");
  const root = at >= 0 ? path.resolve(argv[at + 1] ?? ".") : APP_DIR;
  const findings = scan(root);
  for (const finding of findings) process.stderr.write(`check-ipc: ${finding}\n`);
  if (findings.length === 0)
    process.stdout.write("check-ipc: every IPC handler goes through the registry\n");
  return findings.length === 0 ? 0 : 1;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  process.exitCode = main(process.argv.slice(2));
}
