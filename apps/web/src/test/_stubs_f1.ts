// FIXTURE: hand-written until P18-11..16 generate the route list; replace it with their output.
// The generated REST route list P18-11..16 produce from the backend (method, path template, and
// whether the route resolves a profile `Scope`). Interface assumed: one entry per route,
// `{ method, path, scoped }`, path templates with `{name}` placeholders. Only the routes the
// prework pages call, plus the families P18-13/15/16 own, are listed here.

export interface GeneratedRoute {
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  scoped: boolean;
}

export const GENERATED_ROUTES: readonly GeneratedRoute[] = [
  { method: "GET", path: "/api/status", scoped: true },
  { method: "GET", path: "/api/profiles", scoped: false },
  { method: "GET", path: "/api/sessions", scoped: true },
  { method: "GET", path: "/api/sessions/search", scoped: true },
  { method: "GET", path: "/api/sessions/stats", scoped: true },
  { method: "GET", path: "/api/sessions/{id}/messages", scoped: true },
  { method: "GET", path: "/api/sessions/{id}/mutation-snapshot", scoped: true },
  { method: "PATCH", path: "/api/sessions/{id}", scoped: true },
  { method: "DELETE", path: "/api/sessions/{id}", scoped: true },
  { method: "POST", path: "/api/sessions/prune", scoped: true },
  { method: "GET", path: "/api/config", scoped: true },
  { method: "PUT", path: "/api/config", scoped: true },
  { method: "GET", path: "/api/config/schema", scoped: true },
  { method: "GET", path: "/api/config/effective", scoped: true },
  { method: "GET", path: "/api/env", scoped: true },
  { method: "PUT", path: "/api/env", scoped: true },
  { method: "DELETE", path: "/api/env", scoped: true },
  { method: "GET", path: "/api/logs", scoped: true },
  { method: "GET", path: "/api/analytics/usage", scoped: true },
  { method: "GET", path: "/api/files", scoped: false },
  { method: "POST", path: "/api/files/upload", scoped: false },
  { method: "GET", path: "/api/cron/jobs", scoped: true },
  { method: "POST", path: "/api/cron/jobs/{id}/pause", scoped: true },
  { method: "GET", path: "/api/skills", scoped: true },
  { method: "POST", path: "/api/skills/toggle", scoped: true },
  { method: "GET", path: "/api/tools/toolsets", scoped: true },
  { method: "POST", path: "/api/skills/hub/install", scoped: true },
  { method: "GET", path: "/api/plugins", scoped: true },
  { method: "POST", path: "/api/plugins/install", scoped: true },
  { method: "GET", path: "/api/mcp/servers", scoped: true },
  { method: "POST", path: "/api/mcp/servers/{name}/test", scoped: true },
  { method: "POST", path: "/api/daemon/restart", scoped: true },
  { method: "GET", path: "/api/messaging/platforms", scoped: true },
  { method: "POST", path: "/api/providers/login/start", scoped: true },
  { method: "GET", path: "/api/model/info", scoped: true },
  { method: "POST", path: "/api/pairing/approve", scoped: true },
  { method: "GET", path: "/api/memory", scoped: true },
  { method: "POST", path: "/api/webhooks", scoped: true },
  { method: "POST", path: "/api/ops/doctor", scoped: true },
  { method: "DELETE", path: "/api/credentials/pool/{provider}/{index}", scoped: true },
  { method: "POST", path: "/api/auth/ws-ticket", scoped: false },
];
