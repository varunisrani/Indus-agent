// What the launcher (`indus --tui`, P12-32) hands the renderer through the environment, read in
// one place (research 05 §3.29): the startup resume id and prompt, the active-session file, and the
// options a new session is created with.

export type Env = Readonly<Record<string, string | undefined>>;

export const ENV = {
  resume: "INDUS_TUI_RESUME",
  query: "INDUS_TUI_QUERY",
  image: "INDUS_TUI_IMAGE",
  model: "INDUS_MODEL",
  provider: "INDUS_TUI_PROVIDER",
  reasoning: "INDUS_TUI_REASONING",
  cwd: "INDUS_CWD",
  tuiCwd: "INDUS_TUI_CWD",
  toolsets: "INDUS_TUI_TOOLSETS",
  skills: "INDUS_TUI_SKILLS",
  maxTurns: "INDUS_TUI_MAX_TURNS",
  toolProgress: "INDUS_TUI_TOOL_PROGRESS",
  activeSessionFile: "INDUS_TUI_ACTIVE_SESSION_FILE",
} as const;

/**
 * Launch switches that become boolean creation options: option name → env names (the launcher
 * exports the `INDUS_TUI_*` spelling; the general one is read too).
 */
export const FLAG_ENV: Readonly<Record<string, readonly string[]>> = {
  yolo: ["INDUS_TUI_YOLO", "INDUS_YOLO_MODE"],
  ignore_rules: ["INDUS_TUI_IGNORE_RULES", "INDUS_IGNORE_RULES"],
  safe_mode: ["INDUS_TUI_SAFE_MODE", "INDUS_SAFE_MODE"],
  ignore_user_config: ["INDUS_TUI_IGNORE_USER_CONFIG", "INDUS_IGNORE_USER_CONFIG"],
  checkpoints: ["INDUS_TUI_CHECKPOINTS"],
  pass_session_id: ["INDUS_TUI_PASS_SESSION_ID"],
  accept_hooks: ["INDUS_TUI_ACCEPT_HOOKS", "INDUS_ACCEPT_HOOKS"],
};

/** The `session.create` keys our protocol declares; the bridge refuses any other key. */
export const DECLARED_CREATE_KEYS: ReadonlySet<string> = new Set([
  "title",
  "cwd",
  "model",
  "toolsets",
  "idempotency_key",
]);

const TRUTHY: ReadonlySet<string> = new Set(["1", "true", "yes", "on"]);

/** A launch flag's value counts as set when it reads 1, true, yes or on (any case). */
export function isTruthy(value: string | undefined): boolean {
  return TRUTHY.has((value ?? "").trim().toLowerCase());
}

function text(env: Env, name: string): string | null {
  const value = env[name]?.trim();
  return value ? value : null;
}

/** Comma (and, for skills, newline) separated names: trimmed, empty ones and repeats dropped. */
export function nameList(raw: string | undefined, separators: RegExp = /,/): string[] {
  const seen = new Set<string>();
  for (const piece of (raw ?? "").split(separators)) {
    const name = piece.trim();
    if (name) seen.add(name);
  }
  return [...seen];
}

/** `--max-turns`: a whole number travels as a number, a word ("unlimited") as the word. */
export function maxTurnsValue(raw: string): number | string {
  const trimmed = raw.trim();
  return /^[-+]?\d+$/.test(trimmed) ? Number.parseInt(trimmed, 10) : trimmed;
}

/** Every creation option the launch environment asks for (declared by our protocol or not). */
export function launchOptions(env: Env): Record<string, unknown> {
  const options: Record<string, unknown> = {};
  const model = text(env, ENV.model);
  if (model) options.model = model;
  const provider = text(env, ENV.provider);
  if (provider) options.provider = provider;
  const reasoning = text(env, ENV.reasoning);
  if (reasoning) options.reasoning_effort = reasoning;
  const cwd = text(env, ENV.tuiCwd) ?? text(env, ENV.cwd);
  if (cwd) options.cwd = cwd;
  const toolsets = nameList(env[ENV.toolsets]);
  if (toolsets.length > 0) options.toolsets = toolsets;
  const skills = nameList(env[ENV.skills], /[,\n]/);
  if (skills.length > 0) options.skills = skills;
  const turns = text(env, ENV.maxTurns);
  if (turns) options.max_turns = maxTurnsValue(turns);
  for (const [option, names] of Object.entries(FLAG_ENV)) {
    if (names.some((name) => isTruthy(env[name]))) options[option] = true;
  }
  return options;
}

/** Split options into what can be sent and the names the bridge would refuse. */
export function declaredOnly(options: Record<string, unknown>): {
  params: Record<string, unknown>;
  dropped: string[];
} {
  const params: Record<string, unknown> = {};
  const dropped: string[] = [];
  for (const [key, value] of Object.entries(options)) {
    if (DECLARED_CREATE_KEYS.has(key)) params[key] = value;
    else dropped.push(key);
  }
  return { params, dropped };
}

/** The one-shot startup prompt: query text and/or an image path. */
export interface StartupPrompt {
  readonly text: string;
  readonly image: string | null;
}

/** Said when only an image was given. */
export const IMAGE_ONLY_PROMPT = "Describe this image.";

export function startupPrompt(env: Env): StartupPrompt | null {
  const query = env[ENV.query];
  const image = text(env, ENV.image);
  const hasQuery = query !== undefined && query.trim() !== "";
  if (!hasQuery && image === null) return null;
  return { text: hasQuery ? query : IMAGE_ONLY_PROMPT, image };
}

export function startupResumeId(env: Env): string | null {
  return text(env, ENV.resume);
}

/** The tool-progress modes `config.set key=verbose` takes. */
export const TOOL_PROGRESS_MODES: ReadonlySet<string> = new Set(["off", "new", "all", "verbose"]);

/**
 * `-v` / `-Q`: the tool-progress mode applied with `config.set key=verbose` after create. Null when
 * unset; `{ invalid }` when the value is not a mode (it is reported, never sent).
 */
export function toolProgressMode(env: Env): string | { invalid: string } | null {
  const mode = text(env, ENV.toolProgress)?.toLowerCase() ?? null;
  if (mode === null) return null;
  return TOOL_PROGRESS_MODES.has(mode) ? mode : { invalid: mode };
}
