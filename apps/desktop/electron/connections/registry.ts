// The connections registry (P24-29): every Indus server this desktop knows, in one file.
//
// `<userData>/connections.json` holds `{v, connections[], primaryId, lastUsedId?, launchMode,
// quarantined[]}`. Rules:
//   - exactly one `local` entry ("This computer") always exists; a second one in the file is
//     quarantined, a missing one is added;
//   - labels are unique ignoring case (a clash gets " 2", " 3", …);
//   - a remote is identified by its normalised URL, an SSH entry by `user@host:port` plus its
//     remote profile: adding one that exists updates that entry (id and label kept);
//   - an entry this app cannot read is never dropped: it goes to `quarantined[]` verbatim (at most
//     20) and is written back on every save;
//   - removing the primary makes `local` the primary; `local` cannot be removed;
//   - launch mode `primary` opens the primary, `last-used` the last entry that worked.
// Credentials never live in this file: a token, an OAuth set and access-proxy headers are secrets
// named `connection-<id>-token|oauth|headers` in the desktop secrets store (keychain by default).
// Cloud connections are not built (D-16.12).

import fs from "node:fs";
import path from "node:path";
import { normalizeRemoteUrl, REMOTE_FILE, REMOTE_TOKEN_SECRET } from "../backend/remote.ts";
import { readJsonFile, writeFileAtomic } from "../fsSecure.ts";

export const REGISTRY_FILE = "connections.json";
export const REGISTRY_VERSION = 1;
export const LOCAL_ID = "local";
export const LOCAL_LABEL = "This computer";
export const QUARANTINE_CAP = 20;
export const LABEL_MAX = 64;
const ID_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
const PROFILE_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

export type ConnectionKind = "local" | "remote" | "ssh";
export type AuthMode = "token" | "oauth" | "basic";
export type LaunchMode = "primary" | "last-used";
export type SecretKind = "token" | "oauth" | "headers";

export interface SshTarget {
  host: string;
  user?: string;
  port?: number;
  /** An identity file, used as `-i` (the agent and ~/.ssh/config work without one). */
  keyPath?: string;
  /** The profile the remote Indus serves (default: its default profile). */
  remoteProfile?: string;
}

export interface ConnectionEntry {
  id: string;
  kind: ConnectionKind;
  label: string;
  url?: string;
  authMode?: AuthMode;
  ssh?: SshTarget;
  /** Profiles last seen on that server (for the roster before it answers). */
  profiles?: string[];
}

export interface QuarantinedEntry {
  reason: string;
  entry: unknown;
}

export interface ConnectionRegistry {
  v: typeof REGISTRY_VERSION;
  connections: ConnectionEntry[];
  primaryId: string;
  lastUsedId?: string;
  launchMode: LaunchMode;
  quarantined: QuarantinedEntry[];
}

export class RegistryError extends Error {
  readonly code: "local-not-removable" | "unknown-connection" | "bad-entry";

  constructor(code: RegistryError["code"], message: string) {
    super(message);
    this.code = code;
  }
}

export function secretName(id: string, kind: SecretKind): `connection-${string}` {
  return `connection-${id}-${kind}`;
}

// -- labels, ids, handles -------------------------------------------------------------------------

export function labelKey(label: string): string {
  return label.trim().toLowerCase();
}

/** `candidate` (cut to fit) or the first free `candidate N`, ignoring case. */
export function uniqueLabel(candidate: string, taken: Iterable<string>): string {
  const used = new Set([...taken].map(labelKey));
  const base = candidate.trim().slice(0, LABEL_MAX - 4) || "Connection";
  if (!used.has(labelKey(base))) return base;
  for (let n = 2; ; n += 1) {
    const next = `${base} ${n}`;
    if (!used.has(labelKey(next))) return next;
  }
}

export function labelSlug(label: string): string {
  const slug = label
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48)
    .replace(/-+$/, "");
  return slug || "connection";
}

function freshId(label: string, taken: ReadonlySet<string>): string {
  const base = labelSlug(label);
  if (base !== LOCAL_ID && !taken.has(base)) return base;
  for (let n = 2; ; n += 1) {
    const next = `${base}-${n}`;
    if (!taken.has(next)) return next;
  }
}

/** How a profile shows in a roster spanning several gateways: bare when unique, else suffixed. */
export function agentHandle(profile: string, label: string, duplicated: boolean): string {
  const name = profile.trim() || "default";
  return `@${duplicated ? `${name}-${labelSlug(label)}` : name}`;
}

/** Handles for every (connection, profile) of a roster; same-named profiles get the label. */
export function rosterHandles(
  rosters: ReadonlyArray<{ connectionId: string; label: string; profiles: readonly string[] }>,
): Map<string, string> {
  const counts = new Map<string, number>();
  for (const roster of rosters)
    for (const profile of new Set(roster.profiles))
      counts.set(profile, (counts.get(profile) ?? 0) + 1);
  const out = new Map<string, string>();
  for (const roster of rosters) {
    for (const profile of roster.profiles) {
      const handle = agentHandle(profile, roster.label, (counts.get(profile) ?? 0) > 1);
      out.set(`${roster.connectionId}::${profile}`, handle);
    }
  }
  return out;
}

// -- identity -------------------------------------------------------------------------------------

const HOST_RE = /^[A-Za-z0-9._-]+$|^\[?[0-9A-Fa-f:.]+(%[A-Za-z0-9._-]+)?\]?$/;

/** A checked SSH target, or null. */
export function normalizeSsh(raw: unknown): SshTarget | null {
  if (raw === null || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const host = typeof record.host === "string" ? record.host.trim() : "";
  if (host === "" || host.startsWith("-") || !HOST_RE.test(host)) return null;
  const out: SshTarget = { host };
  if (typeof record.user === "string" && record.user.trim() !== "") {
    const user = record.user.trim();
    if (user.startsWith("-") || !/^[A-Za-z0-9._-]+$/.test(user)) return null;
    out.user = user;
  }
  if (record.port !== undefined && record.port !== null && record.port !== "") {
    const port = Number(record.port);
    if (!Number.isInteger(port) || port < 1 || port > 65_535) return null;
    if (port !== 22) out.port = port;
  }
  if (typeof record.keyPath === "string" && record.keyPath.trim() !== "") {
    const key = record.keyPath.trim();
    if (key.startsWith("-") || /[\r\n\0]/.test(key)) return null;
    out.keyPath = key;
  }
  if (typeof record.remoteProfile === "string" && record.remoteProfile.trim() !== "") {
    const profile = record.remoteProfile.trim();
    if (!PROFILE_RE.test(profile)) return null;
    out.remoteProfile = profile;
  }
  return out;
}

/** The identity two entries share when they are the same server. */
export function identityOf(entry: Pick<ConnectionEntry, "kind" | "url" | "ssh">): string {
  if (entry.kind === "local") return "local";
  if (entry.kind === "remote") return `remote:${entry.url ?? ""}`;
  const ssh = entry.ssh;
  if (ssh === undefined) return "ssh:";
  return `ssh:${ssh.user ?? ""}@${ssh.host.toLowerCase()}:${ssh.port ?? 22}/${ssh.remoteProfile ?? "default"}`;
}

function hostLabel(entry: { kind: ConnectionKind; url?: string; ssh?: SshTarget }): string {
  if (entry.kind === "ssh") return entry.ssh?.host ?? "SSH";
  try {
    return new URL(entry.url ?? "").host || "Remote";
  } catch {
    return "Remote";
  }
}

// -- normalise ------------------------------------------------------------------------------------

function localEntry(label = LOCAL_LABEL): ConnectionEntry {
  return { id: LOCAL_ID, kind: "local", label };
}

function plainCopy(value: unknown): unknown {
  try {
    return JSON.parse(JSON.stringify(value)) as unknown;
  } catch {
    return { unserializable: true };
  }
}

function profilesOf(raw: unknown): string[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const names = raw.filter((p): p is string => typeof p === "string" && PROFILE_RE.test(p));
  return names.length > 0 ? [...new Set(names)] : undefined;
}

/** Any parsed JSON → a valid registry (see the file notes); never throws. */
export function normalizeRegistry(raw: unknown): ConnectionRegistry {
  const parsed = raw !== null && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const quarantined: QuarantinedEntry[] = [];
  const quarantine = (reason: string, entry: unknown) => {
    if (quarantined.length < QUARANTINE_CAP) quarantined.push({ reason, entry: plainCopy(entry) });
  };
  if (Array.isArray(parsed.quarantined)) {
    for (const item of parsed.quarantined) {
      if (item !== null && typeof item === "object" && "entry" in item) {
        const record = item as { reason?: unknown; entry: unknown };
        quarantine(typeof record.reason === "string" ? record.reason : "unknown", record.entry);
      }
    }
  }
  const connections: ConnectionEntry[] = [];
  const ids = new Set<string>();
  const labels: string[] = [];
  const identities = new Set<string>();
  const list = Array.isArray(parsed.connections) ? parsed.connections : [];
  for (const item of list) {
    if (item === null || item === undefined || item === false || item === "") continue;
    if (typeof item !== "object" || Array.isArray(item)) {
      quarantine("malformed", item);
      continue;
    }
    try {
      const record = item as Record<string, unknown>;
      const kind = record.kind;
      if (kind !== "local" && kind !== "remote" && kind !== "ssh") {
        quarantine("unknown-kind", item);
        continue;
      }
      if (kind === "local" && ids.has(LOCAL_ID)) {
        quarantine("second-local", item);
        continue;
      }
      const clean: ConnectionEntry = { id: "", kind, label: "" };
      if (kind === "remote") {
        try {
          clean.url = normalizeRemoteUrl(typeof record.url === "string" ? record.url : "");
        } catch {
          quarantine("bad-url", item);
          continue;
        }
        const mode = record.authMode;
        clean.authMode = mode === "oauth" || mode === "basic" ? mode : "token";
      } else if (kind === "ssh") {
        const ssh = normalizeSsh(record.ssh);
        if (ssh === null) {
          quarantine("bad-ssh", item);
          continue;
        }
        clean.ssh = ssh;
      }
      const identity = identityOf(clean);
      if (kind !== "local" && identities.has(identity)) {
        quarantine("duplicate", item);
        continue;
      }
      const rawLabel = typeof record.label === "string" ? record.label.trim() : "";
      clean.label = uniqueLabel(
        rawLabel || (kind === "local" ? LOCAL_LABEL : hostLabel(clean)),
        labels,
      );
      const rawId = typeof record.id === "string" ? record.id.trim() : "";
      clean.id =
        kind === "local"
          ? LOCAL_ID
          : ID_RE.test(rawId) && rawId !== LOCAL_ID && !ids.has(rawId)
            ? rawId
            : freshId(clean.label, ids);
      const profiles = profilesOf(record.profiles);
      if (profiles !== undefined) clean.profiles = profiles;
      ids.add(clean.id);
      labels.push(clean.label);
      identities.add(identity);
      connections.push(clean);
    } catch {
      quarantine("unreadable", item);
    }
  }
  if (!ids.has(LOCAL_ID)) {
    const label = uniqueLabel(LOCAL_LABEL, labels);
    connections.unshift(localEntry(label));
    ids.add(LOCAL_ID);
  }
  const primaryId =
    typeof parsed.primaryId === "string" && ids.has(parsed.primaryId) ? parsed.primaryId : LOCAL_ID;
  const registry: ConnectionRegistry = {
    v: REGISTRY_VERSION,
    connections,
    primaryId,
    launchMode: parsed.launchMode === "last-used" ? "last-used" : "primary",
    quarantined,
  };
  if (typeof parsed.lastUsedId === "string" && ids.has(parsed.lastUsedId)) {
    registry.lastUsedId = parsed.lastUsedId;
  }
  return registry;
}

// -- changes (pure: each returns a new registry) --------------------------------------------------

export interface EntryInput {
  id?: string;
  kind: "remote" | "ssh";
  label?: string;
  url?: string;
  authMode?: AuthMode;
  ssh?: SshTarget;
}

/** Add or update an entry; an existing server (same identity or id) keeps its id and label. */
export function upsertEntry(
  registry: ConnectionRegistry,
  input: EntryInput,
): { registry: ConnectionRegistry; entry: ConnectionEntry; created: boolean } {
  const draft: ConnectionEntry = { id: "", kind: input.kind, label: "" };
  if (input.kind === "remote") {
    try {
      draft.url = normalizeRemoteUrl(input.url ?? "");
    } catch (error) {
      throw new RegistryError("bad-entry", error instanceof Error ? error.message : "bad address");
    }
    draft.authMode = input.authMode ?? "token";
  } else {
    const ssh = normalizeSsh(input.ssh);
    if (ssh === null) throw new RegistryError("bad-entry", "that is not a usable SSH target");
    draft.ssh = ssh;
  }
  const identity = identityOf(draft);
  const existing =
    registry.connections.find((c) => input.id !== undefined && c.id === input.id) ??
    registry.connections.find((c) => c.kind !== "local" && identityOf(c) === identity);
  const others = registry.connections.filter((c) => c.id !== existing?.id);
  const clash = others.find((c) => c.kind !== "local" && identityOf(c) === identity);
  if (clash !== undefined) {
    throw new RegistryError("bad-entry", `"${clash.label}" already connects to that server`);
  }
  const wanted = input.label?.trim() ?? "";
  draft.label = uniqueLabel(
    wanted || existing?.label || hostLabel(draft),
    others.map((c) => c.label),
  );
  draft.id = existing?.id ?? freshId(draft.label, new Set(registry.connections.map((c) => c.id)));
  if (existing?.profiles !== undefined) draft.profiles = existing.profiles;
  const connections =
    existing === undefined
      ? [...registry.connections, draft]
      : registry.connections.map((c) => (c.id === existing.id ? draft : c));
  return { registry: { ...registry, connections }, entry: draft, created: existing === undefined };
}

export function removeEntry(registry: ConnectionRegistry, id: string): ConnectionRegistry {
  const target = registry.connections.find((c) => c.id === id);
  if (target === undefined) return registry;
  if (target.kind === "local") {
    throw new RegistryError("local-not-removable", "This computer cannot be removed");
  }
  const primaryId = registry.primaryId === id ? LOCAL_ID : registry.primaryId;
  const next: ConnectionRegistry = {
    ...registry,
    primaryId,
    connections: registry.connections.filter((c) => c.id !== id),
  };
  if (registry.lastUsedId === id) delete next.lastUsedId;
  return next;
}

function known(registry: ConnectionRegistry, id: string): void {
  if (!registry.connections.some((c) => c.id === id)) {
    throw new RegistryError("unknown-connection", "no connection has that id");
  }
}

export function setPrimary(registry: ConnectionRegistry, id: string): ConnectionRegistry {
  known(registry, id);
  return { ...registry, primaryId: id };
}

export function setLastUsed(registry: ConnectionRegistry, id: string): ConnectionRegistry {
  known(registry, id);
  return { ...registry, lastUsedId: id };
}

export function setLaunchMode(registry: ConnectionRegistry, mode: LaunchMode): ConnectionRegistry {
  return { ...registry, launchMode: mode };
}

export function setProfiles(
  registry: ConnectionRegistry,
  id: string,
  profiles: readonly string[],
): ConnectionRegistry {
  known(registry, id);
  const clean = profilesOf([...profiles]);
  return {
    ...registry,
    connections: registry.connections.map((c) => {
      if (c.id !== id) return c;
      const { profiles: _old, ...rest } = c;
      return clean === undefined ? rest : { ...rest, profiles: clean };
    }),
  };
}

/** The entry a launch opens (launch mode decides; anything missing falls back to the primary). */
export function launchEntry(registry: ConnectionRegistry): ConnectionEntry {
  const byId = (id: string | undefined) => registry.connections.find((c) => c.id === id);
  const chosen =
    registry.launchMode === "last-used"
      ? (byId(registry.lastUsedId) ?? byId(registry.primaryId))
      : byId(registry.primaryId);
  return chosen ?? (byId(LOCAL_ID) as ConnectionEntry);
}

// -- the file -------------------------------------------------------------------------------------

export interface SecretsLike {
  get(name: string): string | null;
  put(name: string, value: string): void;
  delete(name: string): void;
}

export interface RegistryStoreDeps {
  userData: string;
  secrets: SecretsLike;
  log: (line: string) => void;
}

/** Load and save the registry; the first load takes over the P20 single saved remote. */
export class RegistryStore {
  private readonly deps: RegistryStoreDeps;

  constructor(deps: RegistryStoreDeps) {
    this.deps = deps;
  }

  file(): string {
    return path.join(this.deps.userData, REGISTRY_FILE);
  }

  load(): ConnectionRegistry {
    const exists = fs.existsSync(this.file());
    if (!exists) return this.migrate();
    let raw: unknown = null;
    try {
      raw = readJsonFile(this.file());
    } catch {
      raw = null;
    }
    return normalizeRegistry(raw);
  }

  save(registry: ConnectionRegistry): void {
    const clean = normalizeRegistry(registry);
    fs.mkdirSync(this.deps.userData, { recursive: true });
    writeFileAtomic(this.file(), `${JSON.stringify(clean, null, 2)}\n`, { mode: 0o600 });
  }

  /** Change the registry under one load → change → save. */
  update(change: (registry: ConnectionRegistry) => ConnectionRegistry): ConnectionRegistry {
    const next = change(this.load());
    this.save(next);
    return this.load();
  }

  /** Drop an entry and its secrets. */
  remove(id: string): ConnectionRegistry {
    const next = this.update((registry) => removeEntry(registry, id));
    for (const kind of ["token", "oauth", "headers"] as const) {
      this.deps.secrets.delete(secretName(id, kind));
    }
    return next;
  }

  private migrate(): ConnectionRegistry {
    const legacy = readJsonFile(path.join(this.deps.userData, REMOTE_FILE)) as {
      url?: unknown;
    } | null;
    let registry = normalizeRegistry({});
    if (legacy !== null && typeof legacy.url === "string") {
      try {
        const added = upsertEntry(registry, { kind: "remote", url: legacy.url });
        registry = setPrimary(added.registry, added.entry.id);
        const token = this.deps.secrets.get(REMOTE_TOKEN_SECRET);
        if (token !== null) this.deps.secrets.put(secretName(added.entry.id, "token"), token);
        this.deps.log("[connections] took the saved remote connection into the registry");
      } catch {
        this.deps.log("[connections] the saved remote connection could not be taken over");
      }
    }
    this.save(registry);
    return this.load();
  }
}
