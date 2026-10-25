// Config sections: their order, names, icons, and the field search (W52-e, Config page).

import {
  Bot,
  Brain,
  ClipboardList,
  Clock,
  Cpu,
  Database,
  Ear,
  FileQuestionMark,
  Globe,
  KeyRound,
  Layers,
  ListChecks,
  Lock,
  MessageCircle,
  MessageSquare,
  Mic,
  Monitor,
  Package,
  Palette,
  Plug,
  Radio,
  Route,
  Server,
  Settings,
  ShieldCheck,
  Sparkles,
  Users,
  Volume2,
  Webhook,
  Wrench,
} from "lucide-react";
import type { ComponentType } from "react";
import type { ConfigField, ConfigSchema } from "../../lib/restTypes.ts";

type Icon = ComponentType<{ className?: string }>;

/** Each icon with the sections drawn with it; a section not listed gets the question-mark file. */
const ICON_GROUPS: ReadonlyArray<readonly [Icon, readonly string[]]> = [
  [Cpu, ["model", "model_catalog"]],
  [Server, ["providers"]],
  [Route, ["fallback"]],
  [Bot, ["agent"]],
  [Settings, ["general"]],
  [Monitor, ["terminal", "tui", "dashboard"]],
  [Palette, ["display"]],
  [Globe, ["browser", "web", "egress", "network"]],
  [Lock, ["security"]],
  [ShieldCheck, ["approvals"]],
  [ListChecks, ["command_allowlist"]],
  [Package, ["compression", "tool_output"]],
  [Brain, ["memory", "learning"]],
  [Users, ["delegation"]],
  [Sparkles, ["skills", "curator"]],
  [Layers, ["toolsets"]],
  [Wrench, ["auxiliary", "tools"]],
  [Plug, ["mcp", "plugins"]],
  [Radio, ["gateway", "messaging", "platforms"]],
  [MessageSquare, ["sessions", "session", "session_search"]],
  [MessageCircle, ["prompt", "discord"]],
  [Database, ["database"]],
  [ClipboardList, ["logging"]],
  [Clock, ["cron"]],
  [Webhook, ["webhooks"]],
  [KeyRound, ["secrets"]],
  [Mic, ["voice"]],
  [Volume2, ["tts"]],
  [Ear, ["stt"]],
];

const ICONS: ReadonlyMap<string, Icon> = new Map(
  ICON_GROUPS.flatMap(([icon, sections]) => sections.map((section) => [section, icon] as const)),
);

const NAMES: Record<string, string> = {
  tts: "Text-to-speech",
  stt: "Speech-to-text",
  mcp: "MCP",
};

export function categoryIcon(category: string): Icon {
  return ICONS.get(category) ?? FileQuestionMark;
}

/** "fallback_providers" → "Fallback providers". */
export function categoryName(category: string): string {
  const named = NAMES[category];
  if (named) return named;
  const words = category.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A field's label: its last key segment with `_` as spaces. */
export function fieldLabel(key: string): string {
  return (key.split(".").pop() ?? key).replace(/_/g, " ");
}

export function fieldCategory(field: ConfigField): string {
  return field.category || "general";
}

/** Categories in the schema's order (unknown ones after it, alphabetically). */
export function orderedCategories(schema: ConfigSchema): string[] {
  const order = schema.category_order;
  const present = [...new Set(Object.values(schema.fields).map(fieldCategory))];
  const rank = (c: string) => {
    const i = order.indexOf(c);
    return i < 0 ? order.length : i;
  };
  return present.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

/** True when the query matches the key, the label, the category or the description. */
export function fieldMatches(key: string, field: ConfigField, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const category = fieldCategory(field);
  return [key, fieldLabel(key), category, categoryName(category), field.description].some((text) =>
    text.toLowerCase().includes(q),
  );
}
