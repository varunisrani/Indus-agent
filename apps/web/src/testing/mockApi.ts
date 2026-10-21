// A typed, in-memory dashboard backend behind a `fetch` stand-in. Page tests install it instead of
// the network: every route the pages call is answered from `state`, every call is recorded, and
// one-off failures can be queued. Shapes come from `lib/restTypes.ts` (the generated REST schema
// once P18-11..16 land), so a page and its mock cannot drift apart silently.

import type {
  ActionStatus,
  AuxiliaryResponse,
  CheckpointsResponse,
  ConfigObject,
  ConfigSchema,
  CredentialPoolResponse,
  CronJob,
  CronJobInput,
  DaemonInfo,
  DeliveryTarget,
  EffectiveEntry,
  EnvKeys,
  FileEntry,
  HostStats,
  HubResult,
  LogFile,
  LoginStarted,
  LoginStatus,
  McpServer,
  McpServerCreate,
  McpTool,
  MemoryInfo,
  MessagingPlatform,
  ModelInfo,
  ModelOptions,
  MutationSnapshot,
  PairingResponse,
  PlatformTestResult,
  PluginInfo,
  ProfilesResponse,
  SessionInfo,
  SessionMessage,
  SessionStats,
  ShellHook,
  SkillInfo,
  StatusResponse,
  ToolsetInfo,
  UpdateInfo,
  UsageDay,
  WebhookRoute,
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
  /** Profile name → PERSONA.md text. */
  personas: Record<string, string>;
  status: StatusResponse;
  daemon: DaemonInfo;
  /**
   * Action name → the answers its status route gives, one per poll (the last repeats). Actions
   * with no script answer one finished run.
   */
  actionScripts: Record<string, ActionStatus[]>;
  /** Every action a POST started, in order. */
  actionsStarted: string[];
  platforms: MessagingPlatform[];
  /** What the secret store and `.env` hold for platform fields; never answered as such. */
  platformValues: Record<string, string>;
  platformTests: Record<string, PlatformTestResult>;
  pairing: PairingResponse;
  /** Pairing code → the person it pairs. */
  pairingCodes: Record<string, { platform: string; user_id: string; user_name: string | null }>;
  webhookReceiver: boolean;
  webhooks: WebhookRoute[];
  modelInfo: ModelInfo;
  modelOptions: ModelOptions;
  auxiliary: AuxiliaryResponse;
  /** The device-code grant a sign-in start answers; null → 400 `no_device_flow`. */
  deviceGrant: Omit<LoginStarted, "session_id" | "provider" | "status" | "message"> | null;
  /** Statuses successive polls of a sign-in answer (the last repeats). */
  loginPolls: LoginStatus[];
  hostStats: HostStats;
  update: UpdateInfo;
  memory: MemoryInfo;
  pool: CredentialPoolResponse;
  checkpoints: CheckpointsResponse;
  hooks: ShellHook[];
  hookEvents: string[];
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
    personas: {},
    status: {
      version: "0.1.0",
      daemon: { state: "running", heartbeat_age_s: 4 },
      overall: "ok",
      platforms: [],
      active_sessions: 0,
      recent_sessions: 0,
      auth_required: false,
      auth_providers: [],
      memory: { pressure: "ok", available_percent: 60 },
      disk: { pressure: "ok", used_percent: 40, free_gb: 120 },
    },
    daemon: { running: true, state: "running", pid: 4242, profile: "default", draining: false },
    actionScripts: {},
    actionsStarted: [],
    platforms: [],
    platformValues: {},
    platformTests: {},
    pairing: { pending: [], approved: [] },
    pairingCodes: {},
    webhookReceiver: true,
    webhooks: [],
    modelInfo: {
      model: "fake-model",
      provider: "fake",
      config_context_length: null,
      family_context_length: 128_000,
      effective_context_length: 128_000,
    },
    modelOptions: {
      providers: [
        {
          name: "fake",
          label: "Fake provider",
          description: "Answers from a script",
          family: "openai",
          auth_type: "api_key",
          configured: true,
          device_login: false,
          supports_model_listing: false,
        },
      ],
      current: { provider: "fake", model: "fake-model" },
    },
    auxiliary: { tasks: [], main: { provider: "fake", model: "fake-model" } },
    deviceGrant: null,
    loginPolls: ["pending"],
    hostStats: {
      cpu_count: 4,
      load: [0.5, 0.4, 0.3],
      memory: { total: 8 * 1024 ** 3, available: 4 * 1024 ** 3 },
      disk: { total: 100 * 1024 ** 3, free: 60 * 1024 ** 3, used: 40 * 1024 ** 3 },
    },
    update: {
      install_method: "git",
      current_version: "0.1.0",
      can_apply: true,
      behind: 0,
      update_available: false,
      message: "Up to date.",
      checked_at: 1_760_000_000,
      cached: false,
    },
    memory: { active: "", providers: [], builtin_files: { "MEMORY.md": 0, "USER.md": 0 } },
    pool: { providers: [] },
    checkpoints: { projects: [], total_bytes: 0 },
    hooks: [],
    hookEvents: ["pre_tool_call", "post_tool_call", "on_session_start"],
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
    [
      "POST",
      /^\/api\/files\/download-ticket$/,
      (call) => ok({ ticket: `ticket-for-${text(asRecord(call.body).path)}`, expires_in: 30 }),
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
    ...adminRoutes(state),
  ];
}

// -- admin routes (P18-23..25): channels, webhooks, pairing, daemon, profiles, models, system ----

const PROFILE_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;

function finishedRun(name: string): ActionStatus {
  return {
    name,
    state: "done",
    running: false,
    exit_code: 0,
    lines: [`=== ${name} started ===`, `=== ${name} done (0) ===`],
  };
}

function adminRoutes(state: MockState): Array<[string, RegExp, Handler]> {
  const polls: Record<string, number> = {};
  const logins: Record<string, number> = {};
  let nextLogin = 1;
  let nextPool = 1;

  const started = (name: string, extra: Record<string, unknown> = {}): MockReply => {
    state.actionsStarted.push(name);
    polls[name] = 0;
    return { status: 202, body: { ok: true, action: name, profile: "default", ...extra } };
  };
  const findPlatform = (id: string) => state.platforms.find((p) => p.id === id);
  const findProfile = (name: string) => state.profiles.profiles.find((p) => p.name === name);
  const findHook = (body: Record<string, unknown>) =>
    state.hooks.findIndex((h) => h.event === body.event && h.command === body.command);
  const platformState = (p: MessagingPlatform): string => {
    if (p.explicitly_disabled) return "disabled";
    if (!p.enabled) return "not_configured";
    return state.daemon.running ? "pending_restart" : "daemon_stopped";
  };

  return [
    ["GET", /^\/api\/status$/, () => ok(state.status)],
    ["GET", /^\/api\/daemon$/, () => ok(state.daemon)],
    ["POST", /^\/api\/daemon\/(start|stop|restart)$/, (_call, m) => started(`daemon-${m[1]}`)],
    [
      "GET",
      /^\/api\/actions\/([^/]+)\/status$/,
      (_call, m) => {
        const name = decodeURIComponent(m[1] ?? "");
        const script = state.actionScripts[name];
        if (!script?.length) return ok(finishedRun(name));
        const at = polls[name] ?? 0;
        polls[name] = at + 1;
        return ok(script[Math.min(at, script.length - 1)]);
      },
    ],

    [
      "GET",
      /^\/api\/messaging\/platforms$/,
      () => ok({ platforms: state.platforms, daemon_running: state.daemon.running }),
    ],
    [
      "PUT",
      /^\/api\/messaging\/platforms\/([^/]+)$/,
      (call, m) => {
        const platform = findPlatform(decodeURIComponent(m[1] ?? ""));
        if (!platform) return fail(404, "unknown_platform", "No such messaging platform.");
        const body = asRecord(call.body);
        const env = asRecord(body.env) as Record<string, unknown>;
        const clear = Array.isArray(body.clear_env) ? body.clear_env.map(String) : [];
        for (const key of [...clear, ...Object.keys(env)]) {
          if (!platform.fields.some((f) => f.key === key)) {
            return fail(400, "unknown_field", `${key} is not a setting of ${platform.name}.`);
          }
        }
        for (const [key, raw] of Object.entries(env)) {
          const field = platform.fields.find((f) => f.key === key);
          if (field?.is_secret && field.preview !== null && raw === field.preview) {
            return fail(400, "redacted_value", `${key} was sent as its masked preview.`);
          }
        }
        for (const key of clear) {
          delete state.platformValues[`${platform.id}.${key}`];
          const field = platform.fields.find((f) => f.key === key);
          if (field) Object.assign(field, { is_set: false, preview: null, value: null });
        }
        for (const [key, raw] of Object.entries(env)) {
          const value = text(raw).trim();
          if (!value) continue;
          state.platformValues[`${platform.id}.${key}`] = value;
          const field = platform.fields.find((f) => f.key === key);
          if (!field) continue;
          field.is_set = true;
          if (field.is_secret) field.preview = redact(value);
          else field.value = value;
        }
        if (typeof body.enabled === "boolean") {
          platform.enabled = body.enabled;
          platform.explicitly_disabled = !body.enabled;
        }
        platform.state = platformState(platform);
        return ok({ ok: true, platform: platform.id, restart_needed: true });
      },
    ],
    [
      "POST",
      /^\/api\/messaging\/platforms\/([^/]+)\/test$/,
      (_call, m) => {
        const id = decodeURIComponent(m[1] ?? "");
        const platform = findPlatform(id);
        if (!platform) return fail(404, "unknown_platform", "No such messaging platform.");
        return ok(
          state.platformTests[id] ?? {
            ok: platform.state === "connected",
            state: platform.state,
            message: `${platform.name}: ${platform.state}`,
          },
        );
      },
    ],

    ["GET", /^\/api\/pairing$/, () => ok(state.pairing)],
    [
      "POST",
      /^\/api\/pairing\/approve$/,
      (call) => {
        const body = asRecord(call.body);
        const platform = text(body.platform);
        if (!platform) return fail(422, "invalid", "platform is required");
        const requestId = text(body.request_id);
        const code = text(body.code);
        if (code === "LOCKED") return fail(429, "locked_out", "Too many wrong codes.");
        let user: { user_id: string; user_name: string | null } | undefined;
        if (requestId) {
          const at = state.pairing.pending.findIndex(
            (p) => p.platform === platform && p.request_id === requestId,
          );
          const found = state.pairing.pending[at];
          if (found) {
            state.pairing.pending.splice(at, 1);
            user = { user_id: found.user_id, user_name: found.user_name };
          }
        } else if (code) {
          const found = state.pairingCodes[code];
          if (found?.platform === platform) {
            delete state.pairingCodes[code];
            user = { user_id: found.user_id, user_name: found.user_name };
          }
        } else {
          return fail(400, "code_required", "Give the pairing code (or the request id).");
        }
        if (!user)
          return fail(404, "no_such_code", "That code or request is unknown or has expired.");
        state.pairing.approved.push({ platform, ...user, approved_at: 1_760_000_000 });
        return ok({ ok: true, user });
      },
    ],
    [
      "POST",
      /^\/api\/pairing\/revoke$/,
      (call) => {
        const body = asRecord(call.body);
        const before = state.pairing.approved.length;
        state.pairing.approved = state.pairing.approved.filter(
          (a) => !(a.platform === body.platform && a.user_id === body.user_id),
        );
        if (state.pairing.approved.length === before) {
          return fail(404, "not_approved", "That user is not approved on this platform.");
        }
        return ok();
      },
    ],
    [
      "POST",
      /^\/api\/pairing\/clear-pending$/,
      () => {
        const cleared = state.pairing.pending.length;
        state.pairing.pending = [];
        return ok({ ok: true, cleared });
      },
    ],

    [
      "GET",
      /^\/api\/webhooks$/,
      () =>
        ok({
          receiver_enabled: state.webhookReceiver,
          base_url: "http://127.0.0.1:8644",
          routes: state.webhooks,
        }),
    ],
    [
      "POST",
      /^\/api\/webhooks$/,
      (call) => {
        const body = asRecord(call.body);
        const name = text(body.name).trim().toLowerCase();
        if (!/^[a-z0-9][a-z0-9_-]*$/.test(name)) {
          return fail(400, "bad_name", "Use lower-case letters, digits, '-' and '_' for the name.");
        }
        if (state.webhooks.some((w) => w.name === name)) {
          return fail(409, "route_exists", "A route with that name already exists.");
        }
        const route: WebhookRoute = {
          name,
          source: "subscribed",
          url: `http://127.0.0.1:8644/webhooks/${name}`,
          auth: text(body.auth) || "auto",
          events: Array.isArray(body.events) ? body.events.map(String) : [],
          prompt: text(body.prompt),
          deliver: text(body.deliver) || null,
          deliver_only: body.deliver_only === true,
          description: text(body.description) || `Added from the dashboard: ${name}`,
          created_at: "2026-10-20T12:00:00+00:00",
          enabled: true,
          secret_set: true,
        };
        state.webhooks.push(route);
        // Built at runtime: never a credential-shaped literal in the source.
        const secret = ["whsec", "mock", name, "0123456789"].join("-");
        return { status: 201, body: { ...route, secret } };
      },
    ],
    [
      "PUT",
      /^\/api\/webhooks\/receiver$/,
      (call) => {
        state.webhookReceiver = asRecord(call.body).enabled === true;
        return ok({ ok: true, receiver_enabled: state.webhookReceiver, restart_needed: true });
      },
    ],
    [
      "PUT",
      /^\/api\/webhooks\/([^/]+)\/enabled$/,
      (call, m) => {
        const route = state.webhooks.find((w) => w.name === decodeURIComponent(m[1] ?? ""));
        if (!route) return fail(404, "no_such_route", "No such webhook route.");
        if (route.source === "config") return fail(409, "config_route", "Set in the config file.");
        route.enabled = asRecord(call.body).enabled === true;
        return ok({ ok: true, name: route.name, enabled: route.enabled });
      },
    ],
    [
      "DELETE",
      /^\/api\/webhooks\/([^/]+)$/,
      (_call, m) => {
        const name = decodeURIComponent(m[1] ?? "");
        const route = state.webhooks.find((w) => w.name === name);
        if (!route) return fail(404, "no_such_route", "No such webhook route.");
        if (route.source === "config") return fail(409, "config_route", "Set in the config file.");
        state.webhooks = state.webhooks.filter((w) => w.name !== name);
        return ok({ ok: true, name });
      },
    ],

    [
      "POST",
      /^\/api\/profiles$/,
      (call) => {
        const body = asRecord(call.body);
        const name = text(body.name).trim().toLowerCase();
        if (!PROFILE_ID.test(name) || name === "default") {
          return fail(400, "bad_name", "Use 1-64 lowercase letters, digits, '-' or '_'.");
        }
        if (findProfile(name))
          return fail(409, "profile_exists", "A profile with that name exists.");
        state.profiles.profiles.push({
          name,
          is_default: false,
          is_sticky: false,
          is_current: false,
          model: null,
          provider: null,
          description: text(body.description) || null,
          has_persona: false,
        });
        return { status: 201, body: { ok: true, name, message: `created ${name}` } };
      },
    ],
    [
      "POST",
      /^\/api\/profiles\/active$/,
      (call) => {
        const name = text(asRecord(call.body).name);
        if (!findProfile(name)) return fail(404, "profile_not_found", "No such profile.");
        state.profiles.active = name;
        for (const p of state.profiles.profiles) p.is_sticky = p.name === name;
        return ok({ ok: true, active: name });
      },
    ],
    [
      "POST",
      /^\/api\/profiles\/import$/,
      (call) => {
        const body = asRecord(call.body);
        const name = text(body.name) || "imported";
        state.profiles.profiles.push({ name, is_default: false });
        return { status: 201, body: { ok: true, name, path: `/profiles/${name}` } };
      },
    ],
    [
      "PATCH",
      /^\/api\/profiles\/([^/]+)$/,
      (call, m) => {
        const profile = findProfile(decodeURIComponent(m[1] ?? ""));
        if (!profile) return fail(404, "profile_not_found", "No such profile.");
        if (profile.is_default)
          return fail(400, "default_profile", "The default cannot be renamed.");
        const next = text(asRecord(call.body).new_name).trim().toLowerCase();
        if (!PROFILE_ID.test(next)) return fail(400, "bad_name", "Not a valid profile name.");
        profile.name = next;
        return ok({ ok: true, name: next, message: "" });
      },
    ],
    [
      "DELETE",
      /^\/api\/profiles\/([^/]+)$/,
      (_call, m) => {
        const name = decodeURIComponent(m[1] ?? "");
        const profile = findProfile(name);
        if (!profile) return fail(404, "profile_not_found", "No such profile.");
        if (profile.is_default) return fail(400, "default_profile", "The default cannot go.");
        state.profiles.profiles = state.profiles.profiles.filter((p) => p.name !== name);
        return ok({ ok: true, name, message: `deleted ${name}` });
      },
    ],
    [
      "GET",
      /^\/api\/profiles\/([^/]+)\/persona$/,
      (_call, m) => {
        const name = decodeURIComponent(m[1] ?? "");
        if (!findProfile(name)) return fail(404, "profile_not_found", "No such profile.");
        const content = state.personas[name];
        return ok({ content: content ?? "", exists: content !== undefined });
      },
    ],
    [
      "PUT",
      /^\/api\/profiles\/([^/]+)\/(persona|description|model)$/,
      (call, m) => {
        const profile = findProfile(decodeURIComponent(m[1] ?? ""));
        if (!profile) return fail(404, "profile_not_found", "No such profile.");
        const body = asRecord(call.body);
        if (m[2] === "persona") {
          state.personas[profile.name] = text(body.content);
          profile.has_persona = true;
        } else if (m[2] === "description") {
          profile.description = text(body.description).trim() || null;
        } else {
          if (!text(body.provider) || !text(body.model)) return fail(422, "invalid", "both needed");
          profile.provider = text(body.provider);
          profile.model = text(body.model);
        }
        return ok();
      },
    ],
    [
      "POST",
      /^\/api\/profiles\/([^/]+)\/export$/,
      (_call, m) => {
        const name = decodeURIComponent(m[1] ?? "");
        if (!findProfile(name)) return fail(404, "profile_not_found", "No such profile.");
        return started("profile-export", {
          profile: name,
          archive: `/profiles/${name}/backups/profile-${name}.zip`,
        });
      },
    ],

    ["GET", /^\/api\/model\/info$/, () => ok(state.modelInfo)],
    ["GET", /^\/api\/model\/options$/, () => ok(state.modelOptions)],
    ["GET", /^\/api\/model\/auxiliary$/, () => ok(state.auxiliary)],
    [
      "POST",
      /^\/api\/model\/set$/,
      (call) => {
        const body = asRecord(call.body);
        const provider = text(body.provider);
        const model = text(body.model);
        if (body.scope === "main") {
          if (!model) return fail(400, "model_required", "Name the model.");
          state.modelInfo.model = model;
          if (provider) state.modelInfo.provider = provider;
          state.modelOptions.current = { provider: provider || null, model };
        } else if (body.scope === "auxiliary") {
          const task = state.auxiliary.tasks.find((t) => t.task === body.task);
          if (!task) return fail(400, "unknown_task", "Unknown side task.");
          task.provider = provider || "auto";
          task.model = model;
        } else {
          return fail(400, "bad_scope", "scope must be main or auxiliary.");
        }
        return ok({
          ok: true,
          scope: body.scope,
          provider: provider || null,
          model: model || null,
        });
      },
    ],
    [
      "POST",
      /^\/api\/providers\/login\/start$/,
      (call) => {
        const provider = text(asRecord(call.body).provider);
        if (!state.deviceGrant) {
          return fail(400, "no_device_flow", "This provider documents no device sign-in.");
        }
        const id = `login-${nextLogin++}`;
        logins[id] = 0;
        return ok({
          session_id: id,
          provider,
          status: "pending",
          message: "",
          ...state.deviceGrant,
        });
      },
    ],
    [
      "GET",
      /^\/api\/providers\/login\/([^/]+)$/,
      (_call, m) => {
        const id = decodeURIComponent(m[1] ?? "");
        const at = logins[id];
        if (at === undefined) return fail(404, "no_such_session", "No such sign-in session.");
        logins[id] = at + 1;
        const status = state.loginPolls[Math.min(at, state.loginPolls.length - 1)] ?? "pending";
        const message = status === "approved" ? "Signed in; the credential was added." : "";
        return ok({ session_id: id, provider: "fake", status, message });
      },
    ],
    [
      "DELETE",
      /^\/api\/providers\/login\/([^/]+)$/,
      (_call, m) => {
        const id = decodeURIComponent(m[1] ?? "");
        if (logins[id] === undefined) return fail(404, "no_such_session", "No such session.");
        return ok({ session_id: id, provider: "fake", status: "cancelled", message: "Cancelled." });
      },
    ],

    ["GET", /^\/api\/system\/stats$/, () => ok(state.hostStats)],
    ["GET", /^\/api\/system\/update$/, () => ok(state.update)],
    [
      "POST",
      /^\/api\/system\/update$/,
      () =>
        state.update.can_apply
          ? started("update")
          : fail(409, "update_not_in_place", "This install is not a git checkout."),
    ],
    ["GET", /^\/api\/memory$/, () => ok(state.memory)],
    [
      "PUT",
      /^\/api\/memory\/provider$/,
      (call) => {
        const name = text(asRecord(call.body).provider).trim().toLowerCase();
        if (name && !state.memory.providers.includes(name)) {
          return fail(400, "unknown_memory_provider", "That memory provider is not installed.");
        }
        state.memory.active = name;
        return ok({ ok: true, active: name });
      },
    ],
    ["GET", /^\/api\/credentials\/pool$/, () => ok(state.pool)],
    [
      "POST",
      /^\/api\/credentials\/pool$/,
      (call) => {
        const body = asRecord(call.body);
        const provider = text(body.provider);
        const key = text(body.api_key).trim();
        if (!key || key === "changeme") {
          return fail(400, "placeholder_key", "That does not look like a real key.");
        }
        let group = state.pool.providers.find((p) => p.provider === provider);
        if (!group) {
          group = { provider, label: provider, inherited: false, entries: [] };
          state.pool.providers.push(group);
        }
        const index = group.entries.length + 1;
        const id = `entry-${nextPool++}`;
        group.entries.push({
          index,
          id,
          label: text(body.label) || `api-key-${index}`,
          status: "ok",
          auth_type: "api_key",
          source: "manual",
          env_var: null,
          expires_at: null,
          priority: 0,
          request_count: 0,
          preview: redact(key),
        });
        return { status: 201, body: { ok: true, provider, index, id } };
      },
    ],
    [
      "DELETE",
      /^\/api\/credentials\/pool\/([^/]+)\/(\d+)$/,
      (_call, m) => {
        const group = state.pool.providers.find(
          (p) => p.provider === decodeURIComponent(m[1] ?? ""),
        );
        const index = Number(m[2]);
        const entry = group?.entries[index - 1];
        if (!group || !entry) return fail(404, "no_such_entry", "There is no credential there.");
        group.entries.splice(index - 1, 1);
        group.entries.forEach((e, i) => {
          e.index = i + 1;
        });
        if (!group.entries.length) {
          state.pool.providers = state.pool.providers.filter((p) => p !== group);
        }
        return ok({ ok: true, provider: group.provider, removed: entry.label, env_var_kept: null });
      },
    ],
    ["POST", /^\/api\/ops\/doctor$/, () => started("doctor")],
    [
      "POST",
      /^\/api\/ops\/backup$/,
      () => started("backup", { archive: "/home/user/.indus/backups/indus-backup-1.zip" }),
    ],
    [
      "POST",
      /^\/api\/ops\/(import|import-upload)$/,
      (call) => started("import", { archive: text(asRecord(call.body).archive) || "upload.zip" }),
    ],
    ["GET", /^\/api\/ops\/checkpoints$/, () => ok(state.checkpoints)],
    ["POST", /^\/api\/ops\/checkpoints\/prune$/, () => started("checkpoints-prune")],
    [
      "GET",
      /^\/api\/ops\/hooks$/,
      () => ok({ hooks: state.hooks, events: state.hookEvents, warnings: [] }),
    ],
    [
      "POST",
      /^\/api\/ops\/hooks$/,
      (call) => {
        const body = asRecord(call.body);
        if (body.consent !== true) {
          return fail(400, "consent_required", "A shell hook runs your command; confirm it.");
        }
        if (!state.hookEvents.includes(text(body.event))) {
          return fail(400, "unknown_event", "Unknown event.");
        }
        if (findHook(body) >= 0) return fail(409, "hook_exists", "That hook exists.");
        state.hooks.push({
          event: text(body.event),
          command: text(body.command),
          matcher: text(body.matcher) || null,
          timeout: typeof body.timeout === "number" ? body.timeout : null,
          approved: true,
          grant_id: `grant-${state.hooks.length + 1}`,
          executable: true,
        });
        return { status: 201, body: { ok: true, approved: true } };
      },
    ],
    [
      "DELETE",
      /^\/api\/ops\/hooks$/,
      (call) => {
        const at = findHook(asRecord(call.body));
        if (at < 0) return fail(404, "no_such_hook", "No such hook.");
        state.hooks.splice(at, 1);
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
