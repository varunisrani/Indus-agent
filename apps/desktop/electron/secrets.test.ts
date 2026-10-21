import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DesktopSecretError,
  DesktopSecrets,
  probeKeychain,
  type SafeStorageLike,
} from "./secrets.ts";

const posix = process.platform !== "win32";
const dirs: string[] = [];
afterEach(() => {
  vi.useRealTimers();
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

function userData(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-secrets-"));
  dirs.push(dir);
  return dir;
}

/** A reversible stand-in for safeStorage (not real crypto; enough to prove no plaintext lands). */
function fakeSafeStorage(
  available = true,
  backend = "gnome_libsecret",
): SafeStorageLike & { calls: number } {
  const storage = {
    calls: 0,
    isEncryptionAvailable: () => {
      storage.calls += 1;
      return available;
    },
    encryptString: (plain: string) => {
      storage.calls += 1;
      return Buffer.from(`enc:${Buffer.from(plain).toString("hex").split("").reverse().join("")}`);
    },
    decryptString: (cipher: Buffer) => {
      storage.calls += 1;
      const text = cipher.toString();
      if (!text.startsWith("enc:")) throw new Error("bad cipher");
      return Buffer.from(text.slice(4).split("").reverse().join(""), "hex").toString();
    },
    getSelectedStorageBackend: () => backend,
  };
  return storage;
}

// Built at run time: never a credential-shaped literal in the source.
const TOKEN = ["remote", "token", "f00dfeed", "0123456789"].join("-");

function allFileText(dir: string): string {
  let text = "";
  for (const entry of fs.readdirSync(dir, { withFileTypes: true, recursive: true })) {
    const full = path.join(entry.parentPath, entry.name);
    if (entry.isFile()) text += fs.readFileSync(full, "utf8");
  }
  return text;
}

describe("keychain probe", () => {
  it("a probe that throws → unusable", async () => {
    const storage = fakeSafeStorage();
    storage.isEncryptionAvailable = () => {
      throw new Error("keychain exploded");
    };
    expect(await probeKeychain({ platform: "win32", safeStorage: storage })).toMatchObject({
      usable: false,
    });
  });

  it("a macOS check that hangs → unusable at the 2 s deadline, safeStorage never touched", async () => {
    vi.useFakeTimers();
    const storage = fakeSafeStorage();
    const result = probeKeychain({
      platform: "darwin",
      safeStorage: storage,
      macKeychainCheck: () => new Promise(() => {}),
    });
    await vi.advanceTimersByTimeAsync(1_999);
    let settled = false;
    void result.then(() => {
      settled = true;
    });
    await Promise.resolve();
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(await result).toEqual({ usable: false, reason: "the keychain probe timed out" });
    expect(storage.calls).toBe(0);
  });

  it("a failing macOS check → unusable without touching safeStorage", async () => {
    const storage = fakeSafeStorage();
    expect(
      await probeKeychain({
        platform: "darwin",
        safeStorage: storage,
        macKeychainCheck: async () => false,
      }),
    ).toMatchObject({
      usable: false,
    });
    expect(storage.calls).toBe(0);
  });

  it("Linux basic_text is not a keychain; libsecret is", async () => {
    expect(
      (await probeKeychain({ platform: "linux", safeStorage: fakeSafeStorage(true, "basic_text") }))
        .usable,
    ).toBe(false);
    expect(
      (
        await probeKeychain({
          platform: "linux",
          safeStorage: fakeSafeStorage(true, "gnome_libsecret"),
        })
      ).usable,
    ).toBe(true);
  });
});

describe("file fallback", () => {
  it.skipIf(!posix)("probe failed → entries are 0600 files in a 0700 directory", () => {
    const dir = userData();
    const storage = fakeSafeStorage();
    const secrets = new DesktopSecrets({
      dir: path.join(dir, "secrets"),
      policyPath: path.join(dir, "secrets-policy.json"),
      safeStorage: storage,
      probe: { usable: false, reason: "the keychain probe timed out" },
    });
    expect(secrets.effectiveStore()).toBe("file");
    secrets.put("remote-connection-token", TOKEN);
    expect(secrets.get("remote-connection-token")).toBe(TOKEN);
    expect(fs.statSync(path.join(dir, "secrets")).mode & 0o777).toBe(0o700);
    expect(
      fs.statSync(path.join(dir, "secrets", "remote-connection-token.json")).mode & 0o777,
    ).toBe(0o600);
    expect(storage.calls).toBe(0);
    secrets.delete("remote-connection-token");
    expect(secrets.get("remote-connection-token")).toBeNull();
  });

  it.skipIf(!posix)("tightens a loose existing directory and refuses a symlinked entry", () => {
    const dir = userData();
    fs.mkdirSync(path.join(dir, "secrets"), { mode: 0o755 });
    fs.chmodSync(path.join(dir, "secrets"), 0o755);
    const secrets = new DesktopSecrets({
      dir: path.join(dir, "secrets"),
      policyPath: path.join(dir, "secrets-policy.json"),
      safeStorage: fakeSafeStorage(),
      probe: { usable: false, reason: "x" },
    });
    secrets.put("access-proxy-headers", "h");
    expect(fs.statSync(path.join(dir, "secrets")).mode & 0o777).toBe(0o700);
    fs.rmSync(path.join(dir, "secrets", "remote-connection-token.json"), { force: true });
    fs.writeFileSync(path.join(dir, "elsewhere.json"), '{"v":1,"store":"file","data":"planted"}');
    fs.symlinkSync(
      path.join(dir, "elsewhere.json"),
      path.join(dir, "secrets", "remote-connection-token.json"),
    );
    expect(() => secrets.get("remote-connection-token")).toThrow(DesktopSecretError);
  });

  it("a keychain entry with the keychain unusable reads as absent, no safeStorage call", () => {
    const dir = userData();
    fs.mkdirSync(path.join(dir, "secrets"), { recursive: true });
    fs.writeFileSync(
      path.join(dir, "secrets", "remote-connection-token.json"),
      '{"v":1,"store":"keychain","data":"Zm9v"}',
    );
    const storage = fakeSafeStorage();
    const secrets = new DesktopSecrets({
      dir: path.join(dir, "secrets"),
      policyPath: path.join(dir, "secrets-policy.json"),
      safeStorage: storage,
      probe: { usable: false, reason: "locked" },
    });
    expect(secrets.get("remote-connection-token")).toBeNull();
    expect(storage.calls).toBe(0);
  });
});

describe("keychain store (mocked safeStorage)", () => {
  it("no secret in connection.json or any other file in plaintext when the keychain works", () => {
    const dir = userData();
    const secrets = new DesktopSecrets({
      dir: path.join(dir, "secrets"),
      policyPath: path.join(dir, "secrets-policy.json"),
      safeStorage: fakeSafeStorage(),
      probe: { usable: true, reason: "keychain available" },
    });
    secrets.put("remote-connection-token", TOKEN);
    // P20-11 writes connection.json with a reference to the secret, never the value.
    fs.writeFileSync(
      path.join(dir, "connection.json"),
      JSON.stringify({ url: "https://agent.example", tokenRef: "remote-connection-token" }),
    );
    expect(allFileText(dir)).not.toContain(TOKEN);
    expect(secrets.get("remote-connection-token")).toBe(TOKEN);
    expect(secrets.view()).toEqual({
      store: "keychain",
      effective: "keychain",
      keychainUsable: true,
      reason: "keychain available",
    });
  });

  it("an undecryptable entry reads as absent", () => {
    const dir = userData();
    fs.mkdirSync(path.join(dir, "secrets"), { recursive: true });
    fs.writeFileSync(
      path.join(dir, "secrets", "remote-connection-token.json"),
      '{"v":1,"store":"keychain","data":"Zm9v"}',
    );
    const secrets = new DesktopSecrets({
      dir: path.join(dir, "secrets"),
      policyPath: path.join(dir, "secrets-policy.json"),
      safeStorage: fakeSafeStorage(),
      probe: { usable: true, reason: "ok" },
    });
    expect(secrets.get("remote-connection-token")).toBeNull();
  });
});

describe("policy", () => {
  it("defaults to keychain; switching to file re-writes entries in plaintext files (owner-only)", () => {
    const dir = userData();
    const options = {
      dir: path.join(dir, "secrets"),
      policyPath: path.join(dir, "secrets-policy.json"),
      safeStorage: fakeSafeStorage(),
      probe: { usable: true, reason: "ok" },
    };
    const secrets = new DesktopSecrets(options);
    secrets.put("remote-connection-token", TOKEN);
    expect(secrets.setStore("file")).toMatchObject({ store: "file", effective: "file" });
    expect(JSON.parse(fs.readFileSync(path.join(dir, "secrets-policy.json"), "utf8"))).toEqual({
      v: 1,
      store: "file",
      reason: "user",
    });
    expect(
      fs.readFileSync(path.join(dir, "secrets", "remote-connection-token.json"), "utf8"),
    ).toContain(TOKEN);
    // A new process reads the recorded choice.
    expect(new DesktopSecrets(options).view().store).toBe("file");
    expect(new DesktopSecrets(options).get("remote-connection-token")).toBe(TOKEN);
  });

  it("switching to keychain while it is unusable is refused", () => {
    const dir = userData();
    const secrets = new DesktopSecrets({
      dir: path.join(dir, "secrets"),
      policyPath: path.join(dir, "secrets-policy.json"),
      safeStorage: fakeSafeStorage(),
      probe: { usable: false, reason: "locked" },
    });
    expect(() => secrets.setStore("keychain")).toThrow(/not usable/);
  });

  it("provider keys and other names are refused", () => {
    const dir = userData();
    const secrets = new DesktopSecrets({
      dir: path.join(dir, "secrets"),
      policyPath: path.join(dir, "secrets-policy.json"),
      safeStorage: fakeSafeStorage(),
      probe: { usable: true, reason: "ok" },
    });
    for (const name of ["OPENAI_API_KEY", "openai_api_key", "../escape", ""]) {
      expect(() => secrets.put(name, "x")).toThrow(DesktopSecretError);
    }
  });
});
