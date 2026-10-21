// Test helpers for main-process backend tests: temp homes and a fake `indus` program that runs
// the P20-01 fake backend (`fakeBackend/ensure.mjs`) under this Node, plus cleanup of the fake
// daemons and serves a test started. POSIX only (the fake daemon uses a Unix control socket).

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const FAKE_ENSURE = path.join(HERE, "fakeBackend", "ensure.mjs");

const created: string[] = [];

/** A fresh private temp directory, removed by `cleanupFakes`. */
export function tempDir(prefix = "indus-m-"): string {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), prefix)));
  fs.chmodSync(dir, 0o700);
  created.push(dir);
  return dir;
}

/**
 * Write an executable `indus` at `file` that runs the fake backend. `scenario` pins
 * INDUS_FAKE_SCENARIO for this program (else the caller's env decides).
 */
export function writeFakeIndus(file: string, scenario?: string): string {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const pin = scenario === undefined ? "" : `INDUS_FAKE_SCENARIO=${JSON.stringify(scenario)} `;
  fs.writeFileSync(
    file,
    `#!/bin/sh\n${pin}exec ${JSON.stringify(process.execPath)} ${JSON.stringify(FAKE_ENSURE)} "$@"\n`,
    { mode: 0o755 },
  );
  return file;
}

/** An executable that prints `text` and exits with `code`. */
export function writeScript(file: string, body: string): string {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  return file;
}

function killRecorded(dir: string, depth = 0): void {
  let entries: fs.Dirent[] = [];
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory() && depth < 4) killRecorded(full, depth + 1);
    else if (entry.name === "fake-daemon.json" || entry.name === "fake-serve.json") {
      try {
        const state = JSON.parse(fs.readFileSync(full, "utf8")) as { pid: number };
        process.kill(state.pid, "SIGKILL");
      } catch {
        // not running
      }
    }
  }
}

/** Kill every fake daemon/serve recorded under the temp dirs and remove the dirs. */
export function cleanupFakes(): void {
  for (const dir of created.splice(0)) {
    killRecorded(dir);
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

export const posix = process.platform !== "win32";
