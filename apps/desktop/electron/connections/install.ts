// The connections registry in main (P24-29): the service the boot orchestrator and the
// `connections.*` channels use. Secrets (tokens, a basic user name and password, OAuth sets,
// access-proxy headers) stay here and in the secrets store; the windows see names and flags only.
//
// Saving a token or basic entry tests both legs first (R08.5) and saves only when both pass; an
// OAuth entry is saved first and becomes usable after `signIn`. The launch entry decides what
// `RemoteStore.saved()` hands the orchestrator: null for "This computer", a remote dial for a
// remote entry, and for an SSH entry a dial whose base URL is prepared over SSH (P24-30).

import { randomBytes } from "node:crypto";
import type { SavedRemote } from "../backend/remote.ts";
import { IpcError } from "../ipc/envelope.ts";
import type { IpcRegistry } from "../ipc/registry.ts";
import { OAuthError, OAuthSession, signIn } from "./nativeOauth.ts";
import { decodeHeaders, encodeHeaders } from "./proxyHeaders.ts";
import {
  type AuthMode,
  type ConnectionEntry,
  type ConnectionRegistry,
  type EntryInput,
  type LaunchMode,
  LOCAL_ID,
  launchEntry,
  RegistryError,
  RegistryStore,
  type SecretsLike,
  type SshTarget,
  secretName,
  setLastUsed,
  setLaunchMode,
  setPrimary,
  upsertEntry,
} from "./registry.ts";
import {
  type ConnectionTestDeps,
  type ConnectionTestResult,
  RemoteAuth,
  type RemoteCredentials,
  testRemoteConnection,
} from "./remoteDial.ts";

export interface ConnectionView {
  id: string;
  kind: "local" | "remote" | "ssh";
  label: string;
  url?: string;
  authMode?: AuthMode;
  ssh?: SshTarget;
  primary: boolean;
  lastUsed: boolean;
  hasCredential: boolean;
  signedIn: boolean;
  headerNames: string[];
}

export interface ConnectionsView {
  connections: ConnectionView[];
  primaryId: string;
  launchMode: LaunchMode;
  quarantined: number;
}

export interface ConnectionInput {
  id?: string;
  kind: "remote" | "ssh";
  label?: string;
  url?: string;
  authMode?: AuthMode;
  ssh?: SshTarget;
  /** Token mode: a new token (blank keeps the stored one). */
  token?: string;
  /** Basic mode: user name and password (a blank password keeps the stored one). */
  username?: string;
  password?: string;
  /** Access-proxy headers; `{}` clears them, absent keeps them. */
  headers?: Record<string, string>;
}

export interface SaveResult {
  saved: boolean;
  id?: string;
  test: ConnectionTestResult | null;
  droppedHeaders: string[];
  message?: string;
}

/** Prepares an SSH entry's base URL and serve token (P24-30's `SshBackends`). */
export interface SshDialer {
  prepare(entry: ConnectionEntry, token: string): Promise<{ url: string }>;
}

export interface ConnectionsDeps {
  userData: string;
  secrets: SecretsLike;
  log: (line: string) => void;
  fetch?: typeof fetch;
  test?: ConnectionTestDeps;
  ssh?: SshDialer;
  /** A fresh session token for an SSH entry's remote serve. */
  newToken?: () => string;
}

interface Stored {
  token: string | null;
  basic: { username: string; password: string } | null;
  headers: Record<string, string>;
}

export class ConnectionsService {
  readonly store: RegistryStore;
  private readonly deps: ConnectionsDeps;

  constructor(deps: ConnectionsDeps) {
    this.deps = deps;
    this.store = new RegistryStore({
      userData: deps.userData,
      secrets: deps.secrets,
      log: deps.log,
    });
  }

  private secret(id: string, kind: "token" | "oauth" | "headers"): string | null {
    return this.deps.secrets.get(secretName(id, kind));
  }

  private stored(entry: ConnectionEntry): Stored {
    const raw = this.secret(entry.id, "token");
    let basic: Stored["basic"] = null;
    let token: string | null = raw;
    if (entry.authMode === "basic" && raw !== null) {
      try {
        const parsed = JSON.parse(raw) as { username?: unknown; password?: unknown };
        basic = {
          username: typeof parsed.username === "string" ? parsed.username : "",
          password: typeof parsed.password === "string" ? parsed.password : "",
        };
      } catch {
        basic = null;
      }
      token = null;
    }
    return { token, basic, headers: decodeHeaders(this.secret(entry.id, "headers")) };
  }

  private oauthFor(entry: ConnectionEntry): OAuthSession {
    const name = secretName(entry.id, "oauth");
    return new OAuthSession(
      entry.url ?? "",
      {
        get: () => this.deps.secrets.get(name),
        put: (value) => this.deps.secrets.put(name, value),
        delete: () => this.deps.secrets.delete(name),
      },
      { ...(this.deps.fetch === undefined ? {} : { fetch: this.deps.fetch }) },
    );
  }

  /** The credential view main uses for `entry` at `base` (a URL, or the SSH tunnel's address). */
  authFor(entry: ConnectionEntry, base = entry.url ?? ""): RemoteAuth {
    const stored = this.stored(entry);
    const credentials: RemoteCredentials = {
      mode: entry.kind === "ssh" ? "token" : (entry.authMode ?? "token"),
      token: stored.token,
      basic: stored.basic,
      oauth: entry.authMode === "oauth" ? this.oauthFor(entry) : null,
      headers: stored.headers,
    };
    return new RemoteAuth(
      base,
      credentials,
      this.deps.fetch === undefined ? {} : { fetch: this.deps.fetch },
    );
  }

  view(): ConnectionsView {
    const registry = this.store.load();
    return {
      connections: registry.connections.map((entry) => this.describe(entry, registry)),
      primaryId: registry.primaryId,
      launchMode: registry.launchMode,
      quarantined: registry.quarantined.length,
    };
  }

  private describe(entry: ConnectionEntry, registry: ConnectionRegistry): ConnectionView {
    const view: ConnectionView = {
      id: entry.id,
      kind: entry.kind,
      label: entry.label,
      primary: registry.primaryId === entry.id,
      lastUsed: registry.lastUsedId === entry.id,
      hasCredential: entry.kind === "local" || this.secret(entry.id, "token") !== null,
      signedIn: entry.authMode === "oauth" && this.oauthFor(entry).signedIn(),
      headerNames: Object.keys(decodeHeaders(this.secret(entry.id, "headers"))),
    };
    if (entry.url !== undefined) view.url = entry.url;
    if (entry.authMode !== undefined) view.authMode = entry.authMode;
    if (entry.ssh !== undefined) view.ssh = entry.ssh;
    return view;
  }

  private draft(input: ConnectionInput): { registry: ConnectionRegistry; entry: ConnectionEntry } {
    const entryInput: EntryInput = { kind: input.kind };
    if (input.id !== undefined) entryInput.id = input.id;
    if (input.label !== undefined) entryInput.label = input.label;
    if (input.url !== undefined) entryInput.url = input.url;
    if (input.authMode !== undefined) entryInput.authMode = input.authMode;
    if (input.ssh !== undefined) entryInput.ssh = input.ssh;
    try {
      const { registry, entry } = upsertEntry(this.store.load(), entryInput);
      return { registry, entry };
    } catch (error) {
      if (error instanceof RegistryError) throw new IpcError("bad-request", error.message);
      throw error;
    }
  }

  /** The secrets `input` would leave in place for `entry` (stored values kept where blank). */
  private secretsFor(
    entry: ConnectionEntry,
    input: ConnectionInput,
  ): Stored & { dropped: string[] } {
    const now = this.stored(entry);
    const token = input.token !== undefined && input.token !== "" ? input.token : now.token;
    let basic = now.basic;
    if (input.username !== undefined) {
      basic = {
        username: input.username,
        password:
          input.password !== undefined && input.password !== ""
            ? input.password
            : (basic?.password ?? ""),
      };
    }
    let headers = now.headers;
    let dropped: string[] = [];
    if (input.headers !== undefined) {
      const encoded = encodeHeaders(input.headers);
      headers = decodeHeaders(encoded.stored);
      dropped = encoded.dropped;
    }
    return { token, basic, headers, dropped };
  }

  async test(input: ConnectionInput): Promise<ConnectionTestResult> {
    const { entry } = this.draft(input);
    return this.testDraft(entry, this.secretsFor(entry, input));
  }

  private async testDraft(entry: ConnectionEntry, secrets: Stored): Promise<ConnectionTestResult> {
    let base = entry.url ?? "";
    if (entry.kind === "ssh") {
      if (this.deps.ssh === undefined || secrets.token === null) {
        return {
          ok: false,
          http: "error",
          ws: "error",
          nativeSignIn: false,
          message: "SSH is not available",
        };
      }
      try {
        base = (await this.deps.ssh.prepare(entry, secrets.token)).url;
      } catch (error) {
        return {
          ok: false,
          http: "unreachable",
          ws: "error",
          nativeSignIn: false,
          message: error instanceof Error ? error.message : "the SSH connection failed",
        };
      }
    }
    const auth = new RemoteAuth(
      base,
      {
        mode: entry.kind === "ssh" ? "token" : (entry.authMode ?? "token"),
        token: secrets.token,
        basic: secrets.basic,
        oauth: entry.authMode === "oauth" ? this.oauthFor(entry) : null,
        headers: secrets.headers,
      },
      this.deps.fetch === undefined ? {} : { fetch: this.deps.fetch },
    );
    return testRemoteConnection(auth, this.deps.test ?? {});
  }

  async save(input: ConnectionInput): Promise<SaveResult> {
    const { registry, entry } = this.draft(input);
    const secrets = this.secretsFor(entry, input);
    if (entry.kind === "ssh" && secrets.token === null) {
      secrets.token = (this.deps.newToken ?? defaultToken)();
    }
    const needsTest = !(
      entry.kind === "remote" &&
      entry.authMode === "oauth" &&
      !this.oauthFor(entry).signedIn()
    );
    const test = needsTest ? await this.testDraft(entry, secrets) : null;
    if (test !== null && !test.ok) {
      return {
        saved: false,
        test,
        droppedHeaders: secrets.dropped,
        message: test.message ?? "the test failed",
      };
    }
    this.store.save(registry);
    if (entry.authMode === "basic" && secrets.basic !== null) {
      this.deps.secrets.put(secretName(entry.id, "token"), JSON.stringify(secrets.basic));
    } else if (secrets.token !== null) {
      this.deps.secrets.put(secretName(entry.id, "token"), secrets.token);
    }
    if (input.headers !== undefined) {
      const stored =
        Object.keys(secrets.headers).length === 0 ? "" : JSON.stringify(secrets.headers);
      this.deps.secrets.put(secretName(entry.id, "headers"), stored);
    }
    this.deps.log(`[connections] saved "${entry.label}" (${entry.kind})`);
    return { saved: true, id: entry.id, test, droppedHeaders: secrets.dropped };
  }

  remove(id: string): ConnectionsView {
    try {
      this.store.remove(id);
    } catch (error) {
      if (error instanceof RegistryError) throw new IpcError("bad-request", error.message);
      throw error;
    }
    return this.view();
  }

  setPrimary(id: string): ConnectionsView {
    this.change((registry) => setPrimary(registry, id));
    return this.view();
  }

  setLaunchMode(mode: LaunchMode): ConnectionsView {
    this.change((registry) => setLaunchMode(registry, mode));
    return this.view();
  }

  /** The entry that opened (last-used launch mode reads it). */
  markUsed(id: string): void {
    try {
      this.change((registry) => setLastUsed(registry, id));
    } catch {
      // a removed entry: nothing to remember
    }
  }

  private change(fn: (registry: ConnectionRegistry) => ConnectionRegistry): void {
    try {
      this.store.update(fn);
    } catch (error) {
      if (error instanceof RegistryError) throw new IpcError("bad-request", error.message);
      throw error;
    }
  }

  private entry(id: string): ConnectionEntry {
    const found = this.store.load().connections.find((c) => c.id === id);
    if (found === undefined) throw new IpcError("bad-request", "no connection has that id");
    return found;
  }

  async signIn(
    id: string,
    openBrowser: (url: string) => Promise<void> | void,
  ): Promise<{ ok: boolean; message?: string }> {
    const entry = this.entry(id);
    if (entry.kind !== "remote" || entry.authMode !== "oauth" || entry.url === undefined) {
      throw new IpcError("bad-request", "this connection does not sign in");
    }
    try {
      const tokens = await signIn(entry.url, {
        openBrowser,
        ...(this.deps.fetch === undefined ? {} : { fetch: this.deps.fetch }),
      });
      this.oauthFor(entry).store(tokens);
      return { ok: true };
    } catch (error) {
      const message = error instanceof OAuthError ? error.message : "the sign-in did not finish";
      return { ok: false, message };
    }
  }

  signOut(id: string): { ok: true } {
    this.oauthFor(this.entry(id)).signOut();
    return { ok: true };
  }

  /** The first-run chooser's view (P20-11): the primary remote's URL, if the primary is one. */
  primaryRemote(): { url: string | null; hasToken: boolean } {
    const registry = this.store.load();
    const primary = registry.connections.find((c) => c.id === registry.primaryId);
    if (primary?.kind !== "remote") return { url: null, hasToken: false };
    return { url: primary.url ?? null, hasToken: this.secret(primary.id, "token") !== null };
  }

  /** The chooser saved a tested remote: it becomes (or updates) a token entry and the primary. */
  adoptRemote(url: string, token: string): string {
    const { registry, entry } = upsertEntry(this.store.load(), {
      kind: "remote",
      url,
      authMode: "token",
    });
    this.store.save(setPrimary(registry, entry.id));
    this.deps.secrets.put(secretName(entry.id, "token"), token);
    return entry.id;
  }

  /** The chooser forgot its remote: the primary remote entry goes, "This computer" is primary. */
  forgetPrimaryRemote(): void {
    const registry = this.store.load();
    const primary = registry.connections.find((c) => c.id === registry.primaryId);
    if (primary?.kind === "remote") this.store.remove(primary.id);
  }

  /** What the boot orchestrator dials at launch: null for "This computer". */
  launchRemote(): SavedRemote | null {
    const registry = this.store.load();
    const entry = launchEntry(registry);
    if (entry.id === LOCAL_ID || entry.kind === "local") return null;
    const stored = this.stored(entry);
    if (entry.kind === "remote") {
      if (entry.authMode !== "oauth" && stored.token === null && stored.basic === null) return null;
      return {
        url: entry.url as string,
        token: stored.token ?? "",
        connectionId: entry.id,
        auth: this.authFor(entry),
      };
    }
    const token = stored.token;
    const ssh = this.deps.ssh;
    if (token === null || ssh === undefined) return null;
    return {
      url: `ssh://${entry.ssh?.host ?? ""}`,
      token,
      connectionId: entry.id,
      prepare: async () => {
        const { url } = await ssh.prepare(entry, token);
        return { url, auth: this.authFor(entry, url) };
      },
    };
  }
}

function defaultToken(): string {
  return randomBytes(32).toString("base64url");
}

/** The `connections.*` channels; `onChange` reconnects windows after the primary changed. */
export function installConnectionsIpc(
  registry: IpcRegistry,
  service: ConnectionsService,
  options: {
    /** Reconnect the windows after the primary changed. */
    onChange: () => void;
    /** The audited system-browser opener (needs the person's click in that window). */
    openExternal: (url: string, contentsId: number) => Promise<{ ok: boolean }>;
  },
): void {
  const { onChange } = options;
  registry.handle("indus:connections:list", () => service.view());
  registry.handle("indus:connections:test", (input) => service.test(input as ConnectionInput));
  registry.handle("indus:connections:save", async (input) => {
    const result = await service.save(input as ConnectionInput);
    if (result.saved && result.id !== undefined && service.view().primaryId === result.id)
      onChange();
    return result;
  });
  registry.handle("indus:connections:remove", ({ id }) => {
    const before = service.view().primaryId;
    const view = service.remove(id);
    if (before !== view.primaryId) onChange();
    return view;
  });
  registry.handle("indus:connections:set-primary", ({ id }) => {
    const view = service.setPrimary(id);
    onChange();
    return view;
  });
  registry.handle("indus:connections:set-launch-mode", ({ mode }) => service.setLaunchMode(mode));
  registry.handle("indus:connections:sign-in", ({ id }, { senderId }) =>
    service.signIn(id, async (url) => {
      const opened = await options.openExternal(url, senderId);
      if (!opened.ok) throw new OAuthError("denied", "the system browser could not be opened");
    }),
  );
  registry.handle("indus:connections:sign-out", ({ id }) => service.signOut(id));
}
