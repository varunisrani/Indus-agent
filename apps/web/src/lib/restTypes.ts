// REST shapes the pages read and write. Hand-written for the prework (P18-17..22); P18-11..16
// generate the real ones from the Pydantic models in `indus.protocol` into `@indus/protocol`, and
// this file then re-exports those names.

export interface ProfileSummary {
  name: string;
  is_default: boolean;
}

export interface ProfilesResponse {
  profiles: ProfileSummary[];
  active: string;
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
}

export interface CronJobInput {
  name: string;
  prompt: string;
  schedule: string;
  deliver: string;
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
  verdict: ScanVerdict;
  installed: boolean;
  update_available: boolean;
}

export interface HubSearchResponse {
  results: HubResult[];
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
