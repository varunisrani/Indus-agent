// REST shapes the pages read and write. Hand-written for the prework (P18-17..22); P18-11..16
// generate the real ones from the Pydantic models in `indus.protocol` into `@indus/protocol`, and
// this file then re-exports those names.

export interface ProfileSummary {
  name: string;
  is_default: boolean;
  /** The sticky profile new CLI runs pick up (`indus profile use`). */
  is_sticky?: boolean;
  /** The profile this dashboard backend itself serves. */
  is_current?: boolean;
  model?: string | null;
  provider?: string | null;
  description?: string | null;
  has_persona?: boolean;
  /** Skills this profile has turned on (null when its settings or skills cannot be read). */
  skill_count?: number | null;
}

export interface ProfilesResponse {
  profiles: ProfileSummary[];
  /** The sticky profile. */
  active: string;
  /** The profile this backend serves. */
  current?: string;
}

export interface ProfileCreate {
  name: string;
  clone_from?: string;
  clone_all?: boolean;
  description?: string;
  no_skills?: boolean;
  alias?: boolean;
}

/** Create / rename / delete run `indus profile …`; `message` is its (masked) output. */
export interface ProfileCommandResponse {
  ok: boolean;
  name: string;
  message: string;
}

export interface PersonaResponse {
  content: string;
  exists: boolean;
}

export interface ProfileExportResponse {
  ok: boolean;
  action: string;
  profile: string;
  archive: string;
}

// -- background actions (doctor, backup, daemon start/stop/restart, update, export) ------------

export interface ActionStarted {
  ok: boolean;
  action: string;
  profile: string;
  /** Backup / import: the archive the run writes or reads. */
  archive?: string;
}

export type ActionState = "idle" | "unknown" | "running" | "done" | "failed";

export interface ActionStatus {
  name: string;
  state: ActionState;
  running: boolean;
  exit_code: number | null;
  started_at?: number;
  /** The last lines of the run's log, secrets masked by the server. */
  lines: string[];
}

// -- daemon -------------------------------------------------------------------------------------

/** `GET /api/daemon`: what the control socket says (a stopped daemon has only the first three). */
export interface DaemonInfo {
  running: boolean;
  state: string;
  pid: number | null;
  profile?: string | null;
  code_version?: string | null;
  draining?: boolean;
}

// -- messaging platforms and pairing ----------------------------------------------------------

export type PlatformFieldKind = "credential" | "setting" | "policy";

export interface PlatformField {
  key: string;
  kind: PlatformFieldKind;
  required: boolean;
  is_set: boolean;
  is_secret: boolean;
  /** Credentials: a masked preview when set; never the value. */
  preview: string | null;
  /** Settings and policy values, shown as they are (not secrets). */
  value: string | null;
}

export interface MessagingPlatform {
  id: string;
  name: string;
  description: string;
  installed: boolean;
  install_hint: string | null;
  enabled: boolean;
  explicitly_disabled: boolean;
  configured: boolean;
  /** disabled, not_configured, daemon_stopped, pending_restart, or the daemon's own report. */
  state: string;
  reason: string | null;
  daemon_running: boolean;
  fields: PlatformField[];
}

export interface MessagingPlatformsResponse {
  platforms: MessagingPlatform[];
  daemon_running: boolean;
}

/** Blank values keep what is stored; `clear_env` removes names; `enabled` goes to the config. */
export interface PlatformUpdate {
  enabled?: boolean;
  env?: Record<string, string>;
  clear_env?: string[];
}

export interface PlatformUpdateResponse {
  ok: boolean;
  platform: string;
  restart_needed: boolean;
}

export interface PlatformTestResult {
  ok: boolean;
  state: string;
  message: string;
}

export interface PairingPending {
  platform: string;
  request_id: string;
  user_id: string;
  user_name: string | null;
  age_seconds: number;
}

export interface PairingApproved {
  platform: string;
  user_id: string;
  user_name: string | null;
  approved_at: number | string | null;
}

export interface PairingResponse {
  pending: PairingPending[];
  approved: PairingApproved[];
}

export interface PairingApproveRequest {
  platform: string;
  code?: string;
  request_id?: string;
}

// -- webhooks ---------------------------------------------------------------------------------

export const WEBHOOK_AUTHS = ["auto", "github", "gitlab", "hmac", "hmac_ts"] as const;
export type WebhookAuth = (typeof WEBHOOK_AUTHS)[number];

export interface WebhookRoute {
  name: string;
  /** `subscribed` routes are managed here; `config` routes live in config.yaml (read-only). */
  source: "subscribed" | "config";
  url: string;
  auth: WebhookAuth | "insecure" | string;
  events: string[];
  prompt: string;
  deliver: string | null;
  deliver_only: boolean;
  description: string;
  created_at: string | null;
  enabled: boolean;
  secret_set: boolean;
}

export interface WebhooksResponse {
  receiver_enabled: boolean;
  base_url: string;
  routes: WebhookRoute[];
}

export interface WebhookCreate {
  name: string;
  events?: string[];
  prompt?: string;
  deliver?: string;
  deliver_only?: boolean;
  auth?: WebhookAuth;
  description?: string;
}

/** The create answer: the only response that ever carries the signing secret. */
export interface WebhookCreated extends WebhookRoute {
  secret: string;
}

// -- models and provider sign-in --------------------------------------------------------------

export interface ModelInfo {
  model: string | null;
  provider: string | null;
  config_context_length: number | null;
  family_context_length: number | null;
  effective_context_length: number | null;
}

export interface ProviderOption {
  name: string;
  label: string;
  description: string;
  family: string;
  auth_type: string;
  /** A key or sign-in exists for it. */
  configured: boolean;
  /** A documented device-code sign-in exists. */
  device_login: boolean;
  supports_model_listing: boolean;
}

export interface ModelOptions {
  providers: ProviderOption[];
  current: { provider: string | null; model: string | null };
}

export interface ModelAssignment {
  scope: "main" | "auxiliary";
  task?: string;
  provider: string;
  model: string;
  base_url?: string;
}

export interface AuxiliaryTask {
  task: string;
  provider: string;
  model: string;
  base_url: string;
  description: string;
}

export interface AuxiliaryResponse {
  tasks: AuxiliaryTask[];
  main: { provider: string; model: string };
}

export type LoginStatus = "pending" | "approved" | "denied" | "expired" | "failed" | "cancelled";

export interface LoginSessionInfo {
  session_id: string;
  provider: string;
  status: LoginStatus;
  message: string;
}

export interface LoginStarted extends LoginSessionInfo {
  user_code: string;
  verification_url: string;
  expires_in: number;
  /** Seconds between polls, as the provider asked. */
  interval: number;
}

// -- system -----------------------------------------------------------------------------------

export interface HostStats {
  cpu_count: number | null;
  load: number[] | null;
  cpu_percent?: number;
  memory: { total: number; available: number | null } | null;
  disk: { total: number; free: number; used: number } | null;
}

export interface UpdateInfo {
  install_method: string;
  current_version: string;
  /** Only a git checkout can be updated from here. */
  can_apply: boolean;
  behind: number | null;
  update_available: boolean;
  message: string | null;
  checked_at: number | null;
  cached: boolean;
}

export interface MemoryInfo {
  /** "" = the built-in memory only. */
  active: string;
  providers: string[];
  builtin_files: Record<string, number>;
}

export interface PoolEntryRow {
  /** 1-based, as listed; the delete route takes it. */
  index: number;
  id: string;
  label: string;
  status: string;
  auth_type: string;
  source: string;
  env_var: string | null;
  expires_at: string | null;
  priority: number;
  request_count: number;
  /** Masked; never the value. */
  preview: string;
}

export interface PoolProvider {
  provider: string;
  label: string;
  inherited: boolean;
  entries: PoolEntryRow[];
}

export interface CredentialPoolResponse {
  providers: PoolProvider[];
}

export interface PoolAddRequest {
  provider: string;
  api_key: string;
  label?: string;
}

export interface CheckpointProject {
  key: string;
  workdir: string;
  commits: number;
  bytes: number;
  last_touch: number | null;
  exists: boolean;
}

export interface CheckpointsResponse {
  projects: CheckpointProject[];
  total_bytes: number;
}

export interface ShellHook {
  event: string;
  command: string;
  matcher: string | null;
  timeout: number | null;
  approved: boolean;
  grant_id: string;
  executable: boolean;
}

export interface ShellHooksResponse {
  hooks: ShellHook[];
  events: string[];
  warnings: string[];
}

export interface ShellHookCreate {
  event: string;
  command: string;
  matcher?: string;
  timeout?: number;
  consent: boolean;
}

// -- status (public probe, P18-13) ------------------------------------------------------------

export type Pressure = "ok" | "elevated" | "critical" | "unknown";
export type ComponentHealth =
  | "ok"
  | "degraded"
  | "down"
  | "busy"
  | "corrupt"
  | "unavailable"
  | "unknown";

/** `GET /api/status`; a failed probe answers only `version`, `daemon.state` and `overall`. */
export interface StatusResponse {
  version: string;
  daemon: { state: string; heartbeat_age_s?: number | null };
  overall: "ok" | "degraded" | "unknown";
  platforms?: { name: string; state: string }[];
  active_sessions?: number | null;
  recent_sessions?: number | null;
  auth_required?: boolean;
  auth_providers?: string[];
  components?: {
    store: { status: ComponentHealth };
    daemon: { status: ComponentHealth; state: string };
    platforms: { status: ComponentHealth; configured: number; connected: number };
  };
  memory?: { pressure: Pressure; available_percent?: number };
  disk?: { pressure: Pressure; used_percent?: number; free_gb?: number };
}

// -- sessions ---------------------------------------------------------------------------------

export interface SessionInfo {
  id: string;
  /** Set by a rename; the list falls back to `preview`, then the id. */
  title: string | null;
  preview: string | null;
  /** Where the session came from: cli, tui, web, cron, api_server, acp, a messaging platform… */
  source: string | null;
  model: string | null;
  /** Unix seconds. */
  started_at: number;
  last_active: number;
  ended_at: number | null;
  /** Active in the last few minutes, or holding a running turn. */
  is_active: boolean;
  input_tokens: number;
  output_tokens: number;
  tool_call_count: number;
  message_count: number;
}

export interface SessionListResponse {
  sessions: SessionInfo[];
  total: number;
  limit: number;
  offset: number;
}

export interface SessionToolCall {
  id: string;
  name: string;
  arguments: string;
}

export interface SessionMessage {
  role: "user" | "assistant" | "system" | "tool";
  content: string | null;
  /** Unix seconds, when the store recorded one. */
  timestamp?: number;
  /** Calls an assistant message asked for. */
  tool_calls?: SessionToolCall[];
  /** On a `tool` message: which call it answers, and the tool's name. */
  tool_call_id?: string;
  tool_name?: string;
}

export interface SessionMessagesResponse {
  messages: SessionMessage[];
  session_id: string;
}

export interface SessionSearchHit {
  session_id: string;
  title: string | null;
  source: string | null;
  /** Text with each match wrapped in `<mark>` … `</mark>`; rendered as text, never as HTML. */
  snippet: string;
  role: string | null;
  last_active: number;
}

export interface SessionSearchResponse {
  results: SessionSearchHit[];
}

export interface SessionStats {
  total: number;
  active: number;
  archived: number;
  messages: number;
  by_source: Record<string, number>;
}

export interface MutationSnapshot {
  exists: boolean;
  revision: number;
  generation: number | null;
}

export interface MutationIdentity {
  request_id: string;
  expected_revision: number;
  expected_generation?: number;
}

export interface PruneRequest {
  older_than_days: number;
  dry_run: boolean;
}

/** A conversation file of another coding tool, found under the server user's home (P24-28). */
export interface ImportCandidate {
  source: string;
  path: string;
  title: string;
  cwd: string | null;
  /** Epoch seconds. */
  modified: number;
  size: number;
}

export interface ImportCandidatesResponse {
  sources: string[];
  candidates: ImportCandidate[];
}

export interface ImportedSession {
  session_id: string;
  title: string;
  message_count: number;
}

export interface PruneResponse {
  dry_run: boolean;
  /** Sessions removed, or that would be removed on a dry run. */
  count: number;
  skipped_open: number;
}

// -- config and keys --------------------------------------------------------------------------

export type ConfigValue = string | number | boolean | null | ConfigValue[] | ConfigObject;
export interface ConfigObject {
  [key: string]: ConfigValue;
}

export type ConfigFieldType = "string" | "number" | "integer" | "boolean" | "enum" | "list";

export interface ConfigField {
  type: ConfigFieldType;
  description: string;
  category: string;
  options?: string[];
}

export interface ConfigSchema {
  fields: Record<string, ConfigField>;
  category_order: string[];
}

export type ValueSource = "default" | "file" | "env" | "overlay";

export interface EffectiveEntry {
  key: string;
  value: ConfigValue;
  source: ValueSource;
}

export interface EffectiveConfig {
  entries: EffectiveEntry[];
}

export interface EnvKeyInfo {
  category: string;
  description: string;
  /** Hidden behind the "advanced" toggle. */
  advanced: boolean;
  /** Rendered as a password input. */
  is_password: boolean;
  is_set: boolean;
  /** A masked preview of the stored value; never the value itself (R04.15). */
  redacted_value: string | null;
  /** Where to get a key, if the provider has a sign-up page. */
  url: string | null;
}

export type EnvKeys = Record<string, EnvKeyInfo>;

export interface EnvWriteResponse {
  key: string;
  is_set: boolean;
  redacted_value: string | null;
}

// -- logs, analytics, files -------------------------------------------------------------------

export const LOG_FILES = ["agent", "errors", "gateway"] as const;
export type LogFile = (typeof LOG_FILES)[number];
export const LOG_LEVELS = ["ALL", "DEBUG", "INFO", "WARNING", "ERROR"] as const;
export type LogLevel = (typeof LOG_LEVELS)[number];
export const LOG_COMPONENTS = ["all", "agent", "tools", "gateway", "cli", "cron"] as const;
export type LogComponent = (typeof LOG_COMPONENTS)[number];
export const LOG_TAILS = [50, 100, 200, 500] as const;
export type LogTail = (typeof LOG_TAILS)[number];

export interface LogsResponse {
  file: LogFile;
  lines: string[];
}

export interface UsageDay {
  day: string;
  input_tokens: number;
  output_tokens: number;
  cost: number;
  sessions: number;
}

export interface UsageResponse {
  days: number;
  daily: UsageDay[];
  totals: { input_tokens: number; output_tokens: number; cost: number; sessions: number };
}

export interface FileEntry {
  name: string;
  path: string;
  is_directory: boolean;
  size: number | null;
  mtime: number | null;
}

export interface FileListing {
  path: string;
  parent: string | null;
  locked_root: string | null;
  entries: FileEntry[];
}

export interface FileContent {
  path: string;
  name: string;
  size: number;
  mime_type: string;
  text: string | null;
  truncated: boolean;
}

/** `POST /api/files/download-ticket`: good once, for that path, for `expires_in` seconds. */
export interface DownloadTicket {
  ticket: string;
  expires_in: number;
}

// -- cron -------------------------------------------------------------------------------------

export type CronState = "scheduled" | "paused" | "running" | "completed" | "error";

export interface CronJob {
  id: string;
  name: string;
  prompt: string;
  schedule: string;
  schedule_display: string;
  deliver: string;
  state: CronState;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
  last_status: string | null;
  /** Skills loaded before the prompt runs. */
  skills?: string[];
  /** `times` null = forever. */
  repeat?: { times: number | null; completed: number };
  last_error?: string | null;
  paused_reason?: string | null;
}

export interface CronJobInput {
  name: string;
  prompt: string;
  schedule: string;
  deliver: string;
  skills?: string[];
}

/** One slot of a blueprint (`fields` of a `CronBlueprint`): the human part of the job. */
export interface CronBlueprintField {
  name: string;
  kind: "time" | "weekdays" | "choice" | "interval" | "text";
  label: string;
  default: string | null;
  optional: boolean;
  help: string;
  options?: string[];
  min?: number;
  max?: number;
}

/**
 * A suggested job (`GET /api/cron/blueprints`): the starter catalogue (`source: "catalogue"`,
 * no skill) then the skills' blueprints. `schedule` and `prompt` are prefilled from the slot
 * defaults; a slot with no default stays `{name}` in the prompt for the person to fill.
 */
export interface CronBlueprint {
  /** Unique in the list. */
  key: string;
  title: string;
  source: "catalogue" | "skill";
  skill: string | null;
  description: string;
  schedule: string | null;
  prompt: string | null;
  fields: CronBlueprintField[];
}

export interface DeliveryTarget {
  id: string;
  label: string;
  kind: "local" | "platform" | "email";
  available: boolean;
}

// -- skills, toolsets, plugins, MCP -----------------------------------------------------------

export interface SkillInfo {
  name: string;
  description: string;
  category: string;
  enabled: boolean;
}

export interface ToolsetInfo {
  name: string;
  label: string;
  description: string;
  enabled: boolean;
  tools: string[];
}

export type ScanVerdict = "safe" | "caution" | "dangerous";

export interface HubResult {
  identifier: string;
  name: string;
  description: string;
  source: string;
  trust?: string;
  /** Null in search results: a search downloads nothing, so nothing was scanned yet. */
  verdict: ScanVerdict | null;
  installed: boolean;
  update_available: boolean;
}

export interface HubSearchResponse {
  results: HubResult[];
  /** Hub sources that did not answer in time. */
  timed_out?: string[];
}

export interface PluginInfo {
  name: string;
  version: string | null;
  description: string;
  enabled: boolean;
  source: string | null;
  sha: string | null;
}

export interface PluginsResponse {
  plugins: PluginInfo[];
}

export interface PluginInstallRequest {
  url: string;
  sha: string;
  /** Replace an existing install of the same plugin. */
  force?: boolean;
}

export interface PluginInstallResponse {
  ok: boolean;
  name?: string;
  sha?: string | null;
}

export type McpTransport = "stdio" | "http";
export type McpAuth = "none" | "header" | "oauth";

export interface McpServer {
  name: string;
  enabled: boolean;
  transport: McpTransport;
  /** stdio servers: the command line that starts them. */
  command: string | null;
  args: string[];
  /** Values arrive redacted. */
  env: Record<string, string>;
  /** HTTP servers: endpoint and how the client signs in. */
  url: string | null;
  auth: McpAuth;
}

export interface McpServersResponse {
  servers: McpServer[];
}

/** One server preset (`GET /api/mcp/catalog`): the entry `indus mcp add --preset` writes. */
export interface McpCatalogEntry {
  name: string;
  description: string;
  transport: McpTransport;
  command: string | null;
  args: string[];
  url: string | null;
  /** This profile has a server of that name already. */
  installed: boolean;
}

export interface McpCatalogResponse {
  entries: McpCatalogEntry[];
}

/** Body of an add: a stdio server gives `command` (+ args, env), an HTTP server gives `url`. */
export interface McpServerCreate {
  name: string;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  auth?: McpAuth;
  url?: string;
}

export interface McpTool {
  name: string;
  description: string;
}

export interface McpTestResult {
  ok: boolean;
  error?: string;
  tools: McpTool[];
}

export interface OkResponse {
  ok: boolean;
}
