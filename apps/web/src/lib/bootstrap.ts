// Values the dashboard backend writes into index.html before `</head>` (P18-01). They are read at
// call time, not at import, so tests can set them per case.

declare global {
  interface Window {
    __INDUS_SESSION_TOKEN__?: string;
    __INDUS_BASE_PATH__?: string;
    __INDUS_AUTH_REQUIRED__?: boolean;
    __INDUS_DASHBOARD_EMBEDDED_CHAT__?: boolean;
    __INDUS_INITIAL_PROFILE__?: string;
    __INDUS_DASHBOARD_PROFILE__?: string;
  }
}

function host(): Window | undefined {
  return typeof window === "undefined" ? undefined : window;
}

/** The URL prefix the SPA is served under: "" at the root, else "/a/b" (no trailing slash). */
export function basePath(): string {
  const raw = host()?.__INDUS_BASE_PATH__ ?? "";
  const trimmed = raw.trim().replace(/\/+$/, "");
  if (!trimmed) return "";
  return trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
}

/** The loopback session token, absent in gated mode. */
export function sessionToken(): string | undefined {
  const token = host()?.__INDUS_SESSION_TOKEN__;
  return token ? token : undefined;
}

/** True when a login provider guards the dashboard (gated mode). */
export function authRequired(): boolean {
  return host()?.__INDUS_AUTH_REQUIRED__ === true;
}

/** Whether the backend serves the embedded chat terminal; on unless it says otherwise. */
export function embeddedChatEnabled(): boolean {
  return host()?.__INDUS_DASHBOARD_EMBEDDED_CHAT__ !== false;
}

/** The profile named in the launch (`indus dashboard -p NAME` opens `?profile=NAME`). */
export function initialProfile(): string {
  return (host()?.__INDUS_INITIAL_PROFILE__ ?? "").trim();
}

/** The profile this backend process itself serves. */
export function servingProfile(): string {
  return (host()?.__INDUS_DASHBOARD_PROFILE__ ?? "").trim();
}
