// `/wake on|off|status` (P24-20, J22): the terminal side of the wake word. `on` is an explicit
// gesture, so it sends `consent: true` (the daemon stores the person's agreement and turns the
// feature on before it opens the microphone); `off` stops listening and turns it off; `status`
// says what the listener is doing. A detection arrives as `wake.detected` and hands off to voice
// mode (P10). `wake.*` are not in the generated catalogue yet, hence the plain `rpc.call`.

import { errorText } from "../cards/rpcLike.ts";
import type { LocalCommand, RunContext } from "../slash/types.ts";

const REASONS: Record<string, string> = {
  disabled: "the wake word is off; /wake on turns it on",
  consent_required: "listening needs your agreement first: /wake on",
  owned: "another Indus window or terminal is listening already",
  unavailable: "it cannot run here",
};

interface StartAnswer {
  started?: boolean;
  reason?: string | null;
  hint?: string | null;
  phrase?: string;
}

interface StatusAnswer {
  enabled?: boolean;
  consent?: boolean;
  listening?: boolean;
  owner_surface?: string | null;
  phrase?: string;
  engine?: string;
  available?: boolean;
  hint?: string | null;
}

async function call<T>(ctx: RunContext, method: string, params: object): Promise<T | null> {
  try {
    const result = (await ctx.rpc.call(method, params)) as T;
    return ctx.stale() ? null : result;
  } catch (error) {
    if (!ctx.stale()) ctx.systemLine(`/wake failed: ${errorText(error)}`);
    return null;
  }
}

/** One line for a `wake.start` answer. */
export function startLine(answer: StartAnswer): string {
  if (answer.started === true) {
    return `Listening for “${answer.phrase ?? "hey indus"}”. The microphone is on until /wake off.`;
  }
  const why = REASONS[answer.reason ?? ""] ?? (answer.reason || "it did not start");
  return `The wake word is not listening: ${why}${answer.hint ? ` (${answer.hint})` : ""}.`;
}

export function statusText(answer: StatusAnswer): string {
  return [
    `wake word: ${answer.enabled ? "on" : "off"}${answer.consent ? "" : " (no consent given yet)"}`,
    `listening here: ${answer.listening ? "yes" : "no"}${answer.owner_surface ? ` · owner: ${answer.owner_surface}` : ""}`,
    `phrase: ${answer.phrase ?? "hey indus"} · engine: ${answer.engine ?? "?"}`,
    `ready: ${answer.available ? "yes" : `no${answer.hint ? ` — ${answer.hint}` : ""}`}`,
  ].join("\n");
}

export const WAKE_COMMAND: LocalCommand = {
  name: "wake",
  summary: "the wake word: on, off or status",
  args: "[on|off|status]",
  run: async (arg, ctx) => {
    const word = arg.trim().toLowerCase() || "status";
    if (word === "on") {
      ctx.systemLine("Starting the listener (the first start can take a few minutes)…");
      const answer = await call<StartAnswer>(ctx, "wake.start", { surface: "tui", consent: true });
      if (answer !== null) ctx.systemLine(startLine(answer));
      return;
    }
    if (word === "off") {
      const answer = await call<{ ok?: boolean }>(ctx, "wake.stop", { persist: true });
      if (answer !== null) ctx.systemLine("The wake word is off; the microphone is closed.");
      return;
    }
    if (word === "status") {
      const answer = await call<StatusAnswer>(ctx, "wake.status", {});
      if (answer !== null) ctx.show(statusText(answer), "Wake word");
      return;
    }
    ctx.systemLine("Use /wake on, /wake off or /wake status.");
  },
};
