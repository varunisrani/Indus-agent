// Version grammar shared by the updater, the skew checks and the renderer's notice (P20-37).
// Releases are `X.Y.Z` (stable) or `X.Y.Z-canary.<YYYYMMDDTHHMMSSZ>` (canary, D-16.56): a canary
// sorts above the stable before it and below its own stable. Runtime versions may carry other
// suffixes (a source build's `0.1.0.dev3`); anything after `X.Y.Z` sorts below the plain release.
// Pure module: no Node imports (the renderer uses it too).

export interface ParsedVersion {
  major: number;
  minor: number;
  patch: number;
  /** `canary.<stamp>` or another suffix; null for a plain release. */
  pre: string | null;
}

const VERSION = /^v?(\d{1,9})\.(\d{1,9})\.(\d{1,9})(?:[-.+]?(.*))?$/;

export function parseVersion(text: string): ParsedVersion | null {
  const match = VERSION.exec(text.trim());
  if (match === null) return null;
  const pre = (match[4] ?? "").trim();
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
    pre: pre === "" ? null : pre,
  };
}

/** A release tag of ours: `vX.Y.Z` or `vX.Y.Z-canary.<stamp>`. */
export function parseReleaseTag(tag: string): (ParsedVersion & { canary: boolean }) | null {
  const match = /^v?(\d{1,9})\.(\d{1,9})\.(\d{1,9})(-canary\.(\d{8}T\d{6}Z))?$/.exec(tag.trim());
  if (match === null) return null;
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
    pre: match[5] === undefined ? null : `canary.${match[5]}`,
    canary: match[5] !== undefined,
  };
}

/** <0, 0, >0 like a sort comparator; null when either side is not a version. */
export function compareVersions(a: string, b: string): number | null {
  const left = parseVersion(a);
  const right = parseVersion(b);
  if (left === null || right === null) return null;
  for (const key of ["major", "minor", "patch"] as const) {
    if (left[key] !== right[key]) return left[key] - right[key];
  }
  if (left.pre === right.pre) return 0;
  if (left.pre === null) return 1;
  if (right.pre === null) return -1;
  return left.pre < right.pre ? -1 : 1;
}

/** True when `candidate` is strictly newer than `current`. */
export function isNewer(candidate: string, current: string): boolean {
  const order = compareVersions(candidate, current);
  return order !== null && order > 0;
}

export function plainVersion(text: string): string {
  return text.trim().replace(/^v/, "");
}
