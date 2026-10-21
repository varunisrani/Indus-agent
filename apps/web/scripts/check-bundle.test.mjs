// @vitest-environment node
// P25-13: the bundle budget check on synthetic builds (budgets from tooling/perf/budgets.toml).

import { randomBytes } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { readBudgets } from "../../../tooling/perf/read-budgets.mjs";
import { checkBundle, initialFiles } from "./check-bundle.mjs";

const dirs = [];
afterEach(() => {
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

/** Incompressible JS of roughly `kb` kB gzip. */
const noise = (kb) =>
  `export const x = "${randomBytes(Math.round(kb * 1000)).toString("base64")}";\n`;

function build(files, html) {
  const dist = mkdtempSync(join(tmpdir(), "indus-bundle-"));
  dirs.push(dist);
  mkdirSync(join(dist, "assets"));
  for (const [name, text] of Object.entries(files)) writeFileSync(join(dist, "assets", name), text);
  writeFileSync(join(dist, "index.html"), html);
  return dist;
}

const HTML = `<!doctype html><html><head>
<script type="module" crossorigin src="/indus/assets/index-a1.js"></script>
<link rel="modulepreload" crossorigin href="/indus/assets/react-b2.js">
</head><body></body></html>`;

describe("check-bundle", () => {
  it("reads both budgets from the budgets file", () => {
    const budgets = readBudgets();
    expect(budgets.web.initial_js.limit_kb_gzip).toBe(350);
    expect(budgets.web.route_chunk.limit_kb_gzip).toBe(200);
  });

  it("counts the entry script and its modulepreloads as initial, under a base path", () => {
    const files = ["assets/index-a1.js", "assets/react-b2.js", "assets/SessionsPage-c3.js"];
    expect(initialFiles(HTML, files)).toEqual(["assets/index-a1.js", "assets/react-b2.js"]);
  });

  it("passes a build within both budgets", () => {
    const dist = build(
      { "index-a1.js": noise(40), "react-b2.js": noise(60), "SessionsPage-c3.js": noise(20) },
      HTML,
    );
    const result = checkBundle(dist);
    expect(result.problems).toEqual([]);
    expect(result.routes).toEqual(["assets/SessionsPage-c3.js"]);
  });

  it("fails an oversized route chunk and names it", () => {
    const dist = build(
      { "index-a1.js": noise(40), "react-b2.js": noise(60), "ChatPage-d4.js": noise(230) },
      HTML,
    );
    const { problems } = checkBundle(dist);
    expect(problems).toHaveLength(1);
    expect(problems[0]).toMatch(/^assets\/ChatPage-d4\.js: \d+\.\d kB gzip is over the 200 kB/);
  });

  it("fails initial JS over its budget even when every file is small", () => {
    const dist = build({ "index-a1.js": noise(190), "react-b2.js": noise(190) }, HTML);
    const { problems } = checkBundle(dist);
    expect(problems).toHaveLength(1);
    expect(problems[0]).toMatch(/^initial JS \d+\.\d kB gzip is over the 350 kB budget/);
  });

  it("reports a missing build as a setup problem", () => {
    const empty = mkdtempSync(join(tmpdir(), "indus-bundle-"));
    dirs.push(empty);
    expect(checkBundle(empty).setup).toMatch(/^no build at /);
  });
});
