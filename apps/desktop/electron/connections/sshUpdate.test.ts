import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";
import { type SpawnLike, SshConnection } from "./ssh.ts";
import { MAX_RECOVERY_ATTEMPTS, SshUpdate, UpdateRefused } from "./sshUpdate.ts";

const FAKE_SSH = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../../tests/fakes/ssh_bin/ssh",
);
const viaSh: SpawnLike = (_command, args, options) =>
  spawn("/bin/sh", [FAKE_SSH, ...args], options);
const dirs: string[] = [];
afterAll(() => {
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
});

/** A fake host: HOME is a temp dir, `indus` a script whose `update` bumps the version. */
function host() {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "indus-sshup-"));
  dirs.push(home);
  const bin = path.join(home, "bin");
  fs.mkdirSync(bin);
  fs.writeFileSync(path.join(home, "version"), "indus 0.23.0\n");
  fs.writeFileSync(
    path.join(bin, "indus"),
    [
      "#!/bin/sh",
      'case "$1" in',
      '  --version) cat "$HOME/version" ;;',
      '  update) while [ -f "$HOME/hold" ]; do sleep 0.05; done; echo "indus 0.24.0" > "$HOME/version"; echo updated ;;',
      "esac",
      "",
    ].join("\n"),
    { mode: 0o755 },
  );
  const log = path.join(home, "ssh.log");
  const conn = new SshConnection(
    { host: "box.example" },
    {
      spawn: viaSh,
      env: {
        ...process.env,
        HOME: home,
        PATH: `${bin}:${process.env.PATH ?? ""}`,
        INDUS_FAKE_SSH_LOG: log,
      },
      controlDir: path.join(home, "ctl"),
      fallbackDir: path.join(home, "f"),
      log: () => {},
    },
  );
  const userData = path.join(home, "userData");
  const launches = () =>
    fs.existsSync(log)
      ? fs
          .readFileSync(log, "utf8")
          .split("\n")
          .filter((l) => l.includes("nohup")).length
      : 0;
  return { home, conn, userData, launches };
}

describe.skipIf(process.platform === "win32")("managed SSH updates (fake ssh)", () => {
  it("runs drain → update → restore and removes the record", async () => {
    const h = host();
    const steps: string[] = [];
    const update = new SshUpdate("box", {
      userData: h.userData,
      remote: h.conn,
      drain: async () => void steps.push("drain"),
      restore: async () => void steps.push("restore"),
      log: () => {},
      pollMs: 20,
    });
    const result = await update.run();
    expect(result).toMatchObject({
      outcome: "updated",
      exitCode: 0,
      preVersion: "indus 0.23.0",
      postVersion: "indus 0.24.0",
    });
    expect(steps).toEqual(["drain", "restore"]);
    expect(fs.existsSync(update.recordPath())).toBe(false);
    expect(h.launches()).toBe(1);
  });

  it("an interrupted update resumes from the record", async () => {
    const h = host();
    fs.writeFileSync(path.join(h.home, "hold"), "");
    const phases: string[] = [];
    let polls = 0;
    const first = new SshUpdate("box", {
      userData: h.userData,
      remote: h.conn,
      drain: async () => {
        phases.push(String(first.readRecord()?.phase));
      },
      restore: async () => undefined,
      log: () => {},
      sleep: async () => {
        polls += 1;
        throw new Error("the app quit"); // mid-update
      },
    });
    await expect(first.run()).rejects.toThrow("the app quit");
    expect(phases).toEqual(["draining"]); // written before the drain began
    const record = first.readRecord();
    expect(record).toMatchObject({ phase: "updating", attempts: 0, preVersion: "indus 0.23.0" });
    expect(polls).toBe(1);

    fs.rmSync(path.join(h.home, "hold")); // the detached run goes on without the app
    const restored: string[] = [];
    const again = new SshUpdate("box", {
      userData: h.userData,
      remote: h.conn,
      drain: async () => {
        throw new Error("recovery never drains again");
      },
      restore: async () => void restored.push("restore"),
      log: () => {},
      pollMs: 20,
    });
    await expect(again.run()).rejects.toBeInstanceOf(UpdateRefused); // no second run meanwhile
    const result = await again.recover();
    expect(result).toMatchObject({
      outcome: "updated",
      correlationId: record?.correlationId,
      postVersion: "indus 0.24.0",
    });
    expect(restored).toEqual(["restore"]);
    expect(h.launches()).toBe(1); // the update was never launched twice
    expect(fs.existsSync(again.recordPath())).toBe(false);
  });

  it("gives up after three recovery attempts and stops fencing the host", async () => {
    const h = host();
    const update = new SshUpdate("box", {
      userData: h.userData,
      remote: h.conn,
      drain: async () => undefined,
      restore: async () => {
        throw new Error("serve will not start");
      },
      log: () => {},
      pollMs: 5,
    });
    fs.mkdirSync(path.dirname(update.recordPath()), { recursive: true });
    fs.writeFileSync(
      update.recordPath(),
      JSON.stringify({
        v: 1,
        connectionId: "box",
        correlationId: "0d1f8a5e-3c2b-4e7a-9f10-123456789abc",
        phase: "restoring",
        attempts: 0,
        startedAt: 1,
        updatedAt: 1,
      }),
    );
    for (let i = 0; i < MAX_RECOVERY_ATTEMPTS; i += 1) {
      expect((await update.recover()).outcome).toBe("restore-failed");
    }
    expect((await update.recover()).outcome).toBe("given-up");
    expect(fs.existsSync(update.recordPath())).toBe(false);
    expect(fs.existsSync(`${update.recordPath()}.given-up`)).toBe(true);
    expect((await update.recover()).outcome).toBe("nothing-to-recover");
  });

  it("a malformed record is set aside, and a host without Indus needs an install command", async () => {
    const h = host();
    const update = new SshUpdate("box", {
      userData: h.userData,
      remote: h.conn,
      drain: async () => undefined,
      restore: async () => undefined,
      log: () => {},
    });
    fs.mkdirSync(path.dirname(update.recordPath()), { recursive: true });
    fs.writeFileSync(
      update.recordPath(),
      JSON.stringify({ v: 1, connectionId: "other", phase: "x" }),
    );
    expect(update.readRecord()).toBeNull();
    expect(fs.existsSync(`${update.recordPath()}.bad`)).toBe(true);
    fs.rmSync(path.join(h.home, "bin", "indus"));
    await expect(update.run()).rejects.toThrow(/not installed/);
  });
});
