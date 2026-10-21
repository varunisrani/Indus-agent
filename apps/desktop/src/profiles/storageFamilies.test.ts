import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  dropProfileStorage,
  isRegisteredKey,
  migrateProfileStorage,
  PROFILE_FAMILIES,
  profileKeysIn,
  type StorageLike,
} from "./storageFamilies.ts";

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...sources(full));
    else if (/\.(ts|tsx)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

/** Storage-key literals (or the literal head of a template key) in a source text. */
export function storageKeyLiterals(text: string): string[] {
  return [...text.matchAll(/["'`](indus[.-]desktop[.-][A-Za-z0-9._-]*)/g)].map(
    (match) => match[1] as string,
  );
}

class MemoryStorage implements StorageLike {
  readonly data = new Map<string, string>();
  get length(): number {
    return this.data.size;
  }
  key(index: number): string | null {
    return [...this.data.keys()][index] ?? null;
  }
  getItem(key: string): string | null {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }
  removeItem(key: string): void {
    this.data.delete(key);
  }
}

/** One entry per family for `profile` (and one for an unrelated profile). */
function seed(storage: MemoryStorage, profile: string): void {
  for (const family of PROFILE_FAMILIES) {
    if (family.kind === "perKey") {
      storage.setItem(`${family.prefix}${profile}`, JSON.stringify({ family: family.id }));
      storage.setItem(`${family.prefix}other`, JSON.stringify({ family: family.id }));
    } else {
      storage.setItem(
        family.key,
        JSON.stringify({ [profile]: { family: family.id }, other: { keep: true } }),
      );
    }
  }
  storage.setItem("indus-desktop-theme", "dark");
}

describe("profile storage families", () => {
  it("every storage key in the renderer's sources is registered (lint)", () => {
    const missing: string[] = [];
    for (const file of sources(SRC)) {
      for (const key of storageKeyLiterals(fs.readFileSync(file, "utf8"))) {
        if (!isRegisteredKey(key)) missing.push(`${path.relative(SRC, file)}: ${key}`);
      }
    }
    expect(missing).toEqual([]);
    // the scan does find keys (guards against a regex that silently matches nothing)
    const all = sources(SRC).flatMap((file) => storageKeyLiterals(fs.readFileSync(file, "utf8")));
    expect(all.length).toBeGreaterThan(10);
  });

  it("the lint catches a new family that skipped the registry", () => {
    const head = "`indus.desktop.new-thing.";
    const source = [
      "const KEY = ",
      head,
      "$",
      "{profile}`; ",
      'storage.getItem("indus.desktop.statusbar.items");',
    ].join("");
    const unlisted = storageKeyLiterals(source).filter((key) => !isRegisteredKey(key));
    expect(unlisted).toEqual(["indus.desktop.new-thing."]);
  });

  it("rename migrates every registered family and leaves the rest", () => {
    const storage = new MemoryStorage();
    seed(storage, "work");
    const report = migrateProfileStorage(storage, "work", "client");
    expect(report.touched).toEqual(PROFILE_FAMILIES.map((family) => family.id));
    const owners = profileKeysIn(storage).map((entry) => entry.profile);
    expect(owners).not.toContain("work");
    expect(owners.filter((p) => p === "client")).toHaveLength(PROFILE_FAMILIES.length);
    expect(owners.filter((p) => p === "other")).toHaveLength(PROFILE_FAMILIES.length);
    expect(storage.getItem("indus-desktop-theme")).toBe("dark");
    for (const family of PROFILE_FAMILIES) {
      const value =
        family.kind === "perKey"
          ? JSON.parse(storage.getItem(`${family.prefix}client`) ?? "null")
          : JSON.parse(storage.getItem(family.key) ?? "{}").client;
      expect(value).toEqual({ family: family.id });
    }
  });

  it("delete drops every registered family's entry and nothing else", () => {
    const storage = new MemoryStorage();
    seed(storage, "work");
    const report = dropProfileStorage(storage, "work");
    expect(report.touched).toHaveLength(PROFILE_FAMILIES.length);
    const owners = profileKeysIn(storage).map((entry) => entry.profile);
    expect(owners).not.toContain("work");
    expect(owners.filter((p) => p === "other")).toHaveLength(PROFILE_FAMILIES.length);
  });

  it("null and blank mean the default profile; same-name rename is a no-op", () => {
    const storage = new MemoryStorage();
    seed(storage, "default");
    expect(migrateProfileStorage(storage, null, " default ").touched).toEqual([]);
    expect(migrateProfileStorage(storage, null, "home").touched).toHaveLength(
      PROFILE_FAMILIES.length,
    );
  });

  it("a corrupt record family is skipped, not thrown", () => {
    const storage = new MemoryStorage();
    const record = PROFILE_FAMILIES.find((family) => family.kind === "record");
    if (record?.kind !== "record") throw new Error("a record family is expected");
    storage.setItem(record.key, "{oops");
    expect(() => migrateProfileStorage(storage, "a", "b")).not.toThrow();
    expect(() => dropProfileStorage(storage, "a")).not.toThrow();
  });
});
