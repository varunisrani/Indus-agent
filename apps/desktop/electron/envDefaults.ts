// Env defaults baked into the bundle at build time (a packager's non-secret choices, such as a
// fixed home for a portable build). They are applied first thing in main, before Electron
// resolves its paths, with one precedence rule:
//
//   the runtime env wins whenever it names the key — an empty value counts as named;
//   a string default fills only an absent key;
//   a null default clears the key (to empty), whatever the runtime env says.
//
// Only allow-listed keys are accepted; anything else fails the build (`validateEnvDefaults`) and
// is ignored at run time.

export const ENV_DEFAULT_KEYS: ReadonlySet<string> = new Set([
  "INDUS_HOME",
  "INDUS_DESKTOP_USER_DATA_DIR",
]);

export type EnvDefaults = Readonly<Record<string, string | null>>;

export class EnvDefaultsError extends Error {}

/** Check a defaults table (build time). Returns it typed, or throws `EnvDefaultsError`. */
export function validateEnvDefaults(value: unknown): EnvDefaults {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new EnvDefaultsError("env defaults must be a JSON object");
  }
  for (const [key, entry] of Object.entries(value)) {
    if (!ENV_DEFAULT_KEYS.has(key)) {
      throw new EnvDefaultsError(`env default ${JSON.stringify(key)} is not an allowed key`);
    }
    if (entry !== null && (typeof entry !== "string" || entry.includes("\0"))) {
      throw new EnvDefaultsError(`env default ${key} must be a string without NUL, or null`);
    }
  }
  return value as EnvDefaults;
}

/** Parse the JSON the build baked in; a bad table is ignored (empty), never fatal at run time. */
export function parseBakedEnvDefaults(raw: string | undefined): EnvDefaults {
  if (!raw) return {};
  try {
    return validateEnvDefaults(JSON.parse(raw));
  } catch {
    return {};
  }
}

/** Apply `defaults` to `env` in place, by the precedence rule above. Returns the keys changed. */
export function applyEnvDefaults(
  env: Record<string, string | undefined>,
  defaults: EnvDefaults,
): string[] {
  const changed: string[] = [];
  for (const [key, value] of Object.entries(defaults)) {
    if (!ENV_DEFAULT_KEYS.has(key)) continue;
    if (value === null) {
      if (env[key] !== "") changed.push(key);
      env[key] = "";
    } else if (env[key] === undefined) {
      env[key] = value;
      changed.push(key);
    }
  }
  return changed;
}
