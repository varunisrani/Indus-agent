// The profile id rule (P01, `foundation.profiles.normalize_id`), checked in the browser so the
// builder can refuse a bad name before anything is sent. The server checks again.

export const PROFILE_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,63}$/;
export const RESERVED_PROFILE_IDS: ReadonlySet<string> = new Set([
  "indus",
  "default",
  "test",
  "tmp",
  "root",
  "sudo",
]);

/** Trimmed and lower-cased, as the server stores it. */
export function normaliseProfileName(raw: string): string {
  return raw.trim().toLowerCase();
}

/** Why `raw` cannot name a new profile, or null when it can. */
export function profileNameProblem(raw: string, existing: readonly string[] = []): string | null {
  const name = normaliseProfileName(raw);
  if (!name) return "Give the profile a name.";
  if (name.length > 64) return "Use at most 64 characters.";
  if (!PROFILE_ID_PATTERN.test(name)) {
    return "Use lower-case letters, digits, '-' or '_', starting with a letter or digit.";
  }
  if (RESERVED_PROFILE_IDS.has(name)) return `"${name}" is reserved; pick another name.`;
  if (existing.includes(name)) return `A profile named "${name}" already exists.`;
  return null;
}
