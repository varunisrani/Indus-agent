#!/usr/bin/env node
// Web dashboard bundle budget (P25-13), checked on a production build:
//   node apps/web/scripts/check-bundle.mjs [dist dir]      (default: apps/web/dist)
// The initial JS (the module scripts and modulepreloads index.html loads) must stay within
// `web.initial_js.limit_kb_gzip` in total, and every other JS chunk (routes, lazy pages) within
// `web.route_chunk.limit_kb_gzip`; both numbers come from tooling/perf/budgets.toml. Sizes are
// gzip level 9, 1 kB = 1,000 bytes. Exit 1 names every file over its budget; exit 2 when there is
// no build to check.

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import { readBudgets } from "../../../tooling/perf/read-budgets.mjs";

const HERE = fileURLToPath(new URL(".", import.meta.url));
export const DEFAULT_DIST = resolve(HERE, "..", "dist");

const SCRIPT = /<script\b[^>]*\btype=["']module["'][^>]*\bsrc=["']([^"']+)["'][^>]*>/gi;
const PRELOAD = /<link\b[^>]*\brel=["']modulepreload["'][^>]*\bhref=["']([^"']+)["'][^>]*>/gi;

export function gzipKb(bytes) {
  return gzipSync(bytes, { level: 9 }).byteLength / 1000;
}

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...walk(path));
    else out.push(path);
  }
  return out;
}

/** The dist-relative JS files index.html loads up front (base path prefixes ignored). */
export function initialFiles(html, jsFiles) {
  const wanted = [];
  for (const re of [SCRIPT, PRELOAD]) {
    for (const match of html.matchAll(re)) wanted.push(match[1].split(/[?#]/)[0]);
  }
  const found = new Set();
  for (const href of wanted) {
    if (/^[a-z]+:\/\//i.test(href)) continue; // never ours to size
    const hit = jsFiles.find((file) => href.endsWith(`/${file}`) || href === file);
    if (hit !== undefined) found.add(hit);
  }
  return [...found].sort();
}

export function checkBundle(dist = DEFAULT_DIST, budgets = readBudgets()) {
  const index = join(dist, "index.html");
  if (!existsSync(index)) {
    return { setup: `no build at ${dist} (run \`pnpm --filter @indus/web build\` first)` };
  }
  const initialLimit = Number(budgets.web.initial_js.limit_kb_gzip);
  const chunkLimit = Number(budgets.web.route_chunk.limit_kb_gzip);
  const js = walk(dist)
    .filter((path) => /\.(m?js)$/.test(path))
    .map((path) => relative(dist, path).split(sep).join("/"));
  const initial = initialFiles(readFileSync(index, "utf8"), js);
  const sizes = Object.fromEntries(
    js.map((file) => [file, gzipKb(readFileSync(join(dist, file)))]),
  );
  const initialKb = initial.reduce((sum, file) => sum + sizes[file], 0);
  const problems = [];
  if (initial.length === 0) problems.push("index.html loads no module script from the build");
  if (initialKb > initialLimit) {
    problems.push(
      `initial JS ${initialKb.toFixed(1)} kB gzip is over the ${initialLimit} kB budget (web.initial_js): ${initial.join(", ")}`,
    );
  }
  const routes = js.filter((file) => !initial.includes(file)).sort();
  for (const file of routes) {
    if (sizes[file] > chunkLimit) {
      problems.push(
        `${file}: ${sizes[file].toFixed(1)} kB gzip is over the ${chunkLimit} kB chunk budget (web.route_chunk)`,
      );
    }
  }
  return { initial, initialKb, routes, sizes, problems };
}

function main(argv) {
  const dist = resolve(argv[0] ?? DEFAULT_DIST);
  const result = checkBundle(dist);
  if (result.setup) {
    process.stderr.write(`check-bundle: ${result.setup}\n`);
    return 2;
  }
  process.stdout.write(
    `initial JS ${result.initialKb.toFixed(1)} kB gzip (${result.initial.length} files); ` +
      `${result.routes.length} other chunks, largest ${Math.max(0, ...result.routes.map((f) => result.sizes[f])).toFixed(1)} kB gzip\n`,
  );
  for (const problem of result.problems) process.stderr.write(`check-bundle: ${problem}\n`);
  return result.problems.length === 0 ? 0 : 1;
}

if (process.argv[1] !== undefined && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv.slice(2));
}
