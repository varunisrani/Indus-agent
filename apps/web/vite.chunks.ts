// Build chunking and the chunk size budget (P18-29). React and the router load with the shell,
// xterm only with the chat tab, everything else from node_modules in one shared vendor chunk;
// pages are lazy chunks of their own (src/shell/routes.ts).

import type { Plugin } from "vite";

/** Rollup's warning threshold and our budget, in kB of emitted JavaScript (1 kB = 1,000 bytes). */
export const CHUNK_LIMIT_KB = 600;

export function chunkFor(id: string): string | undefined {
  if (!id.includes("node_modules")) return undefined;
  if (/[\\/]node_modules[\\/](react|react-dom|scheduler|react-router)[\\/]/.test(id))
    return "react";
  if (/[\\/]node_modules[\\/]@xterm[\\/]/.test(id)) return "xterm";
  return "vendor";
}

export interface ChunkSize {
  file: string;
  kb: number;
}

/** The emitted JavaScript chunks over the budget, largest first. */
export function oversized(
  bundle: Record<string, { type: string; fileName: string; code?: string }>,
  limitKb: number = CHUNK_LIMIT_KB,
): ChunkSize[] {
  const over: ChunkSize[] = [];
  for (const item of Object.values(bundle)) {
    if (item.type !== "chunk" || item.code === undefined) continue;
    const kb = new TextEncoder().encode(item.code).byteLength / 1000;
    if (kb > limitKb) over.push({ file: item.fileName, kb: Math.round(kb * 10) / 10 });
  }
  return over.sort((a, b) => b.kb - a.kb);
}

/** True when the build runs in CI (`CI` set to anything but an empty string, `0` or `false`). */
export function strictBuild(env: Record<string, string | undefined> = process.env): boolean {
  const raw = env.CI?.trim().toLowerCase();
  return raw !== undefined && raw !== "" && raw !== "0" && raw !== "false";
}

/**
 * Names every chunk over the budget after a build: an error in CI (a regression fails the gate), a
 * warning elsewhere (a person's first-launch build still produces working pages).
 */
export function chunkBudget(
  limitKb: number = CHUNK_LIMIT_KB,
  strict: boolean = strictBuild(),
): Plugin {
  return {
    name: "indus:chunk-budget",
    apply: "build",
    generateBundle(_options, bundle) {
      const over = oversized(
        bundle as Record<string, { type: string; fileName: string; code?: string }>,
        limitKb,
      );
      if (over.length === 0) return;
      const list = over.map((c) => `${c.file} (${c.kb} kB)`).join(", ");
      const message = `chunks over the ${limitKb} kB budget: ${list}`;
      if (strict) this.error(message);
      else this.warn(message);
    },
  };
}
