// A typed, in-memory dashboard backend behind a `fetch` stand-in. Page tests install it instead of
// the network: every route the pages call is answered from `state`, every call is recorded, and
// one-off failures can be queued. Shapes come from `lib/restTypes.ts` (the generated REST schema
// once P18-11..16 land), so a page and its mock cannot drift apart silently.

import type {
  ConfigObject,
  ConfigSchema,
  CronJob,
  CronJobInput,
  DeliveryTarget,
  EffectiveEntry,
  EnvKeys,
  FileEntry,
  HubResult,
  LogFile,
  McpServer,
  McpServerCreate,
  McpTool,
  MutationSnapshot,
  PluginInfo,
  ProfilesResponse,
  SessionInfo,
  SessionMessage,
  SessionStats,
  SkillInfo,
  ToolsetInfo,
  UsageDay,
} from "../lib/restTypes.ts";

export interface MockState {
  profiles: ProfilesResponse;
  sessions: SessionInfo[];
  messages: Record<string, SessionMessage[]>;
  /** Session id → the 409 code a delete answers with. */
  deleteConflicts: Record<string, string>;
  snapshots: Record<string, MutationSnapshot>;
  /** How many sessions a prune of any age would remove. */
  pruneCount: number;
  config: ConfigObject;
  schema: ConfigSchema;
  effective: EffectiveEntry[];
  env: EnvKeys;
  /** What the secret store holds; never sent back by any route. */
  envValues: Record<string, string>;
  logs: Record<LogFile, string[]>;
  usage: UsageDay[];
  /** Directory path → entries. */
  files: Record<string, FileEntry[]>;
  fileText: Record<string, string>;
  lockedRoot: string | null;
  cronJobs: CronJob[];
  targets: DeliveryTarget[];
  skills: SkillInfo[];
  toolsets: ToolsetInfo[];
  hub: HubResult[];
  plugins: PluginInfo[];
  mcpServers: McpServer[];
  /** Server name → tools its test lists, or an error string. */
  mcpTools: Record<string, McpTool[] | string>;
}

export interface MockCall {
  method: string;
  /** The URL exactly as the client sent it (base path included). */
  url: string;
  /** Path from `/api/` on, without the query. */
  path: string;
  query: URLSearchParams;
  body: unknown;
  headers: Headers;
}

export interface MockReply {
  status: number;
  body?: unknown;
}

type Handler = (call: MockCall, match: RegExpMatchArray) => MockReply;

export interface MockApi {
  state: MockState;
  calls: MockCall[];
  fetch: typeof fetch;
  /** Replace `globalThis.fetch`; the returned function puts the old one back. */
  install(): () => void;
  callsTo(method: string, path: string | RegExp): MockCall[];
  /** Answer the next matching call with this reply instead of the route. */
  failNext(method: string, path: string | RegExp, reply: MockReply): void;
}

const ok = (body: unknown = { ok: true }): MockReply => ({ status: 200, body });
const fail = (status: number, code: string, message: string): MockReply => ({
  status,
  body: { detail: { code, message } },
});

// -- schedule grammar (the kept forms of P14-01, enough to validate what the page sends) ---------

const DURATION = /^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$/;
const CLOCK = /^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/;
const DAYS = /^(daily|day|everyday|weekdays?|weekends?|mon|tue|wed|thu|fri|sat|sun)$/;
const CRON_FIELD = /^[A-Za-z0-9*,\-/]+$/;

function isDuration(text: string): boolean {
  const match = DURATION.exec(text);
  return match !== null && Number(match[1]) > 0;
}

function isClock(text: string): boolean {
  const match = CLOCK.exec(text);
  if (!match) return false;
  const hour = Number(match[1]);
  const minute = match[2] === undefined ? 0 : Number(match[2]);
  if (minute > 59) return false;
  return match[3] ? hour >= 1 && hour <= 12 : hour <= 23;
}

/** True for the schedule strings the cron store accepts (subset used by the dashboard). */
export function isValidSchedule(raw: string): boolean {
  const text = raw.trim().toLowerCase();
  if (!text) return false;
  const fields = text.split(/\s+/);
  if (fields.length === 5 && fields.every((field) => CRON_FIELD.test(field))) return true;
  if (/^\d{4}-\d{2}-\d{2}/.test(text)) return !Number.isNaN(Date.parse(raw.trim()));
  const at = /^(?:every\s+)?(\S+)\s+(?:at\s+)?(.+)$/.exec(text);
  if (at?.[1] && at[2] && DAYS.test(at[1]) && isClock(at[2])) return true;
  if (text.startsWith("every ")) return isDuration(text.slice(6).trim());
  if (text.startsWith("in ")) return isDuration(text.slice(3).trim());
  return isDuration(text);
}

// -- defaults ----------------------------------------------------------------------------------

function session(id: string, source: string, title: string, extra: Partial<SessionInfo> = {}) {
  const base: SessionInfo = {
    id,
    source,
    model: "fake-model",
    title,
    started_at: 1_760_000_000,
    ended_at: 1_760_000_600,
    last_active: 1_760_000_600,
    is_active: false,
    message_count: 4,
    tool_call_count: 1,
    input_tokens: 1200,
    output_tokens: 300,
    preview: `${title} preview`,
  };
  return { ...base, ...extra };
}

export function defaultMockState(): MockState {
  return {
    profiles: { profiles: [{ name: "default", is_default: true }], active: "default" },
    sessions: [
      session("s-chat-1", "cli", "Refactor the parser"),
      session("s-chat-2", "web", "Plan the release"),
      session("s-cron-1", "cron", "Nightly digest"),
    ],
    messages: {
      "s-chat-1": [
        { role: "user", content: "Please refactor the parser." },
        {
          role: "assistant",
          content: "Reading the file first.",
          tool_calls: [{ id: "t1", name: "read_file", arguments: '{"path":"parser.py"}' }],
        },
        { role: "tool", content: "def parse(): ...", tool_name: "read_file", tool_call_id: "t1" },
        { role: "assistant", content: "## Done\n\nThe parser is split in two." },
      ],
    },
    deleteConflicts: {},
    snapshots: {},
    pruneCount: 3,
    config: { model: { provider: "fake" }, display: { interface: "tui", bell: false } },
    schema: {
      fields: {
        "model.provider": { type: "string", description: "Model provider", category: "model" },
        "display.interface": {
          type: "enum",
          description: "Interface",
          category: "display",
          options: ["cli", "tui"],
        },
        "display.bell": { type: "boolean", description: "Ring the bell", category: "display" },
      },
      category_order: ["model", "display"],
    },
    effective: [],
    env: {},
    envValues: {},
    logs: { agent: [], errors: [], gateway: [] },
    usage: [],
    files: {},
    fileText: {},
    lockedRoot: null,
    cronJobs: [],
    targets: [{ id: "local", label: "Save locally", kind: "local", available: true }],
    skills: [],
    toolsets: [],
    hub: [],
    plugins: [],
    mcpServers: [],
    mcpTools: {},
  };
}

// -- request parsing ---------------------------------------------------------------------------

function readBody(body: BodyInit | null | undefined): unknown {
  if (body === undefined || body === null) return undefined;
  if (typeof body === "string") {
    try {
      return JSON.parse(body) as unknown;
    } catch {
      return body;
    }
  }
  if (body instanceof FormData) {
    const out: Record<string, FormDataEntryValue> = {};
    body.forEach((value, key) => {
      out[key] = value;
    });
    return out;
  }
  return body;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function redact(value: string): string {
  return value.length <= 8 ? "****" : `${value.slice(0, 4)}…${value.slice(-2)}`;
}

function markHits(source: string, q: string): string | null {
  const at = source.toLowerCase().indexOf(q.toLowerCase());
  if (at < 0) return null;
  return `${source.slice(0, at)}<mark>${source.slice(at, at + q.length)}</mark>${source.slice(at + q.length)}`;
}

function jobFrom(input: CronJobInput, id: string): CronJob {
  return {
    id,
    name: input.name,
    prompt: input.prompt,
    schedule: input.schedule,
    schedule_display: input.schedule,
    deliver: input.deliver,
    state: "scheduled",
    enabled: true,
    last_run_at: null,
    next_run_at: "2026-10-21T09:00:00Z",
    last_status: null,
  };
}

function cronInput(body: unknown): CronJobInput {
  const record = asRecord(body);
  return {
    name: text(record.name),
    prompt: text(record.prompt),
    schedule: text(record.schedule),
    deliver: text(record.deliver) || "local",
  };
}

// -- routes ------------------------------------------------------------------------------------

function routes(state: MockState): Array<[string, RegExp, Handler]> {
  const findSession = (id: string) => state.sessions.find((s) => s.id === id);
  const findJob = (id: string) => state.cronJobs.find((j) => j.id === id);
  const findServer = (name: string) => state.mcpServers.find((s) => s.name === name);
  let nextJob = state.cronJobs.length + 1;

  const sessionStats = (): SessionStats => {
    const bySource: Record<string, number> = {};
    for (const s of state.sessions) {
      const key = s.source ?? "unknown";
      bySource[key] = (bySource[key] ?? 0) + 1;
    }
    return {
      total: state.sessions.length,
      active: state.sessions.filter((s) => s.is_active).length,
      archived: 0,
      messages: state.sessions.reduce((sum, s) => sum + s.message_count, 0),
      by_source: bySource,
    };
  };

  return [
    ["GET", /^\/api\/profiles$/, () => ok(state.profiles)],

    [
      "GET",
      /^\/api\/sessions$/,
      (call) => {
        const only = call.query.get("sources")?.split(",").filter(Boolean);
        const without = call.query.get("exclude_sources")?.split(",").filter(Boolean);
        const exact = call.query.get("source");
        const rows = state.sessions.filter((s) => {
          const source = s.source ?? "";
          if (exact) return source === exact;
          if (only && !only.includes(source)) return false;
          if (without?.includes(source)) return false;
          return true;
        });
        const limit = Number(call.query.get("limit") ?? 50);
        const offset = Number(call.query.get("offset") ?? 0);
        return ok({
          sessions: rows.slice(offset, offset + limit),
          total: rows.length,
          limit,
          offset,
        });
      },
    ],
    [
      "GET",
      /^\/api\/sessions\/search$/,
      (call) => {
        const q = call.query.get("q") ?? "";
        const results = state.sessions.flatMap((s) => {
          const snippet = markHits(s.preview ?? "", q) ?? markHits(s.title ?? "", q);
          if (!q || snippet === null) return [];
          return [
            {
              session_id: s.id,
              title: s.title,
              source: s.source,
              snippet,
              role: "user",
              last_active: s.last_active,
            },
          ];
        });
        return ok({ results });
      },
    ],
    ["GET", /^\/api\/sessions\/stats$/, () => ok(sessionStats())],
    [
      "POST",
      /^\/api\/sessions\/prune$/,
      (call) => {
        const body = asRecord(call.body);
        const days = body.older_than_days;
        if (typeof days !== "number" || days < 0) return fail(422, "invalid", "bad day count");
        if (body.dry_run === true)
          return ok({ dry_run: true, count: state.pruneCount, skipped_open: 0 });
        const count = state.pruneCount;
        state.pruneCount = 0;
        return ok({ dry_run: false, count, skipped_open: 0 });
      },
    ],
    [
      "GET",
      /^\/api\/sessions\/([^/]+)\/messages$/,
      (_call, m) => {
        const id = decodeURIComponent(m[1] ?? "");
        if (!findSession(id)) return fail(404, "not_found", "no such session");
        return ok({ session_id: id, messages: state.messages[id] ?? [] });
      },
    ],
    [
      "GET",
      /^\/api\/sessions\/([^/]+)\/mutation-snapshot$/,
      (_call, m) => {
        const id = decodeURIComponent(m[1] ?? "");
        const snap = state.snapshots[id] ?? {
          exists: findSession(id) !== undefined,
          revision: 7,
          generation: 2,
        };
        return ok(snap);
      },
    ],
    [
      "GET",
      /^\/api\/sessions\/([^/]+)\/export$/,
      (_call, m) => {
        const id = decodeURIComponent(m[1] ?? "");
        const found = findSession(id);
        if (!found) return fail(404, "not_found", "no such session");
        return ok({ session: found, messages: state.messages[id] ?? [] });
      },
    ],
    [
      "PATCH",
      /^\/api\/sessions\/([^/]+)$/,
      (call, m) => {
        const found = findSession(decodeURIComponent(m[1] ?? ""));
        const body = asRecord(call.body);
        if (!found) return fail(404, "not_found", "no such session");
        if (typeof body.request_id !== "string") {
          return fail(409, "client_upgrade_required", "identity missing");
        }
        found.title = text(body.title);
        return ok(found);
      },
    ],
    [
      "DELETE",
      /^\/api\/sessions\/([^/]+)$/,
      (call, m) => {
        const id = decodeURIComponent(m[1] ?? "");
        const conflict = state.deleteConflicts[id];
        if (conflict) return fail(409, conflict, `refused: ${conflict}`);
        const hasId = call.query.has("request_id");
        const hasRevision = call.query.has("expected_revision");
        if (!hasId && !hasRevision) return fail(409, "client_upgrade_required", "no identity");
        if (!hasId || !hasRevision) return fail(409, "mutation_identity_required", "partial");
        state.sessions = state.sessions.filter((s) => s.id !== id);
        return ok();
      },
    ],

    ["GET", /^\/api\/config$/, () => ok(state.config)],
    [
      "PUT",
      /^\/api\/config$/,
      (call) => {
        const config = asRecord(call.body).config;
        if (typeof config !== "object" || config === null) return fail(422, "invalid", "no config");
        state.config = config as ConfigObject;
        return ok();
      },
    ],
    ["GET", /^\/api\/config\/schema$/, () => ok(state.schema)],
    ["GET", /^\/api\/config\/effective$/, () => ok({ entries: state.effective })],

    ["GET", /^\/api\/env$/, () => ok(state.env)],
    [
      "PUT",
      /^\/api\/env$/,
      (call) => {
        const body = asRecord(call.body);
        const key = text(body.key);
        const value = text(body.value);
        if (!key || !value) return fail(422, "invalid", "key and value are required");
        state.envValues[key] = value;
        const known = state.env[key];
        const redacted = redact(value);
        state.env[key] = {
          is_set: true,
          redacted_value: redacted,
          description: known?.description ?? "",
          url: known?.url ?? null,
          category: known?.category ?? "custom",
          is_password: known?.is_password ?? true,
          advanced: known?.advanced ?? false,
        };
        return ok({ key, is_set: true, redacted_value: redacted });
      },
    ],
    [
      "DELETE",
      /^\/api\/env$/,
      (call) => {
        const key = text(asRecord(call.body).key);
        delete state.envValues[key];
        const known = state.env[key];
        if (known) state.env[key] = { ...known, is_set: false, redacted_value: null };
        return ok();
      },
    ],

    [
      "GET",
      /^\/api\/logs$/,
      (call) => {
        const file = (call.query.get("file") ?? "agent") as LogFile;
        const lines = Number(call.query.get("lines") ?? 100);
        const all = state.logs[file] ?? [];
        return ok({ file, lines: all.slice(-lines) });
      },
    ],
    [
      "GET",
      /^\/api\/analytics\/usage$/,
      (call) => {
        const days = Number(call.query.get("days") ?? 30);
        const daily = state.usage.slice(-days);
        const totals = daily.reduce(
          (sum, d) => ({
            input_tokens: sum.input_tokens + d.input_tokens,
            output_tokens: sum.output_tokens + d.output_tokens,
            cost: sum.cost + d.cost,
            sessions: sum.sessions + d.sessions,
          }),
          { input_tokens: 0, output_tokens: 0, cost: 0, sessions: 0 },
        );
        return ok({ days, daily, totals });
      },
    ],

    [
      "GET",
      /^\/api\/files$/,
      (call) => {
        const path = call.query.get("path") ?? state.lockedRoot ?? "/home/user";
        const entries = state.files[path];
        if (!entries) return fail(404, "not_found", "no such folder");
        const cut = path.lastIndexOf("/");
        const parent =
          path === state.lockedRoot || path === "/" ? null : cut <= 0 ? "/" : path.slice(0, cut);
        return ok({ path, parent, locked_root: state.lockedRoot, entries });
      },
    ],
    [
      "GET",
      /^\/api\/files\/read$/,
      (call) => {
        const path = call.query.get("path") ?? "";
        const content = state.fileText[path];
        if (content === undefined) return fail(404, "not_found", "no such file");
        return ok({
          path,
          name: path.slice(path.lastIndexOf("/") + 1),
          size: content.length,
          mime_type: "text/plain",
          text: content,
          truncated: false,
        });
      },
    ],
    [
      "POST",
      /^\/api\/files\/mkdir$/,
      (call) => {
        const path = text(asRecord(call.body).path);
        const cut = path.lastIndexOf("/");
        const dir = cut <= 0 ? "/" : path.slice(0, cut);
        const entry: FileEntry = {
          name: path.slice(cut + 1),
          path,
          is_directory: true,
          size: null,
          mtime: null,
        };
        state.files[dir] = [...(state.files[dir] ?? []), entry];
        state.files[path] = [];
        return ok();
      },
    ],
    ["POST", /^\/api\/files\/upload$/, () => ok()],
    [
      "DELETE",
      /^\/api\/files$/,
      (call) => {
        const path = call.query.get("path") ?? "";
        for (const [dir, entries] of Object.entries(state.files)) {
          state.files[dir] = entries.filter((e) => e.path !== path);
        }
        return ok();
      },
    ],

    ["GET", /^\/api\/cron\/jobs$/, () => ok({ jobs: state.cronJobs })],
    ["GET", /^\/api\/cron\/delivery-targets$/, () => ok({ targets: state.targets })],
    [
      "POST",
      /^\/api\/cron\/jobs$/,
      (call) => {
        const input = cronInput(call.body);
        if (!input.prompt) return fail(422, "invalid", "a prompt is required");
        if (!isValidSchedule(input.schedule)) {
          return fail(400, "invalid_schedule", `'${input.schedule}' is not a schedule`);
        }
        const job = jobFrom(input, `job-${nextJob++}`);
        state.cronJobs.push(job);
        return ok(job);
      },
    ],
    [
      "PUT",
      /^\/api\/cron\/jobs\/([^/]+)$/,
      (call, m) => {
        const job = findJob(decodeURIComponent(m[1] ?? ""));
        if (!job) return fail(404, "not_found", "no such job");
        const input = cronInput(call.body);
        if (!isValidSchedule(input.schedule)) {
          return fail(400, "invalid_schedule", `'${input.schedule}' is not a schedule`);
        }
        Object.assign(job, input, { schedule_display: input.schedule });
        return ok(job);
      },
    ],
    [
      "POST",
      /^\/api\/cron\/jobs\/([^/]+)\/(pause|resume|trigger)$/,
      (_call, m) => {
        const job = findJob(decodeURIComponent(m[1] ?? ""));
        if (!job) return fail(404, "not_found", "no such job");
        if (m[2] === "pause") Object.assign(job, { state: "paused", enabled: false });
        if (m[2] === "resume") Object.assign(job, { state: "scheduled", enabled: true });
        if (m[2] === "trigger") job.last_status = "queued";
        return ok(job);
      },
    ],
    [
      "DELETE",
      /^\/api\/cron\/jobs\/([^/]+)$/,
      (_call, m) => {
        const id = decodeURIComponent(m[1] ?? "");
        state.cronJobs = state.cronJobs.filter((j) => j.id !== id);
        return ok();
      },
    ],

    ["GET", /^\/api\/skills$/, () => ok({ skills: state.skills })],
    [
      "POST",
      /^\/api\/skills\/toggle$/,
      (call) => {
        const body = asRecord(call.body);
        const skill = state.skills.find((s) => s.name === body.name);
        if (!skill) return fail(404, "not_found", "no such skill");
        skill.enabled = body.enabled === true;
        return ok();
      },
    ],
    ["GET", /^\/api\/tools\/toolsets$/, () => ok({ toolsets: state.toolsets })],
    [
      "GET",
      /^\/api\/skills\/hub\/search$/,
      (call) => {
        const q = (call.query.get("q") ?? "").toLowerCase();
        return ok({ results: state.hub.filter((h) => h.name.toLowerCase().includes(q)) });
      },
    ],
    [
      "POST",
      /^\/api\/skills\/hub\/install$/,
      (call) => {
        const id = asRecord(call.body).identifier;
        const hit = state.hub.find((h) => h.identifier === id);
        if (!hit) return fail(404, "not_found", "no such skill");
        if (hit.verdict === "dangerous") {
          return {
            status: 409,
            body: {
              detail: {
                code: "scan_blocked",
                message: "The scanner rated this skill dangerous.",
                verdict: "dangerous",
              },
            },
          };
        }
        hit.installed = true;
        return ok();
      },
    ],
    ["POST", /^\/api\/skills\/hub\/(update|uninstall)$/, () => ok()],

    ["GET", /^\/api\/plugins$/, () => ok({ plugins: state.plugins })],
    [
      "POST",
      /^\/api\/plugins\/install$/,
      (call) => {
        const body = asRecord(call.body);
        if (!/^[0-9a-f]{40}$/.test(text(body.sha))) {
          return fail(422, "invalid_sha", "a full 40-character commit SHA is required");
        }
        return ok();
      },
    ],
    [
      "POST",
      /^\/api\/plugins\/([^/]+)\/(enable|disable)$/,
      (_call, m) => {
        const plugin = state.plugins.find((p) => p.name === decodeURIComponent(m[1] ?? ""));
        if (!plugin) return fail(404, "not_found", "no such plugin");
        plugin.enabled = m[2] === "enable";
        return ok();
      },
    ],

    ["GET", /^\/api\/mcp\/servers$/, () => ok({ servers: state.mcpServers })],
    [
      "POST",
      /^\/api\/mcp\/servers$/,
      (call) => {
        const body = asRecord(call.body) as Partial<McpServerCreate>;
        if (!body.name) return fail(422, "invalid", "a name is required");
        if (!body.url === !body.command) return fail(422, "invalid", "give a URL or a command");
        const env: Record<string, string> = {};
        for (const key of Object.keys(body.env ?? {})) env[key] = "****";
        state.mcpServers.push({
          name: body.name,
          transport: body.url ? "http" : "stdio",
          url: body.url ?? null,
          command: body.command ?? null,
          args: body.args ?? [],
          env,
          auth: body.auth ?? "none",
          enabled: true,
        });
        return ok();
      },
    ],
    [
      "POST",
      /^\/api\/mcp\/servers\/([^/]+)\/test$/,
      (_call, m) => {
        const name = decodeURIComponent(m[1] ?? "");
        if (!findServer(name)) return fail(404, "not_found", "no such server");
        const tools = state.mcpTools[name] ?? [];
        if (typeof tools === "string") return ok({ ok: false, error: tools, tools: [] });
        return ok({ ok: true, tools });
      },
    ],
    [
      "PUT",
      /^\/api\/mcp\/servers\/([^/]+)\/enabled$/,
      (call, m) => {
        const server = findServer(decodeURIComponent(m[1] ?? ""));
        if (!server) return fail(404, "not_found", "no such server");
        server.enabled = asRecord(call.body).enabled === true;
        return ok();
      },
    ],
    [
      "DELETE",
      /^\/api\/mcp\/servers\/([^/]+)$/,
      (_call, m) => {
        const name = decodeURIComponent(m[1] ?? "");
        state.mcpServers = state.mcpServers.filter((s) => s.name !== name);
        return ok();
      },
    ],
  ];
}

function matches(pattern: string | RegExp, path: string): boolean {
  return typeof pattern === "string" ? pattern === path : pattern.test(path);
}

export function createMockApi(overrides: Partial<MockState> = {}): MockApi {
  const state: MockState = { ...defaultMockState(), ...overrides };
  const calls: MockCall[] = [];
  const queued: Array<{ method: string; path: string | RegExp; reply: MockReply }> = [];
  const table = routes(state);

  const fetchStandIn = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const method = (init?.method ?? "GET").toUpperCase();
    const local = url.replace(/^[a-z]+:\/\/[^/]+/i, "");
    const apiAt = local.indexOf("/api/");
    const fromApi = apiAt < 0 ? local : local.slice(apiAt);
    const queryAt = fromApi.indexOf("?");
    const path = queryAt < 0 ? fromApi : fromApi.slice(0, queryAt);
    const query = new URLSearchParams(queryAt < 0 ? "" : fromApi.slice(queryAt + 1));
    const call: MockCall = {
      method,
      url,
      path,
      query,
      body: readBody(init?.body),
      headers: new Headers(init?.headers),
    };
    calls.push(call);

    let reply: MockReply | undefined;
    const pending = queued.findIndex((q) => q.method === method && matches(q.path, path));
    if (pending >= 0) {
      reply = queued[pending]?.reply;
      queued.splice(pending, 1);
    }
    if (!reply) {
      for (const [routeMethod, pattern, handler] of table) {
        if (routeMethod !== method) continue;
        const match = path.match(pattern);
        if (match) {
          reply = handler(call, match);
          break;
        }
      }
    }
    reply ??= { status: 404, body: { detail: `no mock route for ${method} ${path}` } };
    const bodyText = reply.body === undefined ? null : JSON.stringify(reply.body);
    return new Response(reply.status === 204 ? null : bodyText, {
      status: reply.status,
      headers: { "Content-Type": "application/json" },
    });
  };

  return {
    state,
    calls,
    fetch: fetchStandIn as typeof fetch,
    install() {
      const previous = globalThis.fetch;
      globalThis.fetch = fetchStandIn as typeof fetch;
      return () => {
        globalThis.fetch = previous;
      };
    },
    callsTo(method, path) {
      return calls.filter((c) => c.method === method && matches(path, c.path));
    },
    failNext(method, path, reply) {
      queued.push({ method, path, reply });
    },
  };
}
