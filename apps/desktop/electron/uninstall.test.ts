import { describe, expect, it, vi } from "vitest";
import {
  CONFIRM_PHRASE,
  decideOwnership,
  helperCommand,
  LEVEL_ARGS,
  type OwnershipInput,
  posixHelper,
  SUMMARY_ARGS,
  type UninstallDeps,
  UninstallService,
  windowsHelper,
} from "./uninstall.ts";

const HOME = "/home/someone/.indus";
const INDUS = {
  command: "/home/someone/.indus/indus-agent/venv/bin/python",
  args: ["-m", "indus"],
};

function summary(extra: Record<string, unknown> = {}): string {
  return JSON.stringify({
    gui_installed: true,
    agent_installed: true,
    install_kind: "git",
    home: HOME,
    code_dir: "/home/someone/.indus/indus-agent",
    ...extra,
  });
}

function input(extra: Partial<OwnershipInput> = {}): OwnershipInput {
  return {
    home: HOME,
    execPath: "/opt/Indus/indus",
    summaryText: summary(),
    receipts: [{ id: "r1", installer: "sh", outcome: "success" }],
    ...extra,
  };
}

function deps(extra: Partial<UninstallDeps> = {}) {
  const spawned: Array<{ command: string; args: readonly string[] }> = [];
  const scripts: string[] = [];
  const ran: Array<readonly string[]> = [];
  const quit = vi.fn();
  const base: UninstallDeps = {
    home: HOME,
    platform: "linux",
    pid: 4242,
    execPath: "/opt/Indus/indus",
    indus: async () => INDUS,
    run: async (_indus, args) => {
      ran.push(args);
      return summary();
    },
    readReceipts: async () => [{ outcome: "success" }],
    writeHelper: async (text, ext) => {
      scripts.push(text);
      return `/tmp/indus-uninstall${ext}`;
    },
    spawnDetached: (command, args) => spawned.push({ command, args }),
    quit,
    ...extra,
  };
  return { deps: base, spawned, scripts, ran, quit };
}

describe("levels and the CLI flags table", () => {
  it("the helper command line matches docs/internals/uninstall-levels.md", () => {
    expect(LEVEL_ARGS).toEqual({
      gui: ["uninstall", "--gui", "--yes"],
      "keep-data": ["uninstall", "--yes"],
      full: ["uninstall", "--full", "--yes"],
    });
    expect(SUMMARY_ARGS).toEqual(["uninstall", "--gui-summary"]);
    expect(helperCommand(INDUS, "full")).toEqual([
      INDUS.command,
      "-m",
      "indus",
      "uninstall",
      "--full",
      "--yes",
    ]);
    expect(() => helperCommand(INDUS, "data" as never)).toThrow();
  });
});

describe("ownership", () => {
  it("a confirmed git install offers all three levels", () => {
    expect(decideOwnership(input())).toEqual({
      allowed: true,
      levels: ["gui", "keep-data", "full"],
      installKind: "git",
      home: HOME,
      confirmPhrase: CONFIRM_PHRASE,
    });
  });

  it("a Nix-owned receipt hides all options", () => {
    const view = decideOwnership(input({ summaryText: summary({ install_kind: "nix" }) }));
    expect(view.allowed).toBe(false);
    expect(view.levels).toEqual([]);
    expect(view.reason).toContain("Nix");
    const store = decideOwnership(input({ execPath: "/nix/store/abc-indus/bin/indus" }));
    expect(store).toMatchObject({ allowed: false, levels: [], installKind: "nix" });
  });

  it.each([
    ["summary failed", { summaryText: null }, "gave no answer"],
    ["not JSON", { summaryText: "Traceback ..." }, "could not be read"],
    [
      "no git checkout",
      { summaryText: summary({ agent_installed: false }) },
      "development checkout",
    ],
    ["another home", { summaryText: summary({ home: "/elsewhere" }) }, "/elsewhere"],
    ["no receipt", { receipts: [] }, "install_receipts"],
  ])("%s: the reason names what could not be confirmed (W54-D2-4)", (_label, extra, words) => {
    const view = decideOwnership(input(extra as Partial<OwnershipInput>));
    expect(view.reason).toContain("could not confirm how it was installed");
    expect(view.reason).toContain(words);
  });

  it.each([
    ["docker kind", { summaryText: summary({ install_kind: "docker" }) }],
    ["bundled runtime", { summaryText: summary({ install_kind: "desktop" }) }],
    ["no git checkout", { summaryText: summary({ agent_installed: false }) }],
    ["another home", { summaryText: summary({ home: "/elsewhere" }) }],
    ["summary failed", { summaryText: null }],
    ["not JSON", { summaryText: "Traceback ..." }],
    ["a JSON list", { summaryText: "[1]" }],
    ["no receipt", { receipts: [] }],
    ["only a failed receipt", { receipts: [{ outcome: "failed" }, { outcome: "interrupted" }] }],
  ])("%s hides every level", (_label, extra) => {
    const view = decideOwnership(input(extra as Partial<OwnershipInput>));
    expect(view.allowed).toBe(false);
    expect(view.levels).toEqual([]);
    expect(view.reason).toBeTruthy();
  });
});

describe("run", () => {
  it('"everything" without the typed confirmation does nothing', async () => {
    for (const typed of [undefined, "", "yes", "uninstall", "Uninstall Everything!"]) {
      const rig = deps();
      const answer = await new UninstallService(rig.deps).run("full", typed);
      expect(answer).toMatchObject({ ok: false, code: "confirm" });
      expect(rig.scripts).toEqual([]);
      expect(rig.spawned).toEqual([]);
      expect(rig.quit).not.toHaveBeenCalled();
    }
  });

  it("with the phrase it writes the helper, starts it detached and quits", async () => {
    const rig = deps();
    const answer = await new UninstallService(rig.deps).run("full", CONFIRM_PHRASE);
    expect(answer).toEqual({ ok: true });
    expect(rig.spawned).toEqual([{ command: "/bin/sh", args: ["/tmp/indus-uninstall.sh"] }]);
    expect(rig.scripts[0]).toContain("'uninstall' '--full' '--yes'");
    expect(rig.quit).toHaveBeenCalledOnce();
  });

  it("ownership is checked again in main; a refused install spawns nothing", async () => {
    const rig = deps({ run: async () => summary({ install_kind: "nix" }) });
    const service = new UninstallService(rig.deps);
    expect(await service.run("gui", undefined)).toMatchObject({ ok: false, code: "not-owned" });
    expect(rig.spawned).toEqual([]);
    const none = deps({ indus: async () => null });
    expect(await new UninstallService(none.deps).run("keep-data", undefined)).toMatchObject({
      ok: false,
      code: "not-owned",
    });
  });

  it("a second run while one is starting is refused", async () => {
    const rig = deps();
    const service = new UninstallService(rig.deps);
    await service.run("gui", undefined);
    expect(await service.run("gui", undefined)).toMatchObject({ ok: false, code: "busy" });
    expect(rig.spawned).toHaveLength(1);
  });

  it("windows uses a batch helper through cmd", async () => {
    const rig = deps({ platform: "win32", home: "C:\\Users\\me\\.indus" });
    rig.deps.run = async () => summary({ home: "C:\\Users\\me\\.indus" });
    await new UninstallService(rig.deps).run("keep-data", undefined);
    expect(rig.spawned[0]?.command).toBe("cmd.exe");
    expect(rig.scripts[0]).toContain('"uninstall" "--yes"');
  });
});

describe("helper scripts", () => {
  it("POSIX: waits for the pid (bounded), sets INDUS_HOME, quotes every word, removes itself", () => {
    const text = posixHelper({
      pid: 99,
      argv: ["/path with space/py", "-m", "indus", "it's", "--yes"],
      home: "/home/o'neil/.indus",
    });
    expect(text).toContain("pid=99");
    expect(text).toContain('kill -0 "$pid"');
    expect(text).toContain("-lt 60");
    expect(text).toContain("INDUS_HOME='/home/o'\\''neil/.indus'");
    expect(text).toContain("'/path with space/py' '-m' 'indus' 'it'\\''s' '--yes'");
    expect(text.trimEnd().endsWith('rm -f -- "$0"')).toBe(true);
    expect(posixHelper({ pid: Number.NaN, argv: ["x"], home: "/h" })).toContain("pid=0");
  });

  it("Windows: exact PID match, bounded wait, refuses paths cmd cannot quote", () => {
    const text = windowsHelper({ pid: 7, argv: ["C:\\py.exe", "-m", "indus"], home: "C:\\h" });
    expect(text).toContain('set "PID=7"');
    expect(text).toContain('findstr /r /c:" %PID% "');
    expect(text).toContain("geq 30");
    expect(text).toContain('set "INDUS_HOME=C:\\h"');
    expect(text).toContain("\r\n");
    for (const bad of ['C:\\a"b', "C:\\100%", "C:\\a&b", "C:\\a^b"]) {
      expect(() => windowsHelper({ pid: 1, argv: [bad], home: "C:\\h" })).toThrow();
    }
  });
});
