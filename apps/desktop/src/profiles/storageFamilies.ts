// The one registry of the renderer's local-storage families (P20-26, R08.7).
//
// A family keyed by profile must move with a profile rename and go away with a profile delete,
// or every restore after a rename points at a profile that no longer exists. Every key the
// renderer writes is listed here: either as a profile family (`perKey`: one key per profile,
// `prefix + profile`; `record`: one key holding `{ [profile]: value }`) or as a window-wide key
// that does not depend on the profile. `storageFamilies.test.ts` scans the sources for key
// literals and fails on one that is not listed, so a new family cannot skip the migration.

export type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem" | "key"> & {
  readonly length: number;
};

export type ProfileFamily =
  | { readonly id: string; readonly kind: "perKey"; readonly prefix: string }
  | { readonly id: string; readonly kind: "record"; readonly key: string };

/** Families keyed by profile: migrated on rename, dropped on delete. */
export const PROFILE_FAMILIES: readonly ProfileFamily[] = [
  // P20-23: the sticky model per profile and the "first pick wrote the default" marker
  { id: "model-sticky", kind: "perKey", prefix: "indus.desktop.model." },
  { id: "model-default-checked", kind: "perKey", prefix: "indus.desktop.model-default-checked." },
  // P20-25: unread watermarks, projects and workspace settings, sidebar view state
  { id: "sidebar-seen", kind: "perKey", prefix: "indus.desktop.sidebar.seen." },
  { id: "workspace", kind: "perKey", prefix: "indus.desktop.workspace." },
  { id: "sidebar-view", kind: "perKey", prefix: "indus.desktop.sidebar.view." },
  // P20-25: the folder a fresh draft starts in (cleared once used)
  { id: "new-chat-target", kind: "record", key: "indus.desktop.new-chat-target" },
  // P24-33: a profile's own theme and chat / terminal fonts
  { id: "appearance-profiles", kind: "record", key: "indus.desktop.appearance.profiles" },
];

/** Keys that are the same for every profile (left alone by rename and delete). */
export const WINDOW_KEYS: readonly string[] = [
  "indus-desktop-theme", // P20-15
  "indus-desktop-appearance", // P24-33 (preset, accent, user themes)
  "indus.desktop.statusbar.items", // P20-23
  "indus.desktop.model-presets", // P20-23 (per model, not per profile)
  "indus.desktop.model-favourites", // P20-23
  "indus.desktop.inflight.v1", // P20-20 (per session; pruned by age)
  "indus.desktop.image-drafts.v1", // P20-20
  "indus.desktop.panes.v1", // P20-27
  "indus.desktop.plugins.decisions", // P24-32 (the person's on/off choice per desktop plugin)
];

/** The profile part of a key: `null` and blank mean the default profile. */
export function profileKey(profile: string | null | undefined): string {
  const trimmed = (profile ?? "").trim();
  return trimmed === "" ? "default" : trimmed;
}

/** The storage key of a per-key family for one profile. */
export function familyKey(
  family: ProfileFamily & { kind: "perKey" },
  profile: string | null,
): string {
  return `${family.prefix}${profileKey(profile)}`;
}

export function perKeyFamily(id: string): ProfileFamily & { kind: "perKey" } {
  const family = PROFILE_FAMILIES.find((f) => f.id === id);
  if (family === undefined || family.kind !== "perKey") throw new Error(`no per-key family ${id}`);
  return family;
}

export function recordFamily(id: string): ProfileFamily & { kind: "record" } {
  const family = PROFILE_FAMILIES.find((f) => f.id === id);
  if (family === undefined || family.kind !== "record") throw new Error(`no record family ${id}`);
  return family;
}

/** Is a literal key (or key prefix) covered by the registry? */
export function isRegisteredKey(key: string): boolean {
  if (WINDOW_KEYS.includes(key)) return true;
  return PROFILE_FAMILIES.some((family) =>
    family.kind === "perKey"
      ? key === family.prefix || key.startsWith(family.prefix)
      : key === family.key,
  );
}

function readRecord(storage: StorageLike, key: string): Record<string, unknown> | null {
  let raw: string | null;
  try {
    raw = storage.getItem(key);
  } catch {
    return null;
  }
  if (raw === null) return null;
  try {
    const parsed = JSON.parse(raw) as unknown;
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function safely(fn: () => void): void {
  try {
    fn();
  } catch {
    // storage unavailable or full: nothing more can be done here
  }
}

export interface MigrationReport {
  /** Family ids that held something for the profile. */
  readonly touched: readonly string[];
}

/** Move every family's entry from `from` to `to` (the new name's entry is replaced). */
export function migrateProfileStorage(
  storage: StorageLike,
  from: string | null,
  to: string | null,
): MigrationReport {
  const a = profileKey(from);
  const b = profileKey(to);
  const touched: string[] = [];
  if (a === b) return { touched };
  for (const family of PROFILE_FAMILIES) {
    if (family.kind === "perKey") {
      const oldKey = `${family.prefix}${a}`;
      let value: string | null = null;
      try {
        value = storage.getItem(oldKey);
      } catch {
        value = null;
      }
      if (value === null) continue;
      touched.push(family.id);
      safely(() => storage.setItem(`${family.prefix}${b}`, value as string));
      safely(() => storage.removeItem(oldKey));
    } else {
      const record = readRecord(storage, family.key);
      if (record === null || !(a in record)) continue;
      touched.push(family.id);
      const { [a]: moved, ...rest } = record;
      safely(() => storage.setItem(family.key, JSON.stringify({ ...rest, [b]: moved })));
    }
  }
  return { touched };
}

/** Remove every family's entry for a deleted profile. */
export function dropProfileStorage(storage: StorageLike, profile: string | null): MigrationReport {
  const a = profileKey(profile);
  const touched: string[] = [];
  for (const family of PROFILE_FAMILIES) {
    if (family.kind === "perKey") {
      const key = `${family.prefix}${a}`;
      let present = false;
      try {
        present = storage.getItem(key) !== null;
      } catch {
        present = false;
      }
      if (!present) continue;
      touched.push(family.id);
      safely(() => storage.removeItem(key));
    } else {
      const record = readRecord(storage, family.key);
      if (record === null || !(a in record)) continue;
      touched.push(family.id);
      const { [a]: _dropped, ...rest } = record;
      safely(() => storage.setItem(family.key, JSON.stringify(rest)));
    }
  }
  return { touched };
}

/** Every stored key that names a profile, with its profile (for diagnostics and tests). */
export function profileKeysIn(storage: StorageLike): Array<{ key: string; profile: string }> {
  const out: Array<{ key: string; profile: string }> = [];
  for (let i = 0; i < storage.length; i += 1) {
    const key = storage.key(i);
    if (key === null) continue;
    for (const family of PROFILE_FAMILIES) {
      if (family.kind === "perKey" && key.startsWith(family.prefix)) {
        out.push({ key, profile: key.slice(family.prefix.length) });
      } else if (family.kind === "record" && key === family.key) {
        for (const profile of Object.keys(readRecord(storage, key) ?? {}))
          out.push({ key, profile });
      }
    }
  }
  return out;
}

/** `window.localStorage` when there is one. */
export function browserStorage(): StorageLike | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}
