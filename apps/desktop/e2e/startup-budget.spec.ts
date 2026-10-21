// P25-13: desktop cold start to interactive chat within `desktop.cold_start.limit_ms` of
// tooling/perf/budgets.toml, on the packaged build against the fake backend. Each launch gets a
// fresh user-data folder and home (a cold start for the app); the median of three launches is
// compared with the budget. Needs the unsigned `--dir` package CI's desktop-package job builds
// (`build/release/linux-unpacked`); without it the spec skips and says why. Run in the e2e lane
// (`pnpm --filter @indus/desktop e2e`), one file at a time.

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { readBudgets } from "../../../tooling/perf/read-budgets.mjs";
import { displayWrapper, rootSandboxArgs } from "../testing/electron/binary.ts";
import { appPage, Cdp } from "../testing/electron/cdp.ts";
import { cleanupFakes } from "../testing/fakeIndus.ts";
import {
  APP_ROOT,
  CONNECTED,
  cleanEnv,
  skipReason as e2eSkip,
  fakeBackend,
  freePort,
  HEALTHY,
  tempDir,
} from "./fixtures.ts";

const BINARY = path.join(APP_ROOT, "build", "release", "linux-unpacked", "indus-desktop");
const LAUNCHES = 3;
const budget = (readBudgets().desktop as unknown as { cold_start: { limit_ms: number } })
  .cold_start;
const skipReason =
  e2eSkip !== ""
    ? e2eSkip
    : process.platform !== "linux"
      ? "the budget is measured on the Linux package here (macOS/Windows: the release lane)"
      : fs.existsSync(BINARY)
        ? ""
        : "no packaged build (CI's desktop-package job makes build/release/linux-unpacked)";

afterAll(() => cleanupFakes());

/** Milliseconds from spawning the packaged app to a connected window with a usable composer. */
async function coldStart(): Promise<number> {
  const work = tempDir("indus-e2e-start-");
  const fake = fakeBackend(work, HEALTHY);
  const port = await freePort();
  const display = displayWrapper();
  const [command, ...args] = [
    ...(display?.command ?? []),
    BINARY,
    ...rootSandboxArgs(),
    "--disable-gpu",
    `--remote-debugging-port=${port}`,
  ];
  const started = performance.now();
  const child = spawn(command as string, args, {
    cwd: work,
    detached: true,
    stdio: "ignore",
    env: cleanEnv({
      HOME: path.join(work, "user-home"),
      INDUS_HOME: fake.home,
      INDUS_DESKTOP_INDUS: fake.indus,
      INDUS_DESKTOP_USER_DATA_DIR: path.join(work, "user-data"),
    }),
  });
  const gone = new Promise<void>((resolve) => child.once("exit", () => resolve()));
  let cdp: Cdp | null = null;
  try {
    cdp = await Cdp.open(await appPage(port, 30_000));
    await cdp.send("Runtime.enable");
    await cdp.waitFor(CONNECTED, "the connection", 30_000);
    await cdp.waitFor(
      `!!document.querySelector('textarea:not([disabled])')`,
      "the composer",
      30_000,
    );
    return performance.now() - started;
  } finally {
    cdp?.close();
    if (child.pid !== undefined && child.exitCode === null) {
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch {
        // already gone
      }
      await Promise.race([gone, new Promise((resolve) => setTimeout(resolve, 10_000))]);
    }
  }
}

describe.skipIf(skipReason !== "")(
  `desktop startup budget${skipReason ? ` (skipped: ${skipReason})` : ""}`,
  () => {
    it("reaches an interactive chat within the cold-start budget", async () => {
      const samples: number[] = [];
      for (let i = 0; i < LAUNCHES; i += 1) samples.push(await coldStart());
      const median = [...samples].sort((a, b) => a - b)[Math.floor(LAUNCHES / 2)] as number;
      process.stdout.write(
        `PERF desktop.cold_start: median ${median.toFixed(0)} ms of [${samples.map((s) => s.toFixed(0)).join(", ")}]; budget ${budget.limit_ms} ms\n`,
      );
      expect(median).toBeLessThanOrEqual(budget.limit_ms);
    }, 180_000);
  },
);
