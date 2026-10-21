// The environment of a terminal pane's shell (P20-31), by the same rules as the agent's env
// builder (`src/indus/safety/child_env.py`, R02.22): the credential names Indus's own integrations
// use never reach the shell, nor does any `INDUS_` setting of this process. The desktop cannot read
// the profile's provider registry, so it also drops every `*_API_KEY` name. `terminalEnv.test.ts`
// keeps `CHILD_ENV_FLOOR` equal to the Python floor.

/** The Python builder's static floor (`_FLOOR_GROUPS`), mirrored; a test compares the two. */
export const CHILD_ENV_FLOOR: readonly string[] = [
  // model providers
  "AI_GATEWAY_API_KEY",
  "ANTHROPIC_API_KEY",
  "ANTHROPIC_BASE_URL",
  "ANTHROPIC_TOKEN",
  "AWS_BEARER_TOKEN_BEDROCK",
  "AZURE_OPENAI_API_KEY",
  "AZURE_OPENAI_ENDPOINT",
  "CEREBRAS_API_KEY",
  "COHERE_API_KEY",
  "DASHSCOPE_API_KEY",
  "DEEPSEEK_API_KEY",
  "FIREWORKS_API_KEY",
  "GEMINI_API_KEY",
  "GLM_API_KEY",
  "GOOGLE_API_KEY",
  "GOOGLE_APPLICATION_CREDENTIALS",
  "GROQ_API_KEY",
  "HELICONE_API_KEY",
  "HF_TOKEN",
  "KIMI_API_KEY",
  "LM_API_KEY",
  "LM_BASE_URL",
  "MINIMAX_API_KEY",
  "MISTRAL_API_KEY",
  "MOONSHOT_API_KEY",
  "NVIDIA_API_KEY",
  "OLLAMA_API_KEY",
  "OPENAI_API_BASE",
  "OPENAI_API_KEY",
  "OPENAI_BASE_URL",
  "OPENAI_ORGANIZATION",
  "OPENAI_ORG_ID",
  "OPENROUTER_API_KEY",
  "PERPLEXITY_API_KEY",
  "TOGETHER_API_KEY",
  "XAI_API_KEY",
  "ZAI_API_KEY",
  // web backends
  "BRAVE_API_KEY",
  "EXA_API_KEY",
  "FIRECRAWL_API_KEY",
  "FIRECRAWL_API_URL",
  "JINA_API_KEY",
  "PARALLEL_API_KEY",
  "SERPAPI_API_KEY",
  "TAVILY_API_KEY",
  // messaging platforms
  "DISCORD_BOT_TOKEN",
  "EMAIL_PASSWORD",
  "HASS_TOKEN",
  "MATTERMOST_TOKEN",
  "SIGNAL_ACCOUNT",
  "SLACK_APP_TOKEN",
  "SLACK_BOT_TOKEN",
  "SLACK_SIGNING_SECRET",
  "TELEGRAM_BOT_TOKEN",
  "TELEGRAM_WEBHOOK_SECRET",
  "WHATSAPP_ACCESS_TOKEN",
  "WHATSAPP_APP_SECRET",
  // github
  "GH_TOKEN",
  "GITHUB_APP_ID",
  "GITHUB_APP_INSTALLATION_ID",
  "GITHUB_APP_PRIVATE_KEY_PATH",
  "GITHUB_TOKEN",
  // sandbox vendors
  "DAYTONA_API_KEY",
  "DAYTONA_JWT_TOKEN",
  "E2B_API_KEY",
  "MODAL_OAUTH_CLIENT_SECRET",
  "MODAL_OAUTH_REFRESH_TOKEN",
  "MODAL_TOKEN_ID",
  "MODAL_TOKEN_SECRET",
  "VERCEL_OIDC_TOKEN",
  "VERCEL_PROJECT_ID",
  "VERCEL_TEAM_ID",
  "VERCEL_TOKEN",
];

const FLOOR = new Set(CHILD_ENV_FLOOR.map((name) => name.toUpperCase()));

/** Names the Electron process carries that make no sense (or harm) in a person's shell. */
const DESKTOP_ONLY = new Set([
  "ELECTRON_RUN_AS_NODE",
  "ELECTRON_NO_ATTACH_CONSOLE",
  "ELECTRON_ENABLE_LOGGING",
  "ELECTRON_ENABLE_STACK_DUMPING",
  "CHROME_DESKTOP",
  "NODE_OPTIONS",
  "PYTHONHOME",
  // Colour switches an agent runner may leave behind; this is a real colour terminal.
  "NO_COLOR",
  "FORCE_COLOR",
  "COLORFGBG",
]);

/** True when the shell must not inherit `name`. */
export function isTerminalBlocked(name: string): boolean {
  const upper = name.toUpperCase();
  if (FLOOR.has(upper)) return true;
  if (upper.endsWith("_API_KEY")) return true;
  if (upper.startsWith("INDUS_") || upper.startsWith("_INDUS_")) return true;
  if (upper.startsWith("NPM_CONFIG_") || upper.startsWith("NPM_PACKAGE_")) return true;
  return DESKTOP_ONLY.has(upper);
}

/** `LC_CTYPE` for the shell: glibc refuses the bare `UTF-8` macOS accepts. */
export function terminalLocale(
  env: Readonly<Record<string, string | undefined>>,
  platform: string,
): string {
  const explicit = env.LC_CTYPE?.trim();
  if (explicit !== undefined && explicit !== "") return explicit;
  if (platform === "darwin") return "UTF-8";
  const lang = env.LANG?.trim();
  return lang !== undefined && lang !== "" ? lang : "C.UTF-8";
}

/** The shell's environment from this process's. */
export function terminalEnv(
  base: Readonly<Record<string, string | undefined>>,
  options: { platform: string; appVersion: string },
): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [name, value] of Object.entries(base)) {
    if (value === undefined || isTerminalBlocked(name)) continue;
    env[name] = value;
  }
  env.TERM = "xterm-256color";
  env.COLORTERM = "truecolor";
  env.TERM_PROGRAM = "Indus";
  env.TERM_PROGRAM_VERSION = options.appVersion;
  if (options.platform !== "win32") env.LC_CTYPE = terminalLocale(base, options.platform);
  return env;
}
