// Commands the renderer runs itself. Each needs either client state (stores, overlay, composer)
// or a direct bridge call; the rest of the catalogue goes to the bridge through dispatch.

import { writeFile } from "node:fs/promises";
import type {
  ConfigGetResult,
  ConfigSetResult,
  ImageAttachResult,
  PromptBackgroundResult,
  ReloadMcpResult,
  SessionBranchResult,
  SessionCompressResult,
  SessionHistoryResult,
  SessionSteerResult,
  SessionTitleResult,
  SessionUndoResult,
  SessionUsageResult,
} from "@indus/protocol";
import type { ModelPickerPayload } from "../cards/payloads.ts";
import { errorText } from "../cards/rpcLike.ts";
import { PET_COMMAND } from "../commands/pet.ts";
import { WAKE_COMMAND } from "../commands/wake.ts";
import { applyModelChoice } from "../pickers/modelPickerState.ts";
import { steerParams } from "../state/turnStore.ts";
import type { UiState } from "../state/uiStore.ts";
import type { LocalCommand, RunContext } from "./types.ts";

/** Exit code that asks the launcher to run the updater. */
export const UPDATE_EXIT_CODE = 42;
export const UPDATE_DELAY_MS = 100;
export const NO_CONFIRM_ENV = "INDUS_TUI_NO_CONFIRM";

const DETAILS_CYCLE: readonly UiState["detailsMode"][] = ["collapsed", "expanded", "hidden"];
const BUSY_MODES: readonly UiState["busyInputMode"][] = ["queue", "steer", "interrupt"];
const VERBOSE_CYCLE = ["off", "new", "all", "verbose"] as const;
const MOUSE_PRESETS = ["off", "wheel", "buttons", "all"] as const;

let pickerSerial = 0;

function needSession(ctx: RunContext, name: string): string | null {
  if (ctx.sid === null) ctx.systemLine(`/${name} needs a session: send a message first.`);
  return ctx.sid;
}

function unavailable(ctx: RunContext, name: string): void {
  ctx.systemLine(`/${name} is not available in this build yet.`);
}

async function call<T>(
  ctx: RunContext,
  name: string,
  method: string,
  params: object,
): Promise<T | null> {
  try {
    const result = (await ctx.rpc.call(method, params)) as T;
    return ctx.stale() ? null : result;
  } catch (error) {
    if (!ctx.stale()) ctx.systemLine(`/${name} failed: ${errorText(error)}`);
    return null;
  }
}

const setting = (
  ctx: RunContext,
  name: string,
  key: string,
  value: unknown,
  withSession: boolean,
) =>
  call<ConfigSetResult>(ctx, name, "config.set", {
    key,
    value,
    ...(withSession && ctx.sid !== null ? { session_id: ctx.sid } : {}),
  });

const reading = (ctx: RunContext, name: string, key: string) =>
  call<ConfigGetResult>(ctx, name, "config.get", {
    key,
    ...(ctx.sid !== null ? { session_id: ctx.sid } : {}),
  });

const flag = (arg: string): boolean | null => {
  const word = arg.trim().toLowerCase();
  if (["on", "true", "yes", "1"].includes(word)) return true;
  if (["off", "false", "no", "0"].includes(word)) return false;
  return null;
};

function describe(value: unknown): string {
  if (value === null || value === undefined) return "not set";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

async function startFresh(arg: string, ctx: RunContext, typed: string): Promise<void> {
  const isNew = /^\/?new\b/i.test(typed.trim());
  if (ctx.ui.getState().busy) {
    ctx.systemLine("A reply is still running: stop the turn first (Esc or /stop).");
    return;
  }
  if (!ctx.newSession) {
    unavailable(ctx, isNew ? "new" : "clear");
    return;
  }
  const title = isNew && arg.trim() ? arg.trim() : undefined;
  if (!ctx.env[NO_CONFIRM_ENV]) {
    const yes = await ctx.confirm({
      title: isNew ? "Start a new session?" : "Clear the screen and start over?",
      detail: "The current conversation stays saved; you can resume it from /sessions.",
      danger: true,
      confirmLabel: isNew ? "start the new session" : "clear",
      cancelLabel: "stay here",
    });
    if (!yes || ctx.stale()) return;
  }
  ctx.newSession(title);
}

function helpText(ctx: RunContext): string {
  const lines = ["Commands run here:"];
  for (const command of LOCAL_COMMANDS) {
    const names = [command.name, ...(command.aliases ?? [])].map((n) => `/${n}`).join(", ");
    lines.push(`  ${names}${command.args ? ` ${command.args}` : ""} — ${command.summary}`);
  }
  const local = new Set(LOCAL_COMMANDS.flatMap((c) => [c.name, ...(c.aliases ?? [])]));
  // `unavailable`: the server does not run it on this connection (W48-h), so it is not offered.
  const remote = (ctx.catalog() ?? []).filter(
    (entry) => !local.has(entry.name) && entry.runs !== "unavailable",
  );
  if (remote.length > 0) {
    lines.push("", "Commands the agent runs:");
    for (const entry of remote) {
      lines.push(
        `  /${entry.name}${entry.args_hint ? ` ${entry.args_hint}` : ""} — ${entry.description}`,
      );
    }
  }
  return lines.join("\n");
}

function sessionStatus(ctx: RunContext): string {
  const ui = ctx.ui.getState();
  const info = ui.info ?? {};
  const field = (key: string): string => describe(info[key]);
  return [
    `session: ${ui.sid ?? "none yet"}`,
    `title: ${field("title")}`,
    `model: ${field("model")}`,
    `state: ${ui.busy ? "replying" : "idle"}${ui.statusText ? ` (${ui.statusText})` : ""}`,
    `busy input: ${ui.busyInputMode} · details: ${ui.detailsMode} · status bar: ${ui.statusBar}`,
  ].join("\n");
}

const stamp = (date: Date): string => {
  const two = (n: number): string => String(n).padStart(2, "0");
  return `${date.getFullYear()}${two(date.getMonth() + 1)}${two(date.getDate())}-${two(date.getHours())}${two(date.getMinutes())}${two(date.getSeconds())}`;
};

export const LOCAL_COMMANDS: readonly LocalCommand[] = [
  {
    name: "help",
    summary: "list every command",
    run: (_arg, ctx) => ctx.page(helpText(ctx), "Commands"),
  },
  {
    name: "quit",
    aliases: ["exit"],
    summary: "leave Indus Agent",
    run: (_arg, ctx) => ctx.exit(0),
  },
  {
    name: "update",
    summary: "close the TUI and update Indus Agent",
    run: (_arg, ctx) => {
      ctx.systemLine("Closing to update…");
      ctx.later(() => ctx.exit(UPDATE_EXIT_CODE), UPDATE_DELAY_MS);
    },
  },
  {
    name: "clear",
    aliases: ["new"],
    summary: "start a fresh session (/new takes a title)",
    args: "[title]",
    run: startFresh,
  },
  {
    name: "redraw",
    summary: "repaint the whole screen",
    run: (_arg, ctx) => (ctx.redraw ? ctx.redraw() : unavailable(ctx, "redraw")),
  },
  {
    name: "status",
    summary: "what this session is doing",
    run: (_arg, ctx) => ctx.show(sessionStatus(ctx), "Status"),
  },
  {
    name: "title",
    summary: "show or set the session title",
    args: "[text]",
    run: async (arg, ctx) => {
      const sid = needSession(ctx, "title");
      if (sid === null) return;
      const params = arg ? { session_id: sid, title: arg } : { session_id: sid };
      const result = await call<SessionTitleResult>(ctx, "title", "session.title", params);
      if (result === null) return;
      const title = result.title ?? "(none)";
      ctx.systemLine(
        arg
          ? `Title set to ${title}${result.pending ? " (saved after the first reply)" : ""}.`
          : `Title: ${title}`,
      );
    },
  },
  {
    name: "details",
    summary: "tool and reasoning sections: collapsed, expanded or hidden",
    args: "[collapsed|expanded|hidden|cycle]",
    run: async (arg, ctx) => {
      const word = arg.trim().toLowerCase();
      const now = ctx.ui.getState().detailsMode;
      let next: UiState["detailsMode"];
      if (word === "" || word === "cycle") {
        next =
          DETAILS_CYCLE[(DETAILS_CYCLE.indexOf(now) + 1) % DETAILS_CYCLE.length] ?? "collapsed";
      } else if ((DETAILS_CYCLE as readonly string[]).includes(word)) {
        next = word as UiState["detailsMode"];
      } else {
        ctx.systemLine("Use /details collapsed, expanded, hidden or cycle.");
        return;
      }
      ctx.ui.getState().patch({ detailsMode: next });
      ctx.systemLine(`Details: ${next}.`);
      // The settings file knows collapsed and expanded; hidden lasts for this run only.
      if (next !== "hidden") await setting(ctx, "details", "details", next, false);
    },
  },
  {
    name: "copy",
    summary: "copy the selection or the last reply",
    args: "[n]",
    run: (arg, ctx) => (ctx.copy ? ctx.copy(arg) : unavailable(ctx, "copy")),
  },
  {
    name: "paste",
    summary: "attach an image from the clipboard",
    run: (_arg, ctx) => (ctx.paste ? ctx.paste() : unavailable(ctx, "paste")),
  },
  {
    name: "prompt",
    summary: "write the next message in your editor",
    args: "[text]",
    run: (arg, ctx) => (ctx.openEditor ? ctx.openEditor(arg) : unavailable(ctx, "prompt")),
  },
  {
    name: "logs",
    summary: "recent bridge log lines",
    run: (_arg, ctx) => {
      if (!ctx.logLines) return unavailable(ctx, "logs");
      const lines = ctx.logLines();
      if (lines.length === 0) ctx.systemLine("No log lines yet.");
      else ctx.page(lines.join("\n"), "Logs");
    },
  },
  {
    name: "history",
    summary: "page through this conversation",
    run: (_arg, ctx) => {
      if (!ctx.transcriptText) return unavailable(ctx, "history");
      const text = ctx.transcriptText();
      if (text.trim() === "") ctx.systemLine("Nothing said yet.");
      else ctx.page(text, "Conversation");
    },
  },
  {
    name: "save",
    summary: "write this conversation to a JSON file",
    args: "[path]",
    run: async (arg, ctx) => {
      const sid = needSession(ctx, "save");
      if (sid === null) return;
      const result = await call<SessionHistoryResult>(ctx, "save", "session.history", {
        session_id: sid,
      });
      if (result === null) return;
      const path = arg.trim() || `indus-session-${stamp(new Date())}.json`;
      const text = `${JSON.stringify({ session_id: sid, messages: result.messages }, null, 2)}\n`;
      try {
        await (ctx.writeFile ?? ((p: string, t: string) => writeFile(p, t, { mode: 0o600 })))(
          path,
          text,
        );
        ctx.systemLine(`Saved ${result.messages.length} messages to ${path}.`);
      } catch (error) {
        ctx.systemLine(`/save failed: ${errorText(error)}`);
      }
    },
  },
  {
    name: "statusbar",
    summary: "status bar on top, at the bottom, or off",
    args: "[top|bottom|off|on|toggle]",
    run: async (arg, ctx) => {
      const word = arg.trim().toLowerCase();
      const now = ctx.ui.getState().statusBar;
      const next: UiState["statusBar"] | null =
        word === "" || word === "toggle"
          ? now === "off"
            ? "top"
            : "off"
          : word === "on"
            ? "top"
            : word === "top" || word === "bottom" || word === "off"
              ? word
              : null;
      if (next === null) {
        ctx.systemLine("Use /statusbar top, bottom, off, on or toggle.");
        return;
      }
      ctx.ui.getState().patch({ statusBar: next });
      ctx.systemLine(`Status bar: ${next}.`);
      await setting(ctx, "statusbar", "statusbar", next !== "off", false);
    },
  },
  {
    name: "queue",
    summary: "show the queue, or add a message to it",
    args: "[text]",
    run: (arg, ctx) => {
      if (arg) {
        if (!ctx.enqueue) return unavailable(ctx, "queue");
        ctx.enqueue(arg);
        ctx.systemLine("Queued for after the current reply.");
        return;
      }
      const waiting = ctx.queued?.() ?? [];
      ctx.systemLine(
        waiting.length === 0
          ? "Nothing queued."
          : [`${waiting.length} queued:`, ...waiting.map((t, i) => `  ${i + 1}. ${t}`)].join("\n"),
      );
    },
  },
  {
    name: "steer",
    summary: "add guidance to the running reply without stopping it",
    args: "<text>",
    run: async (arg, ctx) => {
      if (!arg) {
        ctx.systemLine("Usage: /steer <text>");
        return;
      }
      const sid = needSession(ctx, "steer");
      if (sid === null) return;
      const result = await call<SessionSteerResult>(
        ctx,
        "steer",
        "session.steer",
        steerParams(sid, arg),
      );
      if (result === null) return;
      if (result.queued) ctx.systemLine("Steer note handed to the running reply.");
      else {
        ctx.systemLine("Nothing is running, so it goes out as a message.");
        ctx.submit(arg);
      }
    },
  },
  {
    name: "undo",
    summary: "take back the last exchange (its text returns to the composer)",
    run: async (_arg, ctx) => {
      const sid = needSession(ctx, "undo");
      if (sid === null) return;
      const result = await call<SessionUndoResult>(ctx, "undo", "session.undo", {
        session_id: sid,
        count: 1,
      });
      if (result === null) return;
      if (result.removed === 0) {
        ctx.systemLine("Nothing to undo.");
        return;
      }
      ctx.systemLine(`Removed ${result.removed} message${result.removed === 1 ? "" : "s"}.`);
      if (result.prefill) ctx.prefill(result.prefill);
    },
  },
  {
    name: "retry",
    summary: "send the last message again",
    run: async (_arg, ctx) => {
      const sid = needSession(ctx, "retry");
      if (sid === null) return;
      const result = await call<SessionUndoResult>(ctx, "retry", "session.undo", {
        session_id: sid,
        count: 1,
      });
      if (result === null) return;
      if (!result.prefill) {
        ctx.systemLine("Nothing to retry.");
        return;
      }
      ctx.submit(result.prefill);
    },
  },
  {
    name: "bg",
    summary: "run a prompt in a background session",
    args: "<text>",
    run: async (arg, ctx) => {
      if (!arg) return ctx.systemLine("Usage: /bg <text>");
      const sid = needSession(ctx, "bg");
      if (sid === null) return;
      const result = await call<PromptBackgroundResult>(ctx, "bg", "prompt.background", {
        session_id: sid,
        text: arg,
      });
      if (result)
        ctx.systemLine(`Running in the background (${result.task_id}); the reply arrives here.`);
    },
  },
  {
    name: "btw",
    summary: "ask a side question without disturbing this conversation",
    args: "<question>",
    run: async (arg, ctx) => {
      if (!arg) return ctx.systemLine("Usage: /btw <question>");
      const sid = needSession(ctx, "btw");
      if (sid === null) return;
      const result = await call<PromptBackgroundResult>(ctx, "btw", "prompt.background", {
        session_id: sid,
        text: arg,
      });
      if (result) ctx.systemLine(`Side question sent (${result.task_id}).`);
    },
  },
  {
    name: "model",
    summary: "pick or switch the model (/model --refresh reloads the list)",
    args: "[provider:model | --refresh]",
    run: async (arg, ctx) => {
      const word = arg.trim();
      if (word === "" || word === "--refresh") {
        pickerSerial += 1;
        const payload: ModelPickerPayload = { sessionId: ctx.sid, refresh: word === "--refresh" };
        ctx.overlay.getState().open({
          id: `model-picker:${pickerSerial}`,
          kind: "modelPicker",
          payload,
          ownsInput: true,
        });
        return;
      }
      await applyModelChoice(
        ctx.rpc,
        { sessionId: ctx.sid, provider: "", model: word },
        { confirm: ctx.confirm, systemLine: (t) => !ctx.stale() && ctx.systemLine(t) },
      );
    },
  },
  {
    name: "sessions",
    aliases: ["switch", "session", "resume"],
    summary: "browse, switch or resume sessions",
    run: (arg, ctx) => {
      // `/sessions new [title]` starts a session right away (the current one stays live).
      const words = arg.trim().split(/\s+/);
      if (words[0]?.toLowerCase() === "new" && ctx.newLiveSession) {
        return ctx.newLiveSession(words.slice(1).join(" ") || undefined);
      }
      return ctx.openSessions ? ctx.openSessions() : unavailable(ctx, "sessions");
    },
  },
  {
    name: "image",
    summary: "attach an image file to the next message",
    args: "<path>",
    run: async (arg, ctx) => {
      if (!arg) return ctx.systemLine("Usage: /image <path>");
      const sid = needSession(ctx, "image");
      if (sid === null) return;
      const result = await call<ImageAttachResult>(ctx, "image", "image.attach", {
        session_id: sid,
        path: arg,
      });
      if (result)
        ctx.systemLine(
          `Attached ${result.name}; ${result.attached} image(s) go with your next message.`,
        );
    },
  },
  {
    name: "compress",
    summary: "summarise older turns to free context",
    args: "[focus]",
    run: async (arg, ctx) => {
      const sid = needSession(ctx, "compress");
      if (sid === null) return;
      const params = arg ? { session_id: sid, focus: arg } : { session_id: sid };
      const result = await call<SessionCompressResult>(ctx, "compress", "session.compress", params);
      if (result === null) return;
      const said: Record<SessionCompressResult["status"], string> = {
        compressed: "Older turns were summarised.",
        nothing: "Nothing to compress yet.",
        blocked: "Compression is not possible right now.",
        aborted: "Compression stopped.",
        preview: "Compression preview ready.",
      };
      ctx.systemLine(
        result.message ? `${said[result.status]} ${result.message}` : said[result.status],
      );
    },
  },
  {
    name: "branch",
    summary: "copy this conversation into a new session",
    args: "[title]",
    run: async (arg, ctx) => {
      const sid = needSession(ctx, "branch");
      if (sid === null) return;
      const params = arg ? { session_id: sid, title: arg } : { session_id: sid };
      const result = await call<SessionBranchResult>(ctx, "branch", "session.branch", params);
      if (result === null) return;
      ctx.systemLine(`Branched into ${result.title ?? result.session_id}.`);
      ctx.switchSession?.(result.session_id);
    },
  },
  {
    name: "theme",
    aliases: ["skin"],
    summary: "show or change the colour theme",
    args: "[name]",
    run: async (arg, ctx) => {
      const name = arg.trim();
      if (!name) {
        ctx.systemLine(`Theme: ${ctx.ui.getState().themeName}.`);
        return;
      }
      const result = await setting(ctx, "theme", "theme", name, false);
      if (result === null) return;
      ctx.ui.getState().patch({ themeName: String(result.value ?? name) });
      ctx.systemLine(`Theme: ${String(result.value ?? name)}.`);
    },
  },
  {
    name: "yolo",
    summary: "approve every command in this session without asking",
    args: "[on|off]",
    run: async (arg, ctx) => {
      if (needSession(ctx, "yolo") === null) return;
      let next = flag(arg);
      if (next === null) {
        if (arg.trim() && arg.trim() !== "toggle")
          return ctx.systemLine("Use /yolo on, off or toggle.");
        const now = await reading(ctx, "yolo", "yolo");
        if (now === null) return;
        next = now.value !== true;
      }
      const result = await setting(ctx, "yolo", "yolo", next, true);
      if (result) {
        ctx.systemLine(
          next
            ? "Approvals off: commands run without asking in this session."
            : "Approvals back on.",
        );
      }
    },
  },
  {
    name: "reasoning",
    summary: "set the reasoning effort, or show/hide reasoning text",
    args: "[level|show|hide]",
    run: async (arg, ctx) => {
      const word = arg.trim().toLowerCase();
      if (word === "show" || word === "hide") {
        ctx.ui.getState().patch({ showReasoning: word === "show" });
        ctx.systemLine(word === "show" ? "Reasoning text shown." : "Reasoning text hidden.");
        return;
      }
      if (needSession(ctx, "reasoning") === null) return;
      if (word === "") {
        const now = await reading(ctx, "reasoning", "reasoning");
        if (now) ctx.systemLine(`Reasoning effort: ${describe(now.value)}.`);
        return;
      }
      const result = await setting(ctx, "reasoning", "reasoning", word, true);
      if (result) ctx.systemLine(`Reasoning effort: ${describe(result.value)}.`);
    },
  },
  {
    name: "fast",
    summary: "show or change fast mode",
    args: "[mode|on|off]",
    run: async (arg, ctx) => {
      if (needSession(ctx, "fast") === null) return;
      const word = arg.trim().toLowerCase();
      if (word === "" || word === "status") {
        const now = await reading(ctx, "fast", "fast");
        if (now) ctx.systemLine(`Fast mode: ${describe(now.value)}.`);
        return;
      }
      const result = await setting(ctx, "fast", "fast", word, true);
      if (result) ctx.systemLine(`Fast mode: ${describe(result.value)}.`);
    },
  },
  {
    name: "busy",
    summary: "what Enter does while a reply runs: queue, steer or interrupt",
    args: "[queue|steer|interrupt]",
    run: async (arg, ctx) => {
      const word = arg.trim().toLowerCase();
      if (word === "" || word === "status") {
        ctx.systemLine(`While busy, Enter will ${ctx.ui.getState().busyInputMode}.`);
        return;
      }
      if (!(BUSY_MODES as readonly string[]).includes(word)) {
        ctx.systemLine("Use /busy queue, steer or interrupt.");
        return;
      }
      ctx.ui.getState().patch({ busyInputMode: word as UiState["busyInputMode"] });
      ctx.systemLine(`While busy, Enter will ${word}.`);
      await setting(ctx, "busy", "busy", word, false);
    },
  },
  {
    name: "verbose",
    summary: "how much tool activity to show: off, new, all or verbose",
    args: "[off|new|all|verbose|cycle]",
    run: async (arg, ctx) => {
      let word = arg.trim().toLowerCase();
      if (word === "" || word === "cycle") {
        const now = await reading(ctx, "verbose", "verbose");
        if (now === null) return;
        const at = VERBOSE_CYCLE.indexOf(String(now.value) as (typeof VERBOSE_CYCLE)[number]);
        word = VERBOSE_CYCLE[(at + 1) % VERBOSE_CYCLE.length] ?? "all";
      } else if (!(VERBOSE_CYCLE as readonly string[]).includes(word)) {
        ctx.systemLine("Use /verbose off, new, all, verbose or cycle.");
        return;
      }
      const result = await setting(ctx, "verbose", "verbose", word, true);
      if (result) ctx.systemLine(`Tool activity: ${describe(result.value)}.`);
    },
  },
  {
    name: "usage",
    summary: "token use and cost of this session",
    run: async (_arg, ctx) => {
      const sid = needSession(ctx, "usage");
      if (sid === null) return;
      const result = await call<SessionUsageResult>(ctx, "usage", "session.usage", {
        session_id: sid,
      });
      if (result === null) return;
      const rows = Object.entries(result.usage ?? {}).map(
        ([key, value]) => `${key}: ${describe(value)}`,
      );
      ctx.show(rows.length > 0 ? rows.join("\n") : "No usage yet.", "Usage");
    },
  },
  {
    name: "stop",
    summary: "stop background processes of this session",
    run: (arg, ctx) => ctx.forward("stop", arg),
  },
  {
    name: "reload-mcp",
    summary: "reconnect MCP servers",
    run: async (_arg, ctx) => {
      const params = ctx.sid !== null ? { session_id: ctx.sid } : {};
      const result = await call<ReloadMcpResult>(ctx, "reload-mcp", "reload.mcp", params);
      if (result)
        ctx.systemLine(
          result.message || (result.reloaded ? "MCP servers reloaded." : "Nothing reloaded."),
        );
    },
  },
  {
    name: "reload",
    summary: "re-read the environment file",
    run: (arg, ctx) => ctx.forward("reload", arg),
  },
  {
    name: "tools",
    summary: "list or toggle tools",
    args: "[args]",
    run: (arg, ctx) => ctx.forward("tools", arg),
  },
  {
    name: "skills",
    summary: "browse and manage skills",
    args: "[args]",
    run: (arg, ctx) => ctx.forward("skills", arg),
  },
  {
    name: "mouse",
    summary: "mouse capture: on, off, toggle, wheel, buttons or all",
    args: "[on|off|toggle|wheel|buttons|all]",
    run: async (arg, ctx) => {
      const word = arg.trim().toLowerCase();
      const now = ctx.ui.getState().mousePreset;
      const next =
        word === "" || word === "toggle"
          ? now === "off"
            ? "all"
            : "off"
          : word === "on"
            ? "all"
            : (MOUSE_PRESETS as readonly string[]).includes(word)
              ? word
              : null;
      if (next === null) {
        ctx.systemLine("Use /mouse on, off, toggle, wheel, buttons or all.");
        return;
      }
      ctx.ui.getState().patch({ mousePreset: next });
      ctx.systemLine(`Mouse: ${next}.`);
      await setting(ctx, "mouse", "mouse", next !== "off", false);
    },
  },
  WAKE_COMMAND, // P24-20
  PET_COMMAND, // P24-19
];
