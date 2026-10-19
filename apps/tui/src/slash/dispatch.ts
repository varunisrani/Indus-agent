// Slash dispatch: renderer registry → catalogue aliases and one best name match → `slash.exec` →
// `command.dispatch` (only after a "not owned" refusal). Late results are dropped (R05.10); a crash
// or timeout is reported as itself (R05.12).

import { GatewayRpcError } from "@indus/client";
import type { CatalogEntry, CommandDispatchResult, SlashExecResult } from "@indus/protocol";
import { isLongOutput } from "../cards/Pager.tsx";
import { errorText } from "../cards/rpcLike.ts";
import { captureDestination, isCurrent } from "../state/destination.ts";
import { LOCAL_COMMANDS } from "./localCommands.ts";
import type { LocalCommand, RunContext, SlashContext } from "./types.ts";

export const MAX_ALIAS_HOPS = 4;
export const AMBIGUOUS_LIMIT = 6;

export interface ParsedCommand {
  name: string;
  arg: string;
}

export function parseCommand(line: string): ParsedCommand {
  const body = line.trim().replace(/^\//, "");
  const space = body.search(/\s/);
  if (space < 0) return { name: body, arg: "" };
  return { name: body.slice(0, space), arg: body.slice(space + 1).trim() };
}

export function findLocal(
  name: string,
  registry: readonly LocalCommand[],
): LocalCommand | undefined {
  const wanted = name.toLowerCase();
  return registry.find(
    (command) =>
      command.name.toLowerCase() === wanted ||
      (command.aliases ?? []).some((alias) => alias.toLowerCase() === wanted),
  );
}

export type CatalogMatch =
  | { type: "exact"; canonical: string }
  | { type: "match"; canonical: string }
  | { type: "ambiguous"; names: string[] }
  | { type: "none" };

/** Resolve a typed word against the catalogue's names and aliases (descriptions never count). */
export function matchCatalog(name: string, catalog: readonly CatalogEntry[]): CatalogMatch {
  const wanted = name.toLowerCase();
  const keys: { key: string; canonical: string }[] = [];
  for (const entry of catalog) {
    for (const key of [entry.name, ...(entry.aliases ?? [])]) {
      keys.push({ key: key.toLowerCase().replace(/^\//, ""), canonical: entry.name });
    }
  }
  const exact = keys.find((k) => k.key === wanted);
  if (exact) return { type: "exact", canonical: exact.canonical };
  if (wanted === "") return { type: "none" };
  for (const test of [(k: string) => k.startsWith(wanted), (k: string) => k.includes(wanted)]) {
    const names = [...new Set(keys.filter((k) => test(k.key)).map((k) => k.canonical))];
    if (names.length === 1) return { type: "match", canonical: names[0] as string };
    if (names.length > 1) return { type: "ambiguous", names };
  }
  return { type: "none" };
}

const codeName = (error: unknown): string | undefined =>
  error instanceof GatewayRpcError ? error.errorName : undefined;

export interface Dispatcher {
  /** Run one typed line (`/name args`). */
  run(line: string): Promise<void>;
}

export function createDispatcher(
  ctx: SlashContext,
  registry: readonly LocalCommand[] = LOCAL_COMMANDS,
): Dispatcher {
  let flight = 0;

  const run = async (line: string): Promise<void> => {
    flight += 1;
    const mine = flight;
    const destination = captureDestination();
    const stale = (): boolean => mine !== flight || !isCurrent(destination);
    await route(parseCommand(line), line.trim(), stale, 0);
  };

  const show = (stale: () => boolean) => (text: string, title: string) => {
    if (stale()) return;
    if (isLongOutput(text)) ctx.page(text, title);
    else ctx.systemLine(text);
  };

  async function route(
    parsed: ParsedCommand,
    typed: string,
    stale: () => boolean,
    hops: number,
  ): Promise<void> {
    const { name, arg } = parsed;
    if (name === "") {
      ctx.systemLine("Type a command after the slash; /help lists them.");
      return;
    }
    const again = (target: string): Promise<void> => {
      if (hops >= MAX_ALIAS_HOPS) {
        ctx.systemLine(`/${name}: too many alias steps, stopped.`);
        return Promise.resolve();
      }
      const next = { name: target.replace(/^\//, ""), arg };
      return route(next, `/${next.name}${arg ? ` ${arg}` : ""}`, stale, hops + 1);
    };

    const local = findLocal(name, registry);
    if (local) {
      const runCtx: RunContext = {
        ...ctx,
        sid: ctx.ui.getState().sid,
        stale,
        forward: (n, a) => forward(n, a, stale, hops),
        show: show(stale),
      };
      try {
        await local.run(arg, runCtx, typed);
      } catch (error) {
        if (!stale()) ctx.systemLine(`/${local.name} failed: ${errorText(error)}`);
      }
      return;
    }

    const catalog = ctx.catalog();
    if (catalog && catalog.length > 0) {
      const found = matchCatalog(name, catalog);
      if (found.type === "ambiguous") {
        const listed = found.names.slice(0, AMBIGUOUS_LIMIT).map((n) => `/${n}`);
        const more = found.names.length > AMBIGUOUS_LIMIT ? ", …" : "";
        ctx.systemLine(`/${name} is ambiguous: ${listed.join(", ")}${more}`);
        return;
      }
      if (
        (found.type === "exact" || found.type === "match") &&
        found.canonical.toLowerCase() !== name.toLowerCase()
      ) {
        await again(found.canonical);
        return;
      }
    }
    await forward(name, arg, stale, hops);
  }

  async function forward(
    name: string,
    arg: string,
    stale: () => boolean,
    hops: number,
  ): Promise<void> {
    const sid = ctx.ui.getState().sid;
    if (sid === null) {
      ctx.systemLine(`/${name} needs a session: send a message first.`);
      return;
    }
    const command = arg ? `${name} ${arg}` : name;
    let result: unknown;
    try {
      result = await ctx.rpc.call("slash.exec", { session_id: sid, command });
    } catch (error) {
      if (stale()) return;
      const code = codeName(error);
      if (code === "SLASH_NOT_OWNED") {
        await dispatch(name, arg, sid, stale, hops, errorText(error));
        return;
      }
      if (code === "UNSUPPORTED_COMMAND") {
        ctx.systemLine(`Unknown command: /${name} (/help lists them).`);
        return;
      }
      ctx.systemLine(`/${name} failed: ${errorText(error)}`);
      return;
    }
    if (stale()) return;
    const exec = (result ?? {}) as SlashExecResult & Partial<CommandDispatchResult>;
    if (typeof exec.kind === "string") {
      await settleDispatch(exec as CommandDispatchResult, name, arg, stale, hops);
      return;
    }
    let acted = false;
    if (exec.output) {
      show(stale)(exec.output, titleOf(name));
      acted = true;
    }
    if (exec.prefill) {
      ctx.prefill(exec.prefill);
      acted = true;
    }
    if (exec.submit) {
      ctx.submit(exec.submit);
      acted = true;
    }
    if (exec.switch_to) {
      if (ctx.switchSession) ctx.switchSession(exec.switch_to);
      else ctx.systemLine(`/${name} asked to switch to session ${exec.switch_to}.`);
      acted = true;
    }
    if (!acted) ctx.systemLine(`/${name}: done (no output).`);
  }

  async function dispatch(
    name: string,
    arg: string,
    sid: string,
    stale: () => boolean,
    hops: number,
    refusal: string,
  ): Promise<void> {
    let result: CommandDispatchResult;
    try {
      result = (await ctx.rpc.call("command.dispatch", {
        session_id: sid,
        name,
        arg,
      })) as CommandDispatchResult;
    } catch (error) {
      if (stale()) return;
      // The bridge said "not mine" and nothing else claims it: the first refusal says why.
      if (codeName(error) === "UNSUPPORTED_COMMAND") ctx.systemLine(`/${name}: ${refusal}`);
      else ctx.systemLine(`/${name} failed: ${errorText(error)}`);
      return;
    }
    if (stale()) return;
    await settleDispatch(result, name, arg, stale, hops);
  }

  async function settleDispatch(
    raw: CommandDispatchResult,
    name: string,
    arg: string,
    stale: () => boolean,
    hops: number,
  ): Promise<void> {
    const result = raw as CommandDispatchResult & { display?: unknown; notice?: unknown };
    const shown =
      typeof result.display === "string" && result.display.trim()
        ? result.display.trim()
        : `/${name}${arg ? ` ${arg}` : ""}`;
    const notice = typeof result.notice === "string" ? result.notice.trim() : "";
    const message = result.message ?? "";
    switch (result.kind) {
      case "exec":
      case "plugin":
        show(stale)(result.output || `/${name}: done (no output).`, titleOf(name));
        return;
      case "alias":
        if (!result.target) {
          ctx.systemLine(`/${name}: the alias has no target.`);
          return;
        }
        if (hops >= MAX_ALIAS_HOPS) {
          ctx.systemLine(`/${name}: too many alias steps, stopped.`);
          return;
        }
        await route(
          { name: result.target.replace(/^\//, ""), arg },
          `/${result.target}`,
          stale,
          hops + 1,
        );
        return;
      case "skill":
        if (message.trim()) ctx.submit(message, shown);
        else ctx.systemLine(`/${name}: the skill gave nothing to send.`);
        return;
      case "send":
        if (notice) ctx.systemLine(notice);
        if (message.trim()) ctx.submit(message, shown);
        else ctx.systemLine(`/${name}: nothing to send.`);
        return;
      case "prefill":
        if (notice) ctx.systemLine(notice);
        if (message) ctx.prefill(message);
        return;
      default:
        ctx.systemLine(`/${name}: the bridge answered with an unknown result.`);
    }
  }

  return { run };
}

const titleOf = (name: string): string => `/${name}`;
