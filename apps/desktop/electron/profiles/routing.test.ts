import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MainContext } from "../context.ts";
import { SCHEMAS } from "../ipc/schemas.ts";
import {
  type BackendPort,
  decideLaunch,
  installProfiles,
  normalizeProfile,
  PROFILE_STATE_FILE,
  ProfileRouting,
  profileFromArgv,
  readStoredProfiles,
} from "./routing.ts";

const dirs: string[] = [];

function tempDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-profiles-"));
  dirs.push(dir);
  return dir;
}

afterEach(() => {
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

describe("launch decision", () => {
  it.each([
    [["--profile", "work"], { active: "home", defaultProfile: "x" }, "work", "flag"],
    [["--profile=Work"], { active: null, defaultProfile: null }, "work", "flag"],
    [["-p", "client"], { active: "home", defaultProfile: null }, "client", "flag"],
    [[], { active: "home", defaultProfile: "x" }, "home", "remembered"],
    [[], { active: null, defaultProfile: "x" }, "x", "default-profile"],
    [[], { active: null, defaultProfile: null }, "default", "fallback"],
    [["--", "--profile", "work"], { active: null, defaultProfile: null }, "default", "fallback"],
    [["--profile", "Bad Name"], { active: "home", defaultProfile: null }, "home", "remembered"],
  ] as const)("%j with %j → %s (%s)", (argv, stored, profile, source) => {
    expect(decideLaunch(argv, stored)).toEqual({ profile, source });
  });

  it("names follow the backend's rule", () => {
    expect(normalizeProfile(" Default ")).toBe("default");
    expect(normalizeProfile("a_b-1")).toBe("a_b-1");
    expect(normalizeProfile("-x")).toBeNull();
    expect(normalizeProfile("x".repeat(65))).toBeNull();
    expect(normalizeProfile(3)).toBeNull();
    expect(profileFromArgv(["--inspect", "-p"])).toBeNull();
  });

  it("one decision per attempt: a remember during the attempt applies to the next", () => {
    const userData = tempDir();
    const routing = new ProfileRouting({ userData, argv: [], log: () => undefined });
    expect(routing.launch()).toEqual({ profile: "default", source: "fallback", attempt: 1 });
    routing.setActive("work");
    expect(routing.launch().profile).toBe("default");
    expect(routing.state()).toEqual({ active: "work", defaultProfile: null, launch: "default" });
    expect(routing.beginAttempt()).toEqual({ profile: "work", source: "remembered", attempt: 2 });
    // and it survives a restart of main
    const again = new ProfileRouting({ userData, argv: [], log: () => undefined });
    expect(again.launch().profile).toBe("work");
  });

  it("a corrupt or foreign state file reads as nothing remembered", () => {
    const userData = tempDir();
    const file = path.join(userData, PROFILE_STATE_FILE);
    fs.writeFileSync(file, "{nope");
    expect(readStoredProfiles(file)).toEqual({ active: null, defaultProfile: null });
    fs.writeFileSync(file, JSON.stringify({ version: 2, active: "x" }));
    expect(readStoredProfiles(file)).toEqual({ active: null, defaultProfile: null });
    fs.writeFileSync(file, JSON.stringify({ version: 1, active: "../etc", defaultProfile: "ok" }));
    expect(readStoredProfiles(file)).toEqual({ active: null, defaultProfile: "ok" });
  });
});

describe("rename and delete follow-up", () => {
  it("rename moves the remembered names and the running decision", () => {
    const routing = new ProfileRouting({ userData: tempDir(), argv: [], log: () => undefined });
    routing.setActive("work");
    routing.setDefault("work");
    routing.beginAttempt();
    expect(routing.commitChange("rename", "work", "client")).toEqual({
      active: "client",
      defaultProfile: "client",
      launch: "client",
    });
  });

  it("delete clears them and the next attempt falls back", () => {
    const routing = new ProfileRouting({ userData: tempDir(), argv: [], log: () => undefined });
    routing.setActive("work");
    routing.beginAttempt();
    expect(routing.commitChange("delete", "work")).toEqual({
      active: null,
      defaultProfile: null,
      launch: "default",
    });
  });

  it("refuses a rename without a new name and a bad name", () => {
    const routing = new ProfileRouting({ userData: tempDir(), argv: [], log: () => undefined });
    expect(() => routing.commitChange("rename", "work")).toThrow(/new name/);
    expect(() => routing.setActive("../x")).toThrow(/not a profile name/);
  });
});

type Handler = (payload: never, meta: { senderId: number }) => unknown;

function fakeContext(userData: string) {
  const handlers = new Map<string, Handler>();
  const emitted: Array<{ name: string; payload: unknown }> = [];
  const lines: string[] = [];
  const context = {
    registry: {
      handle: (name: string, handler: Handler) => handlers.set(name, handler),
      emit: (_target: unknown, name: string, payload: unknown) => emitted.push({ name, payload }),
    },
    windows: { windows: () => [{ webContents: { id: 1 } }, { webContents: { id: 2 } }] },
    log: { write: (line: string) => lines.push(line) },
    paths: { userData },
  } as unknown as MainContext;
  const call = (name: string, payload: unknown) =>
    (handlers.get(name) as (payload: unknown, meta: { senderId: number }) => unknown)(payload, {
      senderId: 1,
    });
  return { context, handlers, emitted, lines, call };
}

describe("installProfiles", () => {
  it("registers every profile channel the table gives P20-26", () => {
    const { context, handlers } = fakeContext(tempDir());
    installProfiles(context, { argv: [] });
    const invokes = Object.keys(SCHEMAS).filter(
      (name) => name.startsWith("indus:profile:") && name !== "indus:profile:changed",
    );
    expect([...handlers.keys()].sort()).toEqual(invokes.sort());
  });

  it("set tells every window", () => {
    const { context, emitted, call } = fakeContext(tempDir());
    installProfiles(context, { argv: [] });
    call("indus:profile:set", { profile: "work" });
    expect(emitted).toHaveLength(2);
    expect(emitted[0]).toEqual({
      name: "indus:profile:changed",
      payload: { active: "work", defaultProfile: null, launch: "default" },
    });
  });

  it("prepare leaves the profile main is attached to (re-homes to default first)", async () => {
    const { context, call } = fakeContext(tempDir());
    const backend: BackendPort = {
      attachedProfile: () => "work",
      rehome: vi.fn(async () => undefined),
    };
    installProfiles(context, { argv: [], backend: () => backend });
    await expect(
      call("indus:profile:prepare-change", { action: "rename", profile: "work" }),
    ).resolves.toEqual({ ok: true });
    expect(backend.rehome).toHaveBeenCalledWith("default");
    await call("indus:profile:prepare-change", { action: "delete", profile: "other" });
    expect(backend.rehome).toHaveBeenCalledTimes(1);
  });

  it("prepare refuses the default profile and reports a re-home that fails", async () => {
    const { context, call } = fakeContext(tempDir());
    installProfiles(context, {
      argv: [],
      backend: () => ({
        attachedProfile: () => "work",
        rehome: async () => {
          throw new Error("boom");
        },
      }),
    });
    await expect(
      call("indus:profile:prepare-change", { action: "delete", profile: "default" }),
    ).rejects.toThrow(/default profile/);
    await expect(
      call("indus:profile:prepare-change", { action: "delete", profile: "work" }),
    ).rejects.toThrow(/could not leave work: boom/);
  });

  it("the schemas take the new shapes and refuse an unknown action", () => {
    const prepare = SCHEMAS["indus:profile:prepare-change"].request;
    expect(prepare.safeParse({ action: "rename", profile: "work" }).success).toBe(true);
    expect(prepare.safeParse({ action: "wipe", profile: "work" }).success).toBe(false);
    const commit = SCHEMAS["indus:profile:commit-change"].request;
    expect(commit.safeParse({ action: "rename", profile: "a", to: "b" }).success).toBe(true);
    expect(commit.safeParse({ action: "rename", profile: "a", extra: 1 }).success).toBe(false);
  });
});
