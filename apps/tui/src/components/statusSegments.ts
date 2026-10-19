/**
 * What the status rule shows at a given width. Pure: the component only paints the result.
 *
 * Model, context and the YOLO badge always get room. Lower-priority segments appear from their
 * width breakpoint on, and only when the whole segment still fits after the essentials and the
 * right-hand label; nothing is cut in the middle.
 */
import { cellWidth } from "../render/styled.tsx";

export type SegmentId =
  | "indicator"
  | "status"
  | "model"
  | "context"
  | "yolo"
  | "bar"
  | "duration"
  | "compressions"
  | "voice"
  | "bg_tasks"
  | "subagents"
  | "cache_hit"
  | "latency"
  | "tps";

export type SegmentTone = "text" | "muted" | "accent" | "ok" | "warn" | "error";

export interface StatusSegment {
  id: SegmentId;
  text: string;
  tone: SegmentTone;
}

/** Names accepted in `display.status_bar.fields` (shared with the classic CLI's bar). */
/** Normalises the config list: lower-case names; missing, empty or malformed means `null`. */
export function readStatusFields(raw: unknown): ReadonlySet<string> | null {
  if (!Array.isArray(raw)) return null;
  const names = raw.map((v) => String(v).trim().toLowerCase()).filter(Boolean);
  return names.length > 0 ? new Set(names) : null;
}

export const STATUS_FIELDS = [
  "context_detail",
  "context_pct",
  "title",
  "duration",
  "compressions",
  "voice",
  "bg_tasks",
  "bg_subagents",
  "cache_hit",
  "latency",
  "tps",
] as const;
export type StatusField = (typeof STATUS_FIELDS)[number];

export const BREAKPOINTS = {
  bar: 72,
  duration: 76,
  compressions: 80,
  voice: 84,
  bg_tasks: 88,
  subagents: 92,
  cache_hit: 96,
  latency: 104,
  tps: 110,
} as const;

export interface StatusInput {
  cols: number;
  busy: boolean;
  statusText: string;
  model: string;
  reasoningEffort?: string;
  contextUsed?: number;
  contextMax?: number;
  totalTokens?: number;
  durationMs?: number;
  compressions?: number;
  /** Voice state label; absent until voice input exists. */
  voice?: string;
  bgTasks?: number;
  subagents?: number;
  cacheHitPct?: number;
  latencyS?: number;
  tps?: number;
  title?: string;
  cwdLabel?: string;
  branch?: string;
  yolo?: boolean;
  /**
   * `display.status_bar.fields`: the segments to show. `null`/absent (not customised) shows
   * everything; otherwise a segment shows only when its field name is listed.
   */
  fields?: ReadonlySet<string> | null;
  indicatorStyle?: string;
  /** Animation tick for the busy indicator. */
  frame?: number;
}

export interface StatusLayout {
  left: StatusSegment[];
  right: { text: string; tone: SegmentTone; bold: boolean } | null;
}

// ---------------------------------------------------------------- busy indicator

/** Each style's frames share one display width, so the rule never jitters. */
export const INDICATOR_STYLES: Readonly<
  Record<string, { frames: readonly string[]; verb: boolean }>
> = {
  dots: { frames: ["⠋", "⠙", "⠸", "⠴", "⠦", "⠇"], verb: true },
  bar: { frames: ["▁", "▃", "▅", "▇", "▅", "▃"], verb: true },
  arc: { frames: ["◜", "◝", "◞", "◟"], verb: false },
  ascii: { frames: ["[=  ]", "[ = ]", "[  =]", "[ = ]"], verb: true },
};
export const DEFAULT_INDICATOR = "dots";

export const BUSY_VERBS = [
  "thinking",
  "working",
  "musing",
  "sorting",
  "shaping",
  "weighing",
] as const;
const VERB_WIDTH = Math.max(...BUSY_VERBS.map((v) => v.length)) + 1;

export function busyIndicator(style: string | undefined, frame = 0): string {
  const chosen =
    INDICATOR_STYLES[style ?? ""] ??
    (INDICATOR_STYLES[DEFAULT_INDICATOR] as (typeof INDICATOR_STYLES)[string]);
  const glyph = chosen.frames[frame % chosen.frames.length] as string;
  if (!chosen.verb) return glyph;
  // The verb changes about every 2.5 s at the usual 120 ms tick.
  const verb = BUSY_VERBS[Math.floor(frame / 20) % BUSY_VERBS.length] as string;
  return `${glyph} ${`${verb}…`.padEnd(VERB_WIDTH, " ")}`;
}

// ---------------------------------------------------------------- formatting

export function compactNumber(n: number): string {
  const abs = Math.abs(n);
  const fmt = (value: number, unit: string) =>
    `${value >= 100 ? Math.round(value) : Number(value.toFixed(1))}${unit}`;
  if (abs >= 1_000_000) return fmt(n / 1_000_000, "M");
  if (abs >= 1_000) return fmt(n / 1_000, "k");
  return String(Math.round(n));
}

export function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${total % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

const BAR_CELLS = 10;

export function contextBar(pct: number): string {
  const filled = Math.round((Math.max(0, Math.min(100, pct)) / 100) * BAR_CELLS);
  return "▰".repeat(filled) + "▱".repeat(BAR_CELLS - filled);
}

function pctTone(pct: number): SegmentTone {
  if (pct >= 95) return "error";
  if (pct > 80) return "warn";
  return "muted";
}

/** A folder label: home shown as `~`, long paths keep their last two parts. */
export function cwdLabel(cwd: string, home = ""): string {
  let label = cwd;
  if (home && (label === home || label.startsWith(`${home}/`)))
    label = `~${label.slice(home.length)}`;
  const parts = label.split("/").filter(Boolean);
  if (parts.length > 3) label = `…/${parts.slice(-2).join("/")}`;
  return label;
}

// ---------------------------------------------------------------- layout

const SEPARATOR = " │ ";
const LEAD = "─ ";
const RIGHT_GAP = " ─ ";

export function statusSegments(input: StatusInput): StatusLayout {
  const cols = Math.max(1, Math.floor(input.cols));
  const shown = (field: StatusField) => !input.fields || input.fields.has(field);
  const sep = cellWidth(SEPARATOR);

  // Essentials.
  const left: StatusSegment[] = [
    input.busy
      ? {
          id: "indicator",
          text: busyIndicator(input.indicatorStyle, input.frame ?? 0),
          tone: "accent",
        }
      : { id: "status", text: input.statusText || "ready", tone: "muted" },
  ];
  const model = input.reasoningEffort ? `${input.model} · ${input.reasoningEffort}` : input.model;
  if (model) left.push({ id: "model", text: model, tone: "text" });
  const pct =
    input.contextMax && input.contextMax > 0 && input.contextUsed !== undefined
      ? Math.min(100, Math.round((input.contextUsed / input.contextMax) * 100))
      : null;
  const narrow = cols < BREAKPOINTS.bar;
  const contextParts: string[] = [];
  if (pct !== null) {
    if (!narrow && shown("context_detail")) {
      contextParts.push(
        `${compactNumber(input.contextUsed ?? 0)}/${compactNumber(input.contextMax ?? 0)}`,
      );
    }
    if (shown("context_pct")) contextParts.push(`${pct}%`);
  } else if ((input.totalTokens ?? 0) > 0 && shown("context_detail")) {
    contextParts.push(`${compactNumber(input.totalTokens ?? 0)} tok`);
  }
  if (contextParts.length > 0) {
    left.push({
      id: "context",
      text: contextParts.join(" "),
      tone: pct !== null ? pctTone(pct) : "muted",
    });
  }
  if (input.yolo) left.push({ id: "yolo", text: "⚠ yolo", tone: "warn" });

  const essentials =
    cellWidth(LEAD) +
    left.reduce((sum, seg, i) => sum + (i > 0 ? sep : 0) + cellWidth(seg.text), 0);

  // Right label: title badge, else folder and branch. It yields before the essentials do.
  let right: StatusLayout["right"] = null;
  const titleText = input.title && shown("title") ? ` ${input.title} ` : "";
  const place = input.cwdLabel
    ? `${input.cwdLabel}${input.branch ? ` ⎇ ${input.branch}` : ""}`
    : "";
  const label = titleText || place;
  if (label) {
    const room = cols - essentials - cellWidth(RIGHT_GAP);
    if (room >= cellWidth(label))
      right = { text: label, tone: titleText ? "accent" : "muted", bold: !!titleText };
    else if (room >= 8)
      right = {
        text: `${label.slice(0, room - 1)}…`,
        tone: titleText ? "accent" : "muted",
        bold: !!titleText,
      };
  }

  let budget = cols - essentials - (right ? cellWidth(RIGHT_GAP) + cellWidth(right.text) : 0);
  const offer = (seg: StatusSegment | null, from: number) => {
    if (!seg || cols < from) return;
    const need = sep + cellWidth(seg.text);
    if (need > budget) return;
    budget -= need;
    left.push(seg);
  };

  offer(
    pct !== null && shown("context_pct")
      ? { id: "bar", text: contextBar(pct), tone: pctTone(pct) }
      : null,
    BREAKPOINTS.bar,
  );
  offer(
    input.durationMs !== undefined && shown("duration")
      ? { id: "duration", text: `◷ ${formatClock(input.durationMs)}`, tone: "muted" }
      : null,
    BREAKPOINTS.duration,
  );
  const squeezes = input.compressions ?? 0;
  offer(
    squeezes > 0 && shown("compressions")
      ? {
          id: "compressions",
          text: `⇊ ${squeezes}`,
          tone: squeezes >= 10 ? "error" : squeezes >= 5 ? "warn" : "muted",
        }
      : null,
    BREAKPOINTS.compressions,
  );
  offer(
    input.voice && shown("voice") ? { id: "voice", text: input.voice, tone: "muted" } : null,
    BREAKPOINTS.voice,
  );
  const bg = input.bgTasks ?? 0;
  offer(
    bg > 0 && shown("bg_tasks") ? { id: "bg_tasks", text: `bg ${bg}`, tone: "muted" } : null,
    BREAKPOINTS.bg_tasks,
  );
  const subs = input.subagents ?? 0;
  offer(
    subs > 0 && shown("bg_subagents")
      ? { id: "subagents", text: `⑂ ${subs}`, tone: "muted" }
      : null,
    BREAKPOINTS.subagents,
  );
  offer(
    input.cacheHitPct !== undefined && shown("cache_hit")
      ? {
          id: "cache_hit",
          text: `cache ${Math.round(input.cacheHitPct)}%`,
          tone: input.cacheHitPct >= 70 ? "ok" : input.cacheHitPct >= 40 ? "warn" : "muted",
        }
      : null,
    BREAKPOINTS.cache_hit,
  );
  offer(
    input.latencyS !== undefined && shown("latency")
      ? { id: "latency", text: `${input.latencyS.toFixed(1)}s/call`, tone: "muted" }
      : null,
    BREAKPOINTS.latency,
  );
  offer(
    input.tps !== undefined && shown("tps")
      ? { id: "tps", text: `${Math.round(input.tps)} tok/s`, tone: "muted" }
      : null,
    BREAKPOINTS.tps,
  );
  return { left, right };
}

// ---------------------------------------------------------------- from the stores

function num(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function str(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

export interface StatusExtras {
  cols: number;
  frame?: number;
  durationMs?: number;
  branch?: string;
  home?: string;
  yolo?: boolean;
  fields?: ReadonlySet<string> | null;
}

/** Builds the status input from uiStore's `info` (session.info) and `usage` records. */
export function statusFromStores(
  ui: {
    busy: boolean;
    statusText: string;
    info: Record<string, unknown> | null;
    usage: Record<string, unknown> | null;
    indicatorStyle: string;
  },
  extras: StatusExtras,
): StatusInput {
  const info = ui.info ?? {};
  const usage = ui.usage ?? {};
  const input: StatusInput = {
    cols: extras.cols,
    busy: ui.busy,
    statusText: ui.statusText,
    model: str(info.model) ?? "",
    indicatorStyle: ui.indicatorStyle,
  };
  const set = <K extends keyof StatusInput>(key: K, value: StatusInput[K] | undefined) => {
    if (value !== undefined) input[key] = value;
  };
  set("reasoningEffort", str(info.reasoning_effort));
  set("title", str(info.title));
  const cwd = str(info.cwd);
  set("cwdLabel", cwd ? cwdLabel(cwd, extras.home) : undefined);
  set("branch", extras.branch ?? str(info.branch));
  set("contextUsed", num(usage.context_tokens));
  set("contextMax", num(usage.context_window));
  const io = (num(usage.input_tokens) ?? 0) + (num(usage.output_tokens) ?? 0);
  set("totalTokens", io > 0 ? io : undefined);
  set("compressions", num(usage.compressions));
  set("bgTasks", num(usage.bg_tasks));
  set("subagents", num(usage.subagents));
  const read = num(usage.cache_read_tokens);
  const fresh = num(usage.input_tokens);
  set(
    "cacheHitPct",
    num(usage.cache_hit_pct) ??
      (read !== undefined && fresh !== undefined && read + fresh > 0
        ? (read / (read + fresh)) * 100
        : undefined),
  );
  set("latencyS", num(usage.avg_latency_s));
  set("tps", num(usage.avg_tps));
  set("durationMs", extras.durationMs);
  set("frame", extras.frame);
  set("yolo", extras.yolo ?? (info.yolo === true ? true : undefined));
  set("fields", extras.fields);
  return input;
}
