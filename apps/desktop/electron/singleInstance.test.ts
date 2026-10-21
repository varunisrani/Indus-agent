import { describe, expect, it, vi } from "vitest";
import {
  acquireSingleInstance,
  clearStaleLock,
  extractDeepLink,
  type LockApp,
  lockTempDir,
  MachineryGate,
  NotLockHolderError,
  type SecondLaunch,
} from "./singleInstance.ts";

function fakeApp(holder: boolean) {
  const order: string[] = [];
  const listeners: Array<(event: unknown, argv: string[], cwd: string, data: unknown) => void> = [];
  const app: LockApp = {
    requestSingleInstanceLock: vi.fn(() => {
      order.push("lock");
      return holder;
    }),
    on: vi.fn((_event, listener) => {
      listeners.push(listener);
      return app;
    }),
    setAsDefaultProtocolClient: vi.fn((scheme: string) => {
      order.push(`register:${scheme}`);
      return true;
    }),
  };
  return { app, order, listeners };
}

const base = { execPath: "/opt/Indus/indus", registerProtocol: true };

describe("single-instance lock", () => {
  it("a loser stays inert: every guarded machinery call throws and nothing is registered", () => {
    const { app, listeners } = fakeApp(false);
    const launches: SecondLaunch[] = [];
    const instance = acquireSingleInstance({
      ...base,
      app,
      platform: "linux",
      dev: false,
      onSecondLaunch: (l) => launches.push(l),
    });
    expect(instance.holder).toBe(false);
    const spawn = vi.fn();
    const reap = vi.fn();
    const claim = vi.fn();
    expect(() => instance.machinery.guard("spawn a backend", spawn)()).toThrow(NotLockHolderError);
    expect(() => instance.machinery.guard("reap orphans", reap)()).toThrow(NotLockHolderError);
    expect(() => instance.machinery.guard("claim a backend", claim)()).toThrow(NotLockHolderError);
    expect(spawn).not.toHaveBeenCalled();
    expect(reap).not.toHaveBeenCalled();
    expect(claim).not.toHaveBeenCalled();
    expect(listeners).toHaveLength(0);
    expect(app.setAsDefaultProtocolClient).not.toHaveBeenCalled();
  });

  it("the holder runs guarded machinery and receives a second launch's argv and deep link", () => {
    const { app, listeners } = fakeApp(true);
    const launches: SecondLaunch[] = [];
    const instance = acquireSingleInstance({
      ...base,
      app,
      platform: "linux",
      dev: false,
      onSecondLaunch: (l) => launches.push(l),
    });
    expect(instance.holder).toBe(true);
    expect(instance.machinery.guard("spawn", (x: number) => x * 2)(21)).toBe(42);
    listeners[0]?.({}, ["/opt/Indus/indus", "--flag", "indus://open/chat?x=1"], "/home/me", {});
    expect(launches).toEqual([
      {
        argv: ["/opt/Indus/indus", "--flag", "indus://open/chat?x=1"],
        deepLink: "indus://open/chat?x=1",
        workingDirectory: "/home/me",
      },
    ]);
  });

  it("macOS registers the protocol client before taking the lock; others after, holder only", () => {
    const mac = fakeApp(true);
    acquireSingleInstance({
      ...base,
      app: mac.app,
      platform: "darwin",
      dev: false,
      onSecondLaunch: () => {},
    });
    expect(mac.order).toEqual(["register:indus", "lock"]);
    const linux = fakeApp(true);
    acquireSingleInstance({
      ...base,
      app: linux.app,
      platform: "linux",
      dev: false,
      onSecondLaunch: () => {},
    });
    expect(linux.order).toEqual(["lock", "register:indus"]);
  });

  it("dev builds use the dev scheme and register the app path", () => {
    const { app } = fakeApp(true);
    const instance = acquireSingleInstance({
      ...base,
      app,
      platform: "win32",
      dev: true,
      appPath: "/src/app",
      onSecondLaunch: () => {},
    });
    expect(instance.scheme).toBe("indus-dev");
    expect(app.setAsDefaultProtocolClient).toHaveBeenCalledWith("indus-dev", "/opt/Indus/indus", [
      "/src/app",
    ]);
  });

  it("skips OS registration when asked (tests, e2e)", () => {
    const { app } = fakeApp(true);
    acquireSingleInstance({
      ...base,
      registerProtocol: false,
      app,
      platform: "linux",
      dev: false,
      onSecondLaunch: () => {},
    });
    expect(app.setAsDefaultProtocolClient).not.toHaveBeenCalled();
  });

  it("a lock call that throws counts as losing", () => {
    const { app } = fakeApp(true);
    app.requestSingleInstanceLock = () => {
      throw new Error("boom");
    };
    expect(
      acquireSingleInstance({
        ...base,
        app,
        platform: "linux",
        dev: false,
        onSecondLaunch: () => {},
      }).holder,
    ).toBe(false);
  });
});

describe("deep links in argv", () => {
  it("takes the last link on our scheme only", () => {
    expect(extractDeepLink(["a", "indus://one", "indus://two"], "indus")).toBe("indus://two");
    expect(extractDeepLink(["indus-dev://x"], "indus")).toBeNull();
    expect(extractDeepLink(["INDUS://Open/x"], "indus")).toBe("INDUS://Open/x");
    expect(extractDeepLink(["https://example.com"], "indus")).toBeNull();
    expect(extractDeepLink([`indus://${"a".repeat(5000)}`], "indus")).toBeNull();
    expect(extractDeepLink([42, null], "indus")).toBeNull();
  });
});

describe("MachineryGate", () => {
  it("assertHolder names what was refused", () => {
    expect(() => new MachineryGate(false).assertHolder("reap orphans")).toThrow(
      /reap orphans refused/,
    );
    expect(() => new MachineryGate(true).assertHolder("reap orphans")).not.toThrow();
  });
});

describe("lock robustness", () => {
  it("a TMPDIR too long for the lock socket is swapped for /tmp on Linux only", () => {
    expect(lockTempDir({ TMPDIR: "/tmp" }, "linux")).toBeNull();
    expect(lockTempDir({}, "linux")).toBeNull();
    expect(lockTempDir({ TMPDIR: `/${"x".repeat(100)}` }, "linux")).toBe("/tmp");
    expect(lockTempDir({ TMPDIR: `/${"x".repeat(100)}` }, "darwin")).toBeNull();
  });

  const probe = (target: string | Error, alive: boolean) => {
    const unlinked: string[] = [];
    return {
      unlinked,
      probe: {
        hostname: "box",
        isAlive: () => alive,
        fs: {
          readlink: () => {
            if (target instanceof Error) throw target;
            return target;
          },
          unlink: (file: string) => {
            unlinked.push(file);
          },
        },
      },
    };
  };

  it("a stale lock (this host, dead pid) is removed once and the lock retried", () => {
    const { app } = fakeApp(false);
    let calls = 0;
    app.requestSingleInstanceLock = () => {
      calls += 1;
      return calls > 1;
    };
    const stale = probe("box-4242", false);
    const instance = acquireSingleInstance({
      ...base,
      app,
      platform: "linux",
      dev: false,
      onSecondLaunch: () => {},
      staleLock: { file: "/ud/SingletonLock", probe: stale.probe },
    });
    expect(instance.holder).toBe(true);
    expect(stale.unlinked).toEqual(["/ud/SingletonLock"]);
    expect(calls).toBe(2);
  });

  it("a live owner, another host or no link leaves the lock alone", () => {
    for (const [target, alive] of [
      ["box-4242", true],
      ["other-4242", false],
      [new Error("ENOENT"), false],
      ["garbage", false],
    ] as const) {
      const p = probe(target as string | Error, alive);
      expect(clearStaleLock("/ud/SingletonLock", p.probe)).toBeNull();
      expect(p.unlinked).toEqual([]);
    }
  });
});
