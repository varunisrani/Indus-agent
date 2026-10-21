// Sign-in calls for the auth widget (P18-28): who am I, which providers exist, and sign-out.
// They go through the one REST client so the base path and the loopback header are the same as
// everywhere else.

import { assignLocation, LAST_LOCATION_KEY, rawRequest, request } from "./api.ts";
import { basePath } from "./bootstrap.ts";

/** `GET /api/auth/me`: a gated session, or the fixed local identity in loopback mode. */
export interface AuthIdentity {
  /** `loopback` in loopback mode, else the provider that signed the session in. */
  provider: string;
  /** Opaque and possibly long; the sidebar shows `shortUserId` of it. */
  user_id: string;
  /** Empty when the provider gave none (then only the id is shown). */
  display_name: string;
  email: string;
  org_id: string;
  /** Unix seconds; 0 for the loopback identity. */
  expires_at: number;
}

export interface AuthProvider {
  name: string;
  display_name: string;
  kind: "password" | "redirect";
  supports_password: boolean;
}

/** `GET /api/auth/providers` (public). */
export interface AuthProviders {
  auth_required: boolean;
  providers: AuthProvider[];
}

export const LOOPBACK_PROVIDER = "loopback";
/** How much of an opaque user id the sidebar shows. */
export const USER_ID_CHARS = 14;

/**
 * Who is signed in. `allowUnauthorized` keeps a loopback 401 (a stale token) local to the widget
 * instead of starting the global reload-once path; a gated 401 still goes to the login page.
 */
export function fetchIdentity(): Promise<AuthIdentity> {
  return request<AuthIdentity>("/api/auth/me", { allowUnauthorized: true });
}

export function fetchProviders(): Promise<AuthProviders> {
  return request<AuthProviders>("/api/auth/providers", { allowUnauthorized: true });
}

/** The identity the widget hides: the loopback stand-in means there is nothing to sign out of. */
export function isLocalIdentity(identity: AuthIdentity): boolean {
  return identity.provider === LOOPBACK_PROVIDER;
}

/** The user id cut to `USER_ID_CHARS` characters, with an ellipsis when cut. */
export function shortUserId(id: string): string {
  const chars = Array.from(id);
  if (chars.length <= USER_ID_CHARS) return id;
  return `${chars.slice(0, USER_ID_CHARS).join("")}…`;
}

function forgetLastLocation(): void {
  try {
    window.sessionStorage.removeItem(LAST_LOCATION_KEY);
  } catch {
    // storage unavailable: nothing to forget
  }
}

/**
 * Sign out: the server revokes what it can, clears every auth cookie and answers with a redirect
 * to the login page, which the whole page then follows. Rejects (and stays put) when no answer
 * came back, since the cookies are then still set.
 */
export async function signOut(): Promise<void> {
  await rawRequest("/auth/logout", { method: "POST" });
  forgetLastLocation();
  assignLocation(`${basePath()}/login`);
}

/**
 * After a sign-in, the place the browser was when a gated 401 sent it to the login page: read
 * once and forgotten. Only an in-app path under the base path counts; the result is relative to
 * the router's base.
 */
export function takeLastLocation(): string | null {
  let stored: string | null = null;
  try {
    stored = window.sessionStorage.getItem(LAST_LOCATION_KEY);
  } catch {
    return null;
  }
  forgetLastLocation();
  if (!stored?.startsWith("/") || stored.startsWith("//") || stored.includes("\\")) return null;
  const base = basePath();
  let inApp = stored;
  if (base) {
    if (stored !== base && !stored.startsWith(`${base}/`) && !stored.startsWith(`${base}?`)) {
      return null;
    }
    inApp = stored.slice(base.length) || "/";
  }
  if (inApp === "/login" || inApp.startsWith("/login?") || inApp.startsWith("/auth/")) return null;
  return inApp.startsWith("/") ? inApp : `/${inApp}`;
}
