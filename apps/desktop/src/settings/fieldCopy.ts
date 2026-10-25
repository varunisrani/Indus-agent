// How a schema field reads on a settings page: a short label and an optional one-line description
// in our words, plus how an open string is offered (a placeholder, a fixed option list or
// suggestions). ONE table for every settings page and for search (W53: settings A, W52-i, and
// settings B, W52-j, merged). A field without an entry keeps the label derived from its key
// (`fieldLabel`) and the backend's own description.

import type { MessageKey } from "../i18n/index.tsx";

export interface FieldCopy {
  readonly label: MessageKey;
  /** Replaces the schema's description; "" hides it. */
  readonly description?: MessageKey | "";
  /** Shown in an empty text field (default "Not set"). */
  readonly placeholder?: MessageKey;
  /** A string field offered as a compact select (an unknown stored value stays selectable). The
   * option labels are names (product or model sizes), shown as they are. */
  readonly options?: readonly { readonly value: string; readonly label: string }[];
  /** A string field offered as a free text box with suggestions (read when the row renders). */
  readonly suggestions?: () => readonly string[];
}

const opts = (...values: string[]) => values.map((value) => ({ value, label: value }));

function timeZones(): readonly string[] {
  const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
  try {
    return intl.supportedValuesOf?.("timeZone") ?? [];
  } catch {
    return [];
  }
}

export const FIELD_COPY: Readonly<Record<string, FieldCopy>> = {
  // -- settings A (W52-i): Model, Chat, Workspace, Safety, Browser ---------------------------------
  "approvals.mode": {
    label: "settingsA.field.approvalsMode",
    description: "settingsA.field.approvalsModeBody",
  },
  "approvals.timeout": {
    label: "settingsA.field.approvalsTimeout",
    description: "settingsA.field.approvalsTimeoutBody",
  },
  "approvals.mcp_reload_confirm": { label: "settingsA.field.mcpReload" },
  command_allowlist: { label: "settingsA.field.allowlist" },
  "security.redact_secrets": {
    label: "settingsA.field.redact",
    description: "settingsA.field.redactBody",
  },
  "security.allow_private_urls": { label: "settingsA.field.privateUrls" },
  "checkpoints.enabled": {
    label: "settingsA.field.checkpoints",
    description: "settingsA.field.checkpointsBody",
  },
  "browser.real_profile": {
    label: "settingsA.field.realProfile",
    description: "settingsA.field.realProfileBody",
  },
  "browser.real_profile_dir": { label: "settingsA.field.realProfileDir" },
  "browser.route_private_local": { label: "settingsA.field.routeLocal" },
  "terminal.cwd": { label: "settingsA.field.cwd", description: "settingsA.field.cwdBody" },
  "terminal.persistent_shell": {
    label: "settingsA.field.persistentShell",
    description: "settingsA.field.persistentShellBody",
  },
  "terminal.env_passthrough": {
    label: "settingsA.field.envPassthrough",
    description: "settingsA.field.envPassthroughBody",
  },
  "code_execution.mode": {
    label: "settingsA.field.codeMode",
    description: "settingsA.field.codeModeBody",
  },
  file_read_max_chars: {
    label: "settingsA.field.readLimit",
    description: "settingsA.field.readLimitBody",
  },
  timezone: {
    label: "settingsA.field.timezone",
    description: "settingsA.field.timezoneBody",
    placeholder: "settingsA.search",
    suggestions: timeZones,
  },
  "display.show_reasoning": {
    label: "settingsA.field.reasoning",
    description: "settingsA.field.reasoningBody",
  },
  "display.busy_input_mode": {
    label: "settingsA.field.busyMode",
    description: "settingsA.field.busyModeBody",
  },
  "agent.fast_mode": { label: "settingsA.field.speed", description: "settingsA.field.speedBody" },
  "model.context_length": {
    label: "settingsA.field.contextWindow",
    description: "settingsA.field.contextWindowBody",
  },
  "model.base_url": { label: "settingsA.field.baseUrl" },
  "model.api_mode": { label: "settingsA.field.apiMode" },
  "fallback.min_switch_reset_s": {
    label: "settingsA.field.minSwitch",
    description: "settingsA.field.minSwitchBody",
  },
  "moa.reference_timeout": { label: "settingsA.field.moaTimeout" },
  "moa.privacy_filter": { label: "settingsA.field.moaPrivacy" },
  // -- settings B (W52-j): Memory, Voice, Advanced, Sessions ----------------------------------------
  // Memory & Context › Persistent memory
  "memory.notes_enabled": {
    label: "settingsB.field.memoryNotesEnabled",
    description: "settingsB.field.memoryNotesEnabledBody",
  },
  "memory.profile_enabled": {
    label: "settingsB.field.memoryProfileEnabled",
    description: "settingsB.field.memoryProfileEnabledBody",
  },
  "memory.notes_char_limit": { label: "settingsB.field.memoryNotesCharLimit", description: "" },
  "memory.profile_char_limit": { label: "settingsB.field.memoryProfileCharLimit", description: "" },
  "memory.provider": {
    label: "settingsB.field.memoryProvider",
    description: "settingsB.field.memoryProviderBody",
    placeholder: "settingsB.field.memoryProviderPlaceholder",
  },
  // Memory & Context › Context & compression
  "context.engine": {
    label: "settingsB.field.contextEngine",
    description: "settingsB.field.contextEngineBody",
    placeholder: "settingsB.field.contextEnginePlaceholder",
  },
  "compression.enabled": {
    label: "settingsB.field.compressionEnabled",
    description: "settingsB.field.compressionEnabledBody",
  },
  "compression.threshold": { label: "settingsB.field.compressionThreshold", description: "" },
  "compression.target_ratio": { label: "settingsB.field.compressionTargetRatio", description: "" },
  "compression.min_tail_user_messages": {
    label: "settingsB.field.compressionMinTailUserMessages",
    description: "",
  },
  "compression.timeout_s": {
    label: "settingsB.field.compressionTimeoutS",
    description: "settingsB.field.compressionTimeoutSBody",
  },
  // Voice
  "voice.record_key": {
    label: "settingsB.field.voiceRecordKey",
    description: "settingsB.field.voiceRecordKeyBody",
  },
  "voice.max_seconds": { label: "settingsB.field.voiceMaxSeconds", description: "" },
  "voice.silence_seconds": {
    label: "settingsB.field.voiceSilenceSeconds",
    description: "settingsB.field.voiceSilenceSecondsBody",
  },
  "voice.wait_seconds": {
    label: "settingsB.field.voiceWaitSeconds",
    description: "settingsB.field.voiceWaitSecondsBody",
  },
  "stt.provider": {
    label: "settingsB.field.sttProvider",
    placeholder: "settingsB.field.sttProviderPlaceholder",
    suggestions: () => ["local", "openai", "groq"],
  },
  "stt.local_model": {
    label: "settingsB.field.sttLocalModel",
    description: "settingsB.field.sttLocalModelBody",
    options: opts("tiny", "base", "small", "medium", "large-v3"),
  },
  "stt.local_device": {
    label: "settingsB.field.sttLocalDevice",
    description: "settingsB.field.sttLocalDeviceBody",
    options: opts("auto", "cpu", "cuda"),
  },
  "stt.language": { label: "settingsB.field.sttLanguage", description: "" },
  "stt.model": { label: "settingsB.field.sttModel", description: "" },
  "tts.provider": {
    label: "settingsB.field.ttsProvider",
    options: [
      { value: "piper", label: "Piper" },
      { value: "openai", label: "OpenAI" },
    ],
  },
  "voice.read_aloud": {
    label: "settingsB.field.voiceReadAloud",
    description: "settingsB.field.voiceReadAloudBody",
  },
  "tts.voice": { label: "settingsB.field.ttsVoice" },
  "tts.speed": { label: "settingsB.field.ttsSpeed", description: "" },
  // Advanced › Agent limits
  "agent.max_turns": {
    label: "settingsB.field.agentMaxTurns",
    description: "settingsB.field.agentMaxTurnsBody",
  },
  "agent.api_max_retries": { label: "settingsB.field.agentApiMaxRetries", description: "" },
  // Advanced › Tool access
  "prompt.tool_use_enforcement": {
    label: "settingsB.field.promptToolUseEnforcement",
    description: "",
  },
  "code_execution.timeout": { label: "settingsB.field.codeExecutionTimeout", description: "" },
  "code_execution.max_tool_calls": {
    label: "settingsB.field.codeExecutionMaxToolCalls",
    description: "",
  },
  // Advanced › Terminal backend
  "terminal.backend": {
    label: "settingsB.field.terminalBackend",
    description: "settingsB.field.terminalBackendBody",
    options: [
      { value: "local", label: "Local" },
      { value: "docker", label: "Docker" },
      { value: "ssh", label: "SSH" },
      { value: "singularity", label: "Singularity" },
      { value: "modal", label: "Modal" },
      { value: "daytona", label: "Daytona" },
      { value: "vercel", label: "Vercel" },
    ],
  },
  "terminal.timeout": { label: "settingsB.field.terminalTimeout", description: "" },
  "terminal.docker_image": {
    label: "settingsB.field.terminalDockerImage",
    description: "settingsB.field.terminalDockerImageBody",
  },
  "terminal.singularity_image": { label: "settingsB.field.terminalSingularityImage" },
  "terminal.modal_image": { label: "settingsB.field.terminalModalImage" },
  "terminal.daytona_image": { label: "settingsB.field.terminalDaytonaImage" },
  // Advanced › Subagents
  "delegation.model": {
    label: "settingsB.field.delegationModel",
    placeholder: "settingsB.field.delegationModelPlaceholder",
  },
  "delegation.max_iterations": {
    label: "settingsB.field.delegationMaxIterations",
    description: "",
  },
  "delegation.max_concurrent_children": {
    label: "settingsB.field.delegationMaxConcurrentChildren",
    description: "",
  },
  "delegation.max_depth": { label: "settingsB.field.delegationMaxDepth", description: "" },
  // Advanced › Output limits
  "tool_output.max_chars": { label: "settingsB.field.toolOutputMaxChars", description: "" },
  "checkpoints.max_snapshots": {
    label: "settingsB.field.checkpointsMaxSnapshots",
    description: "",
  },
  // Sessions › Archive & retention
  "sessions.auto_prune": {
    label: "settingsB.field.sessionsAutoPrune",
    description: "settingsB.field.sessionsAutoPruneBody",
  },
  "sessions.retention_days": { label: "settingsB.field.sessionsRetentionDays", description: "" },
};

export function fieldCopy(key: string): FieldCopy | undefined {
  return FIELD_COPY[key];
}
