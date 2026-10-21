import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { REMOTE_FILE, REMOTE_TOKEN_SECRET } from "../backend/remote.ts";
import {
  agentHandle,
  identityOf,
  LOCAL_ID,
  launchEntry,
  normalizeRegistry,
  QUARANTINE_CAP,
  REGISTRY_FILE,
  RegistryError,
  RegistryStore,
  removeEntry,
  rosterHandles,
  secretName,
  setLastUsed,
  setLaunchMode,
  setPrimary,
  upsertEntry,
} from "./registry.ts";

const dirs: string[] = [];
afterAll(() => {
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
});

function tempUserData(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-conn-"));
  dirs.push(dir);
  return dir;
}

function memorySecrets() {
  const values = new Map<string, string>();
  return {
    values,
    get: (name: string) => values.get(name) ?? null,
    put: (name: string, value: string) => void values.set(name, value),
    delete: (name: string) => void values.delete(name),
  };
}

const TOKEN = ["conn", "test", "value"].join("-");

describe("normalizeRegistry", () => {
  it("exactly one local entry exists, whatever the file says", () => {
    expect(normalizeRegistry(null).connections).toEqual([
      { id: LOCAL_ID, kind: "local", label: "This computer" },
    ]);
    const two = normalizeRegistry({
      connections: [
        { id: "local", kind: "local", label: "Laptop" },
        { id: "x", kind: "local", label: "Other" },
      ],
    });
    expect(two.connections.filter((c) => c.kind === "local")).toEqual([
      { id: LOCAL_ID, kind: "local", label: "Laptop" },
    ]);
    expect(two.quarantined.map((q) => q.reason)).toEqual(["second-local"]);
  });

  it("a malformed entry survives a save verbatim", () => {
    const dir = tempUserData();
    const store = new RegistryStore({ userData: dir, secrets: memorySecrets(), log: () => {} });
    const odd = { kind: "cloud", label: "Hosted", org: "acme", future: { nested: [1, 2] } };
    fs.writeFileSync(
      path.join(dir, REGISTRY_FILE),
      JSON.stringify({
        connections: [
          odd,
          "hand-edited",
          { id: "r1", kind: "remote", label: "Work", url: "https://work.example:9119" },
          { kind: "remote", url: "ftp://bad" },
        ],
      }),
    );
    const loaded = store.load();
    expect(loaded.connections.map((c) => c.id)).toEqual(["local", "r1"]);
    expect(loaded.quarantined).toEqual([
      { reason: "unknown-kind", entry: odd },
      { reason: "malformed", entry: "hand-edited" },
      { reason: "bad-url", entry: { kind: "remote", url: "ftp://bad" } },
    ]);
    store.update((registry) => setPrimary(registry, "r1"));
    const written = JSON.parse(fs.readFileSync(path.join(dir, REGISTRY_FILE), "utf8"));
    expect(written.quarantined[0]).toEqual({ reason: "unknown-kind", entry: odd });
    expect(store.load().quarantined).toHaveLength(3);
    expect(fs.statSync(path.join(dir, REGISTRY_FILE)).mode & 0o777).toBe(0o600);
  });

  it("caps the quarantine", () => {
    const junk = Array.from({ length: QUARANTINE_CAP + 5 }, (_, i) => `junk-${i}`);
    expect(normalizeRegistry({ connections: junk }).quarantined).toHaveLength(QUARANTINE_CAP);
  });

  it("labels stay unique ignoring case", () => {
    const registry = normalizeRegistry({
      connections: [
        { id: "a", kind: "remote", label: "Office", url: "http://a.example" },
        { id: "b", kind: "remote", label: "office", url: "http://b.example" },
      ],
    });
    expect(registry.connections.map((c) => c.label)).toEqual([
      "This computer",
      "Office",
      "office 2",
    ]);
    const added = upsertEntry(registry, {
      kind: "remote",
      url: "http://c.example",
      label: "OFFICE",
    });
    expect(added.entry.label).toBe("OFFICE 3");
  });

  it("never keeps credentials or unknown fields in an entry", () => {
    const registry = normalizeRegistry({
      connections: [
        { id: "r", kind: "remote", url: "http://r.example", token: TOKEN, headers: { A: "b" } },
      ],
    });
    expect(JSON.stringify(registry)).not.toContain(TOKEN);
    expect(registry.connections[1]).toEqual({
      id: "r",
      kind: "remote",
      label: "r.example",
      url: "http://r.example",
      authMode: "token",
    });
  });
});

describe("changes", () => {
  it("dedupes remotes by normalised URL and SSH targets by user@host:port + profile", () => {
    let registry = normalizeRegistry({});
    const first = upsertEntry(registry, {
      kind: "remote",
      url: "work.example:9119/",
      label: "Work",
    });
    registry = first.registry;
    const again = upsertEntry(registry, { kind: "remote", url: "http://work.example:9119" });
    expect(again.created).toBe(false);
    expect(again.entry.id).toBe(first.entry.id);
    expect(again.entry.label).toBe("Work");
    registry = again.registry;
    const ssh = upsertEntry(registry, {
      kind: "ssh",
      ssh: { host: "Build.example", user: "dev", port: 22 },
    });
    registry = ssh.registry;
    const sameHost = upsertEntry(registry, {
      kind: "ssh",
      ssh: { host: "build.example", user: "dev" },
    });
    expect(sameHost.created).toBe(false);
    const otherProfile = upsertEntry(sameHost.registry, {
      kind: "ssh",
      ssh: { host: "build.example", user: "dev", remoteProfile: "research" },
    });
    expect(otherProfile.created).toBe(true);
    expect(otherProfile.registry.connections).toHaveLength(4);
    expect(identityOf(otherProfile.entry)).toBe("ssh:dev@build.example:22/research");
  });

  it("refuses an SSH target that could be read as an option", () => {
    const registry = normalizeRegistry({});
    for (const ssh of [
      { host: "-oProxyCommand=x" },
      { host: "h", user: "-l" },
      { host: "h", port: 70_000 },
      { host: "h", keyPath: "-i\nx" },
    ]) {
      expect(() => upsertEntry(registry, { kind: "ssh", ssh })).toThrow(RegistryError);
    }
  });

  it("removing the primary retargets the local entry", () => {
    let registry = upsertEntry(normalizeRegistry({}), {
      kind: "remote",
      url: "http://r.example",
    }).registry;
    const id = registry.connections[1]?.id as string;
    registry = setLastUsed(setPrimary(registry, id), id);
    const after = removeEntry(registry, id);
    expect(after.primaryId).toBe(LOCAL_ID);
    expect(after.lastUsedId).toBeUndefined();
    expect(() => removeEntry(after, LOCAL_ID)).toThrow(/cannot be removed/);
    expect(removeEntry(after, "ghost")).toBe(after);
  });

  it("launch mode last-used reopens the last entry that worked", () => {
    let registry = upsertEntry(normalizeRegistry({}), {
      kind: "remote",
      url: "http://r.example",
    }).registry;
    const id = registry.connections[1]?.id as string;
    registry = setLastUsed(registry, id);
    expect(launchEntry(registry).id).toBe(LOCAL_ID); // primary mode
    expect(launchEntry(setLaunchMode(registry, "last-used")).id).toBe(id);
    expect(launchEntry(setLaunchMode(removeEntry(registry, id), "last-used")).id).toBe(LOCAL_ID);
  });
});

describe("handles", () => {
  it("same-named profiles on two gateways get the label suffix", () => {
    const handles = rosterHandles([
      { connectionId: "local", label: "This computer", profiles: ["default", "coder"] },
      { connectionId: "box", label: "Build Box", profiles: ["default", "research"] },
    ]);
    expect(handles.get("local::default")).toBe("@default-this-computer");
    expect(handles.get("box::default")).toBe("@default-build-box");
    expect(handles.get("local::coder")).toBe("@coder");
    expect(handles.get("box::research")).toBe("@research");
    expect(agentHandle("", "x", false)).toBe("@default");
  });
});

describe("RegistryStore", () => {
  it("takes over the P20 saved remote once, token into the connection's secret", () => {
    const dir = tempUserData();
    const secrets = memorySecrets();
    secrets.put(REMOTE_TOKEN_SECRET, TOKEN);
    fs.writeFileSync(
      path.join(dir, REMOTE_FILE),
      JSON.stringify({ v: 1, url: "http://old.example:9119" }),
    );
    const store = new RegistryStore({ userData: dir, secrets, log: () => {} });
    const loaded = store.load();
    const remote = loaded.connections.find((c) => c.kind === "remote");
    expect(remote?.url).toBe("http://old.example:9119");
    expect(loaded.primaryId).toBe(remote?.id);
    expect(secrets.get(secretName(remote?.id as string, "token"))).toBe(TOKEN);
    expect(fs.readFileSync(path.join(dir, REGISTRY_FILE), "utf8")).not.toContain(TOKEN);
    // a second load reads the registry, it does not migrate again
    store.update((registry) => removeEntry(registry, remote?.id as string));
    expect(store.load().connections).toHaveLength(1);
  });

  it("remove drops the entry's secrets", () => {
    const dir = tempUserData();
    const secrets = memorySecrets();
    const store = new RegistryStore({ userData: dir, secrets, log: () => {} });
    const registry = store.update(
      (current) => upsertEntry(current, { kind: "remote", url: "http://r.example" }).registry,
    );
    const id = registry.connections[1]?.id as string;
    secrets.put(secretName(id, "token"), TOKEN);
    secrets.put(secretName(id, "headers"), "{}");
    store.remove(id);
    expect(secrets.values.size).toBe(0);
  });
});
