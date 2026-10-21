// Permission requests and checks, per origin, deny by default. Only the app's own pages may use a
// short list of permissions (copying to the clipboard, notifications, fullscreen and audio-only
// media for voice); every other origin — preview guests included — gets nothing.

import { type AppOrigin, isAppUrl } from "../appOrigin.ts";

export const APP_PERMISSIONS: ReadonlySet<string> = new Set([
  "clipboard-sanitized-write",
  "notifications",
  "fullscreen",
  "media",
]);

export interface PermissionDetails {
  requestingUrl?: string;
  /** For `media`: the kinds asked for (`audio`, `video`). */
  mediaTypes?: string[];
  mediaType?: string;
  isMainFrame?: boolean;
}

export interface PermissionPolicyOptions {
  appOrigin: AppOrigin;
  log: (line: string) => void;
}

function origin(url: string): string {
  try {
    return new URL(url).origin;
  } catch {
    return "<not a url>";
  }
}

/** Decide one permission. `url` is the requesting frame's URL. */
export function permissionAllowed(
  permission: string,
  url: string,
  details: PermissionDetails,
  appOrigin: AppOrigin,
): boolean {
  if (!APP_PERMISSIONS.has(permission)) return false;
  if (!isAppUrl(url, appOrigin)) return false;
  if (details.isMainFrame === false) return false;
  if (permission === "media") {
    const kinds = details.mediaTypes ?? (details.mediaType ? [details.mediaType] : []);
    // Voice needs the microphone only; a camera or screen request is refused.
    return kinds.length > 0 && kinds.every((kind) => kind === "audio");
  }
  return true;
}

export interface PermissionSession {
  setPermissionRequestHandler(
    handler: (
      contents: unknown,
      permission: string,
      callback: (granted: boolean) => void,
      details: PermissionDetails,
    ) => void,
  ): void;
  setPermissionCheckHandler(
    handler: (
      contents: unknown,
      permission: string,
      requestingOrigin: string,
      details: PermissionDetails,
    ) => boolean,
  ): void;
}

/** Install the policy on a session (the app's default session, the preview partition). */
export function installPermissionPolicy(
  session: PermissionSession,
  options: PermissionPolicyOptions,
): void {
  session.setPermissionRequestHandler((_contents, permission, callback, details) => {
    const url = details?.requestingUrl ?? "";
    const granted = permissionAllowed(permission, url, details ?? {}, options.appOrigin);
    if (!granted) options.log(`[security] permission ${permission} denied for ${origin(url)}`);
    callback(granted);
  });
  session.setPermissionCheckHandler((_contents, permission, requestingOrigin, details) => {
    const url = details?.requestingUrl ?? requestingOrigin ?? "";
    return permissionAllowed(permission, url, details ?? {}, options.appOrigin);
  });
}

/** For partitions that host untrusted pages: every request and check is denied. */
export function installDenyAllPermissions(session: PermissionSession): void {
  session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  session.setPermissionCheckHandler(() => false);
}
