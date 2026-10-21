import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { cleanupFakes, tempDir } from "../../testing/fakeIndus.ts";
import { MachineryGate, NotLockHolderError } from "../singleInstance.ts";
import { LEDGER_FILE, OrphanLedger, type ProcessProbe, processStartTime } from "./orphans.ts";

afterAll(cleanupFakes);

const holder = new MachineryGate(true);

function ledger(probe?: ProcessProbe, gate = holder) {
  const userData = tempDir();
  const lines: string[] = [];
  const instance = new OrphanLedger({
    userData,
    ...(probe === undefined ? {} : { probe }),
    guard: (what, fn) => gate.guard(what, fn),
    log: (line) => lines.push(line),
    sleep: async () => {},
  });
  return { instance, userData, lines };
}

describe("orphan ledger", () => {
  it("a recorded process whose pid and start time still match is reaped", async () => {
    const kills: Array<[number, string]> = [];
    let alive = true;
    const probe: ProcessProbe = {
      startTime: (pid) => (pid === 4242 && alive ? "t-100" : null),
      kill: (pid, signal) => {
        kills.push([pid, signal]);
        alive = false;
      },
    };
    const { instance } = ledger(probe);
    expect(instance.claim(4242, "installer-host")).toMatchObject({ pid: 4242, startTime: "t-100" });
    expect(await instance.reapOrphans()).toEqual({ killed: [4242], spared: [] });
    expect(kills).toEqual([[4242, "SIGTERM"]]);
    expect(instance.entries()).toEqual([]);
  });

  it("a reused pid (start time differs) is not killed, and the entry is dropped", async () => {
    const kills: number[] = [];
    let now = "t-100";
    const probe: ProcessProbe = {
      startTime: () => now,
      kill: (pid) => kills.push(pid),
    };
    const { instance } = ledger(probe);
    instance.claim(4242, "installer-host");
    now = "t-999"; // the OS gave pid 4242 to someone else
    expect(await instance.reapOrphans()).toEqual({ killed: [], spared: [4242] });
    expect(kills).toEqual([]);
    expect(instance.entries()).toEqual([]);
  });

  it("released processes are not reaped; one that ignores SIGTERM gets SIGKILL", async () => {
    const kills: string[] = [];
    const { instance } = ledger({
      startTime: () => "t",
      kill: (pid, signal) => kills.push(`${pid}:${signal}`),
    });
    instance.claim(10, "installer-host");
    instance.claim(11, "installer-host");
    instance.release(10);
    expect(instance.entries().map((e) => e.pid)).toEqual([11]);
    expect((await instance.reapOrphans()).killed).toEqual([11]);
    expect(kills).toEqual(["11:SIGTERM", "11:SIGKILL"]);
  });

  it("a corrupt ledger is moved aside and nothing is killed", async () => {
    const kills: number[] = [];
    const { instance, userData, lines } = ledger({
      startTime: () => "t",
      kill: (pid) => kills.push(pid),
    });
    fs.writeFileSync(path.join(userData, LEDGER_FILE), "{ not json");
    expect(await instance.reapOrphans()).toEqual({ killed: [], spared: [] });
    expect(kills).toEqual([]);
    expect(
      fs.readdirSync(userData).some((name) => name.startsWith(`${LEDGER_FILE}.corrupt-`)),
    ).toBe(true);
    expect(lines.join("\n")).toContain("unreadable");
  });

  it("a lock loser may neither claim nor reap", async () => {
    const { instance } = ledger({ startTime: () => "t", kill: () => {} }, new MachineryGate(false));
    expect(() => instance.claim(1, "x")).toThrow(NotLockHolderError);
    expect(() => instance.reapOrphans()).toThrow(NotLockHolderError);
  });

  it.skipIf(process.platform !== "linux" && process.platform !== "darwin")(
    "a real child: identity read from the OS, reaped on the next start",
    async () => {
      const child = spawn(process.execPath, ["-e", "setTimeout(() => {}, 60000)"], {
        stdio: "ignore",
      });
      const pid = child.pid as number;
      const exited = new Promise((resolve) => child.once("exit", resolve));
      expect(processStartTime(pid)).not.toBeNull();
      const { instance } = ledger();
      instance.claim(pid, "installer-host");
      const { killed } = await instance.reapOrphans();
      expect(killed).toEqual([pid]);
      await exited;
      expect(processStartTime(pid)).toBeNull();
    },
  );
});
