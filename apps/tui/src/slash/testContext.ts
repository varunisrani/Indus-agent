// Test-only: a slash context wired to a fake bridge, recording what the commands did.

import { RpcChannel } from "@indus/client";
import { FakeServer } from "@indus/client/testing";
import type { CatalogEntry } from "@indus/protocol";
import { vi } from "vitest";
import { overlayStore } from "../state/overlayStore.ts";
import { uiStore } from "../state/uiStore.ts";
import type { SlashContext } from "./types.ts";

export interface Recorded {
  lines: string[];
  pages: { text: string; title: string | undefined }[];
  submits: { body: string; shown: string | undefined }[];
  prefills: string[];
  exits: number[];
  timers: { run: () => void; ms: number }[];
}

export function slashSetup(options: { catalog?: CatalogEntry[]; timeoutMs?: number } = {}) {
  const server = new FakeServer();
  const channel = new RpcChannel(
    options.timeoutMs === undefined ? {} : { timeoutMs: options.timeoutMs },
  );
  channel.attach(server.connect());
  uiStore.setState({ sid: "s-1", storedSid: "s-1", revision: 0, busy: false, statusText: "" });
  overlayStore.setState({ card: null });
  const out: Recorded = { lines: [], pages: [], submits: [], prefills: [], exits: [], timers: [] };
  const confirm = vi.fn(async () => true);
  const ctx: SlashContext = {
    rpc: { call: (method, params) => channel.call(method, params as object) },
    ui: uiStore,
    overlay: overlayStore,
    systemLine: (text) => out.lines.push(text),
    page: (text, title) => out.pages.push({ text, title }),
    submit: (body, shown) => out.submits.push({ body, shown }),
    prefill: (text) => out.prefills.push(text),
    confirm,
    exit: (code) => out.exits.push(code),
    catalog: () => options.catalog ?? null,
    env: {},
    later: (run, ms) => out.timers.push({ run, ms }),
  };
  return { server, channel, ctx, out, confirm };
}

export const entry = (name: string, extra: Partial<CatalogEntry> = {}): CatalogEntry => ({
  name,
  description: `${name} command`,
  category: "tools",
  kind: "builtin",
  runs: "server",
  ...extra,
});
