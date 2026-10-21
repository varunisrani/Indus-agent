// `/pet` (P24-19, J23): the pet's card in a pane — its name, its moods and a small figure drawn in
// text (our own; the pictures themselves are shown by the desktop overlay). `/pet list`, `/pet use
// <slug>` and `/pet off` choose the shown pet; new pets are made with `indus pet generate`.
// `pet.*` are not in the generated catalogue yet, hence the plain `rpc.call`.

import { errorText } from "../cards/rpcLike.ts";
import type { LocalCommand, RunContext } from "../slash/types.ts";

interface PetInfo {
  enabled?: boolean;
  name?: string;
  slug?: string;
  scale?: number;
  moods?: Record<string, string>;
}

interface PetList {
  pets?: { slug: string; name: string; shown: boolean }[];
}

const FIGURE = [" .-^-^-. ", "( o   o )", " \\  ~  / ", "  '---'  "];

async function call<T>(ctx: RunContext, method: string, params: object): Promise<T | null> {
  try {
    const result = (await ctx.rpc.call(method, params)) as T;
    return ctx.stale() ? null : result;
  } catch (error) {
    if (!ctx.stale()) ctx.systemLine(`/pet failed: ${errorText(error)}`);
    return null;
  }
}

/** The pane text for a `pet.info` answer. */
export function petCard(info: PetInfo): string {
  if (info.enabled !== true) {
    return [
      "No pet is shown.",
      "",
      'Make one with `indus pet generate "a small sleepy otter"`, then `indus pet hatch`,',
      "or pick one with /pet list and /pet use <slug>.",
    ].join("\n");
  }
  const moods = Object.keys(info.moods ?? {}).sort();
  return [
    ...FIGURE,
    "",
    `${info.name ?? info.slug ?? "pet"} (${info.slug ?? "?"})`,
    `moods: ${moods.length > 0 ? moods.join(", ") : "none"}`,
    `size: ${info.scale ?? 1}`,
  ].join("\n");
}

export const PET_COMMAND: LocalCommand = {
  name: "pet",
  summary: "show the pet, or list, use or turn off pets",
  args: "[list|use <slug>|off]",
  run: async (arg, ctx) => {
    const [word = "", ...rest] = arg.trim().split(/\s+/);
    const action = word.toLowerCase();
    if (action === "" || action === "show") {
      const info = await call<PetInfo>(ctx, "pet.info", {});
      if (info !== null) ctx.page(petCard(info), "Pet");
      return;
    }
    if (action === "list") {
      const list = await call<PetList>(ctx, "pet.list", {});
      if (list === null) return;
      const rows = (list.pets ?? []).map((p) => `${p.shown ? "*" : " "} ${p.slug}  ${p.name}`);
      ctx.show(rows.length > 0 ? rows.join("\n") : "No pets yet.", "Pets");
      return;
    }
    if (action === "use") {
      const slug = rest.join(" ").trim();
      if (slug === "") {
        ctx.systemLine("Use /pet use <slug> (see /pet list).");
        return;
      }
      const chosen = await call<{ name?: string }>(ctx, "pet.select", { slug });
      if (chosen !== null) ctx.systemLine(`${chosen.name ?? slug} is shown now.`);
      return;
    }
    if (action === "off") {
      if ((await call(ctx, "pet.disable", {})) !== null) ctx.systemLine("The pet is hidden.");
      return;
    }
    ctx.systemLine("Use /pet, /pet list, /pet use <slug> or /pet off.");
  },
};
