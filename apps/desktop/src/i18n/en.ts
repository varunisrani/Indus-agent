// English strings (D-16.51: English only in 1.0, every visible string through `useT`).
// One flat record; keys are `area.thing`. Other slices append their own block at the end.

import { BRAND_NAME } from "@indus/ui";
import { AMBIENT_STRINGS } from "../ambient/strings.en.ts";
import { CAPABILITY_STRINGS } from "../app/capabilities/strings.en.ts";
import { COMMAND_CENTER_STRINGS } from "../app/command-center/strings.en.ts";
import { MANAGE_STRINGS } from "../app/manage/strings.en.ts";
import { COMPOSER_MENU_STRINGS } from "../chat/composer/strings.en.ts";
import { CHAT_STRINGS } from "../chat/strings.en.ts";
import { TRANSCRIPT_STRINGS } from "../chat/transcriptStrings.en.ts";
import { VOICE_STRINGS } from "../chat/voice/strings.en.ts";
import { PLUGIN_STRINGS } from "../contrib/strings.en.ts";
import { DEEP_LINK_STRINGS } from "../deepLinks/strings.en.ts";
import { ONBOARDING_STRINGS } from "../onboarding/strings.en.ts";
import { ANNOTATE_STRINGS } from "../panes/preview/annotateStrings.en.ts";
import { PANE_STRINGS } from "../panes/strings.en.ts";
import { PET_STRINGS } from "../pet/strings.en.ts";
import { PROFILE_STRINGS } from "../profiles/strings.en.ts";
import { SETTINGS_STRINGS } from "../settings/strings.en.ts";
import { SETTINGS_A_STRINGS } from "../settings/stringsA.en.ts";
import { SETTINGS_B_STRINGS } from "../settings/stringsB.en.ts";
import { UNINSTALL_STRINGS } from "../settings/uninstallStrings.en.ts";
import { SIDEBAR_STRINGS } from "../sidebar/strings.en.ts";
import { THEME_STRINGS } from "../theme/strings.en.ts";
import { WAKE_STRINGS } from "../wake/strings.en.ts";

export const en = {
  "app.name": "Indus",
  "common.working": "Working",
  "common.close": "Close",
  "common.comingLater": "Coming later",

  "window.later.title": "This window arrives in a later release",

  "error.title": "Something went wrong in this window",
  "error.body": "Your conversations are safe. Reloading the window usually fixes this.",
  "error.reload": "Reload window",

  "nav.label": "Main",
  "nav.newChat": "New session",
  "nav.sessions": "Sessions",
  "nav.hideSidebar": "Hide sidebar",
  "nav.showSidebar": "Show sidebar",
  "nav.capabilities": "Capabilities",
  "nav.messaging": "Messaging",
  "nav.artifacts": "Artifacts",
  "nav.settings": "Settings",
  "nav.cron": "Scheduled jobs",
  "nav.profiles": "Profiles",
  "nav.agents": "Agents",
  "nav.more": "More",

  "page.capabilities.body": "Skills, tools and connectors arrive in a later release.",
  "page.messaging.body": "Messaging platforms arrive in a later release.",
  "page.artifacts.body": "Files the agent made for you arrive in a later release.",
  "overlay.settings.body": "Settings arrive in a later release.",
  "overlay.cron.body": "Scheduled tasks arrive in a later release.",
  "overlay.profiles.body": "Profile management arrives in a later release.",
  "overlay.agents.body": "Agent management arrives in a later release.",

  "theme.label": "Theme",
  "theme.light": "Light",
  "theme.dark": "Dark",
  "theme.system": "System",

  "connection.idle": "Starting",
  "connection.connecting": "Connecting",
  "connection.open": "Connected",
  "connection.reconnecting": "Reconnecting",
  "connection.failed": "Not connected",
  "connection.closed": "Disconnected",
  "connection.warning":
    "Still trying to reach Indus. Your conversation stays here and you can keep writing.",

  "chat.label": "Conversation",
  "chat.empty.title": BRAND_NAME,
  "chat.empty.body":
    "Describe a task, ask a question or paste a snippet. Indus keeps the whole session in mind and asks before it guesses.",
  "chat.composer.float": "Float the message box (drag to move)",
  "chat.composer.dock": "Dock the message box (drag or arrow keys to move)",

  "transcript.label": "Messages",
  "transcript.showEarlier": "Show earlier messages",
  "transcript.scrollToBottom": "Scroll to bottom",
  "transcript.timeline": "Prompts",
  "transcript.jumpTo": "Go to prompt: {text}",
  "transcript.you": "You",
  "transcript.assistant": "Indus",
  "transcript.reasoning": "Thinking",
  "transcript.interrupted": "Stopped",
  "transcript.stalled": "No news from this turn for a while.",
  "transcript.async.review": "Background review saved changes",
  "transcript.async.heartbeat": "Heartbeat ran",

  "tool.verb.run": "Ran",
  "tool.verb.read": "Read",
  "tool.verb.write": "Wrote",
  "tool.verb.edit": "Edited",
  "tool.verb.search": "Searched",
  "tool.verb.fetch": "Fetched",
  "tool.verb.use": "Used {name}",
  "tool.state.running": "Running",
  "tool.state.ok": "Done",
  "tool.state.failed": "Failed",
  "tool.state.neutral": "Finished",
  "tool.arguments": "Arguments",
  "tool.output": "Output",
  "tool.noOutput": "No output",
  "tool.showAll": "Show all output",
  "tool.duration": "{seconds} s",
  "tool.image": "Image from {name}",

  "failure.layer.provider": "The model provider failed",
  "failure.layer.endpoint": "The model endpoint could not be reached",
  "failure.layer.streaming": "The reply stream broke off",
  "failure.layer.auth": "Sign-in was refused",
  "failure.layer.daemon": "Lost the connection to Indus",
  "failure.layer.runtime": "The agent stopped",
  "failure.layer.disk": "Could not write to disk",
  "failure.reason.default": "The turn ended without an answer.",
  "failure.reason.max_iterations": "The turn reached its step limit.",
  "failure.reason.budget_exhausted": "The turn ran out of budget.",
  "failure.reason.invalid_tool_strikes": "The model kept calling tools the wrong way.",
  "failure.reason.compression_exhausted": "The conversation is too long to continue.",
  "failure.reason.guardrail_halt": "A safety rule stopped the turn.",
  "failure.reason.persistence_failed": "The conversation could not be saved.",
  "failure.reason.provider_failed": "The provider returned an error.",
  "failure.resetsAt": "You can try again after {time}.",
  "failure.action.retry": "Retry",
  "failure.action.choose-model": "Choose a model",
  "failure.action.open-settings": "Open settings",
  "failure.action.sign-in": "Sign in again",
  "failure.action.reconnect": "Reconnect",
  "failure.action.open-logs": "Open logs",
  "failure.action.new-session": "New chat",

  "link.open": "Open {url} in your browser",

  "boot.phase.resolve": "Looking for Indus on this computer",
  "boot.phase.runtime": "Starting Indus",
  "boot.phase.remote-connect": "Connecting to your Indus",
  "boot.phase.update-wait": "Waiting for an update to finish",
  "boot.phase.first-run": "Welcome",
  "boot.phase.ready": "Ready",
  "boot.phase.error": "Indus could not start",
  "boot.install.title": "Installing Indus",
  "boot.install.stages": "Steps",
  "boot.install.log": "Installer output",
  "boot.install.cancel": "Cancel install",
  "boot.stage.pending": "Waiting",
  "boot.stage.running": "Running",
  "boot.stage.succeeded": "Done",
  "boot.stage.skipped": "Skipped",
  "boot.stage.failed": "Failed",
  "boot.failure.title": "Indus could not start",
  "boot.failure.retry": "Retry",
  "boot.failure.repair": "Repair",
  "boot.failure.settings": "Settings",
  "boot.failure.latched": "Trying again by itself will not help. Repair sets up what is missing.",
  "boot.failure.retrying": "Trying again in {seconds} s (attempt {attempt} of {max})",
  "boot.crash.title": "What the backend printed last",
  "boot.crash.exit": "{message} (exit code {code})",
  "boot.reauth.title": "Sign in again",
  "boot.reauth.body": "Your Indus refused this app's credentials.",
  "boot.reauth.action": "Open connection settings",
  "boot.chooser.title": "How do you want to use Indus?",
  "boot.chooser.body": "Indus runs the agent. Use one you already have, or install it here.",
  "boot.chooser.remote": "Connect to an existing Indus",
  "boot.chooser.remoteHint": "Another computer or a server that runs Indus.",
  "boot.chooser.local": "Install on this computer",
  "boot.chooser.localHint": "Downloads and sets up Indus for you.",

  // find in page, logs viewer (P20-39)
  "find.label": "Find in conversation",
  "find.placeholder": "Find",
  "find.count": "{current} of {total}",
  "find.none": "No results",
  "find.previous": "Previous match",
  "find.next": "Next match",
  "logs.title": "Desktop log",
  "logs.refresh": "Refresh",
  "logs.reveal": "Show log file",
  "logs.copyDiagnostics": "Copy diagnostics",
  "logs.copied": "Diagnostics copied. Secrets were removed before copying.",
  "logs.copyFailed": "Diagnostics could not be copied.",
  "logs.loadFailed": "The log could not be read.",
  "logs.empty": "Nothing logged yet in this session.",
  "logs.unavailable": "The log is available in the desktop app only.",

  // chat surfaces (P20-19..24: composer, queue, cards, model picker, status bar, slash)
  ...CHAT_STRINGS,
  // composer menus, model and effort menus (W52-h)
  ...COMPOSER_MENU_STRINGS,

  // sessions sidebar (P20-25), profile rail and switching (P20-26)
  ...SIDEBAR_STRINGS,
  ...PROFILE_STRINGS,

  // pane shell (P20-27) and working panes (P20-28..31: preview, files, git review, terminal)
  ...PANE_STRINGS,

  // settings, onboarding, deep links (P20-32..34)
  ...SETTINGS_STRINGS,
  ...SETTINGS_A_STRINGS, // settings slice A (W52-i)
  ...ONBOARDING_STRINGS,
  ...DEEP_LINK_STRINGS,

  // HUD, quick entry, tray, keep awake, notifications, updates (P20-35..37)
  ...AMBIENT_STRINGS,

  // desktop pet (P24-19), wake word (P24-20)
  ...PET_STRINGS,
  ...WAKE_STRINGS,

  // preview annotate mode (P24-25)
  ...ANNOTATE_STRINGS,

  // capabilities page (P24-26)
  ...CAPABILITY_STRINGS,

  // themes and appearance (P24-33)
  ...THEME_STRINGS,

  // management pages (P24-27..31)
  ...MANAGE_STRINGS,

  // uninstall from Settings → About (P24-34)
  ...UNINSTALL_STRINGS,

  // composer voice: dictation, read-aloud, spoken conversation (P24-35)
  ...VOICE_STRINGS,

  // desktop plugin inventory (P24-32 wiring)
  ...PLUGIN_STRINGS,

  // transcript look: approval / error cards, tool rows, toasts (W52-g)
  ...TRANSCRIPT_STRINGS,
  // settings-b pages (W52-j)
  ...SETTINGS_B_STRINGS,

  // command center, management overlays, artifacts, layout editor (W52-l)
  ...COMMAND_CENTER_STRINGS,
} as const satisfies Record<string, string>;
