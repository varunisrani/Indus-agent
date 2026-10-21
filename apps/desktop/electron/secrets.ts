// Desktop-held secrets at rest (P20-07, D-16.13, R08.10).
//
// The desktop keeps only its own credentials: the saved remote connection's token and (N17) the
// access-proxy headers. Provider API keys never come here — they go to the profile's SecretStore
// through the daemon / REST API — and any other name is refused.
//
// Default store: the OS keychain through Electron `safeStorage`. Whether the keychain is usable is
// decided once per run by a probe that never asks the person anything and has a 2 s deadline:
//   macOS   `security show-keychain-info` (no UI; a locked, missing or broken keychain is an
//           error) must succeed before `safeStorage` is touched at all;
//   Linux   the selected backend must be a real secret service (`basic_text` is not);
//   Windows DPAPI never prompts.
// When it is not usable, entries are owner-only files (0600 in a 0700 directory) and a keychain
// entry already on disk reads as absent — `safeStorage` is never called, so nothing prompts.

import { execFile } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { ensurePrivateDir, readJsonFile, readRegularFile, writePrivateFile } from "./fsSecure.ts";
import { IpcError } from "./ipc/envelope.ts";
import type { IpcRegistry } from "./ipc/registry.ts";

export const KEYCHAIN_PROBE_TIMEOUT_MS = 2_000;
export const DESKTOP_SECRET_NAMES = ["remote-connection-token", "access-proxy-headers"] as const;
/** Per-connection secrets of the connections registry (P24-29): token, OAuth set, proxy headers. */
export const CONNECTION_SECRET_RE = /^connection-[a-z0-9][a-z0-9-]{0,63}-(token|oauth|headers)$/;
export type ConnectionSecretName = `connection-${string}`;
export type DesktopSecretName = (typeof DESKTOP_SECRET_NAMES)[number] | ConnectionSecretName;
export type SecretStoreKind = "keychain" | "file";

/** The parts of Electron's `safeStorage` we use. */
export interface SafeStorageLike {
  isEncryptionAvailable(): boolean;
  encryptString(plain: string): Buffer;
  decryptString(cipher: Buffer): string;
  getSelectedStorageBackend?(): string;
}

export interface ProbeResult {
  usable: boolean;
  reason: string;
}

export interface ProbeOptions {
  platform: string;
  safeStorage: SafeStorageLike;
  /** Runs the macOS `security` check; resolves true on success. Injected in tests. */
  macKeychainCheck?: (timeoutMs: number) => Promise<boolean>;
  timeoutMs?: number;
}

export class DesktopSecretError extends Error {
  readonly code: "not-a-desktop-secret" | "keychain-unusable" | "unsafe-entry";

  constructor(code: DesktopSecretError["code"], message: string) {
    super(message);
    this.name = "DesktopSecretError";
    this.code = code;
  }
}

/** `security show-keychain-info` on the default keychain; true when it exits 0 in time. */
export function macKeychainCheck(timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    try {
      execFile(
        "/usr/bin/security",
        ["show-keychain-info"],
        { timeout: timeoutMs, killSignal: "SIGKILL", windowsHide: true },
        (error) => resolve(error === null),
      );
    } catch {
      resolve(false);
    }
  });
}

const LINUX_SECURE_BACKENDS = new Set(["gnome_libsecret", "kwallet", "kwallet5", "kwallet6"]);

async function probeInner(options: ProbeOptions, timeoutMs: number): Promise<ProbeResult> {
  if (options.platform === "darwin") {
    const check = options.macKeychainCheck ?? macKeychainCheck;
    if (!(await check(timeoutMs)))
      return { usable: false, reason: "the macOS keychain is locked or missing" };
  }
  if (options.platform === "linux") {
    const backend = options.safeStorage.getSelectedStorageBackend?.() ?? "unknown";
    if (!LINUX_SECURE_BACKENDS.has(backend)) {
      return { usable: false, reason: `no secret service (backend ${backend})` };
    }
  }
  if (!options.safeStorage.isEncryptionAvailable())
    return { usable: false, reason: "encryption unavailable" };
  return { usable: true, reason: "keychain available" };
}

/** Decide whether the keychain is usable this run. Never throws, never waits past the deadline. */
export async function probeKeychain(options: ProbeOptions): Promise<ProbeResult> {
  const timeoutMs = options.timeoutMs ?? KEYCHAIN_PROBE_TIMEOUT_MS;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<ProbeResult>((resolve) => {
    timer = setTimeout(
      () => resolve({ usable: false, reason: "the keychain probe timed out" }),
      timeoutMs,
    );
  });
  try {
    return await Promise.race([
      probeInner(options, timeoutMs).catch(() => ({
        usable: false,
        reason: "the keychain probe failed",
      })),
      deadline,
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

interface StoredEntry {
  v: 1;
  store: SecretStoreKind;
  data: string;
}

interface PolicyFile {
  v: 1;
  store: SecretStoreKind;
  reason: "default" | "user";
}

export interface SecretsPolicyView {
  store: SecretStoreKind;
  effective: SecretStoreKind;
  keychainUsable: boolean;
  reason: string;
}

export function isDesktopSecretName(name: string): name is DesktopSecretName {
  return (
    (DESKTOP_SECRET_NAMES as readonly string[]).includes(name) || CONNECTION_SECRET_RE.test(name)
  );
}

export interface DesktopSecretsOptions {
  dir: string;
  policyPath: string;
  safeStorage: SafeStorageLike;
  probe: ProbeResult;
  platform?: string;
  log?: (line: string) => void;
}

export class DesktopSecrets {
  private readonly options: DesktopSecretsOptions;
  private readonly platform: string;
  private policy: PolicyFile;
  private readonly warned = new Set<string>();

  constructor(options: DesktopSecretsOptions) {
    this.options = options;
    this.platform = options.platform ?? process.platform;
    this.policy = DesktopSecrets.readPolicy(options.policyPath);
  }

  private static readPolicy(file: string): PolicyFile {
    const raw = readJsonFile(file) as Partial<PolicyFile> | null;
    if (raw !== null && (raw.store === "keychain" || raw.store === "file")) {
      return { v: 1, store: raw.store, reason: raw.reason === "user" ? "user" : "default" };
    }
    return { v: 1, store: "keychain", reason: "default" };
  }

  /** The store this run writes to. */
  effectiveStore(): SecretStoreKind {
    return this.policy.store === "keychain" && this.options.probe.usable ? "keychain" : "file";
  }

  view(): SecretsPolicyView {
    return {
      store: this.policy.store,
      effective: this.effectiveStore(),
      keychainUsable: this.options.probe.usable,
      reason: this.options.probe.reason,
    };
  }

  get(name: string): string | null {
    const entry = this.readEntry(this.checkName(name));
    if (entry === null) return null;
    if (entry.store === "file") return entry.data;
    if (!this.options.probe.usable) return null;
    try {
      return this.options.safeStorage.decryptString(Buffer.from(entry.data, "base64"));
    } catch {
      this.warnOnce(name, `[secrets] ${name}: the keychain could not decrypt the saved value`);
      return null;
    }
  }

  put(name: string, value: string): void {
    const checked = this.checkName(name);
    if (value === "") {
      this.delete(checked);
      return;
    }
    this.writeEntry(checked, value, this.effectiveStore());
  }

  delete(name: string): void {
    const file = this.entryPath(this.checkName(name));
    fs.rmSync(file, { force: true });
  }

  /** Switch the store (Settings toggle). Every readable entry is re-written in the new store. */
  setStore(store: SecretStoreKind): SecretsPolicyView {
    if (store === "keychain" && !this.options.probe.usable) {
      throw new DesktopSecretError(
        "keychain-unusable",
        `the keychain is not usable: ${this.options.probe.reason}`,
      );
    }
    const values = new Map<DesktopSecretName, string>();
    for (const name of this.knownNames()) {
      const value = this.get(name);
      if (value !== null) values.set(name, value);
    }
    this.policy = { v: 1, store, reason: "user" };
    writePrivateFile(this.options.policyPath, `${JSON.stringify(this.policy)}\n`, this.platform);
    for (const [name, value] of values) this.writeEntry(name, value, this.effectiveStore());
    return this.view();
  }

  /** The fixed names plus every per-connection entry on disk. */
  private knownNames(): DesktopSecretName[] {
    const names: DesktopSecretName[] = [...DESKTOP_SECRET_NAMES];
    let files: string[] = [];
    try {
      files = fs.readdirSync(this.options.dir);
    } catch {
      files = [];
    }
    for (const file of files) {
      const name = file.endsWith(".json") ? file.slice(0, -5) : "";
      if (CONNECTION_SECRET_RE.test(name)) names.push(name as ConnectionSecretName);
    }
    return names;
  }

  private checkName(name: string): DesktopSecretName {
    if (!isDesktopSecretName(name)) {
      throw new DesktopSecretError(
        "not-a-desktop-secret",
        `${JSON.stringify(name)} is not a desktop secret; provider keys belong in the profile's secret store`,
      );
    }
    return name;
  }

  private entryPath(name: DesktopSecretName): string {
    return path.join(this.options.dir, `${name}.json`);
  }

  private readEntry(name: DesktopSecretName): StoredEntry | null {
    let text: string | null;
    try {
      text = readRegularFile(this.entryPath(name));
    } catch {
      throw new DesktopSecretError(
        "unsafe-entry",
        `${name}: the saved entry is not a regular file`,
      );
    }
    if (text === null) return null;
    try {
      const raw = JSON.parse(text) as Partial<StoredEntry>;
      if ((raw.store === "keychain" || raw.store === "file") && typeof raw.data === "string") {
        return { v: 1, store: raw.store, data: raw.data };
      }
    } catch {
      // fall through
    }
    this.warnOnce(name, `[secrets] ${name}: unreadable entry ignored`);
    return null;
  }

  private writeEntry(name: DesktopSecretName, value: string, store: SecretStoreKind): void {
    ensurePrivateDir(this.options.dir, this.platform);
    const data =
      store === "keychain"
        ? this.options.safeStorage.encryptString(value).toString("base64")
        : value;
    const entry: StoredEntry = { v: 1, store, data };
    writePrivateFile(this.entryPath(name), `${JSON.stringify(entry)}\n`, this.platform);
  }

  private warnOnce(key: string, line: string): void {
    if (this.warned.has(key)) return;
    this.warned.add(key);
    this.options.log?.(line);
  }
}

/** Settings → Gateways "secure storage" toggle (P20-32 renders it). */
export function installSecretsIpc(registry: IpcRegistry, secrets: DesktopSecrets): void {
  registry.handle("indus:secrets:policy:get", () => secrets.view());
  registry.handle("indus:secrets:policy:set", ({ store }) => {
    try {
      return secrets.setStore(store);
    } catch (error) {
      if (error instanceof DesktopSecretError) throw new IpcError(error.code, error.message);
      throw error;
    }
  });
}
