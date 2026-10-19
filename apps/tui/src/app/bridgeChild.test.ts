// The real `indus tui-bridge --stdio` child (P12-27): a prompt streams from a local fake
// OpenAI-compatible endpoint that never finishes, the child is killed mid-turn, and the app
// restarts it and reopens the same stored session without sending anything again.

import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { IndusClient } from "@indus/client";
import { spawnStdioTransport } from "@indus/client/stdio";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { transcriptStore } from "../state/transcriptStore.ts";
import { uiStore } from "../state/uiStore.ts";
import { resetStores } from "../testing/harness.ts";
import { AppRuntime, LinkWatch } from "./appRuntime.ts";
import { RESTART_LINE } from "./recovery.ts";

const ROOT = resolve(import.meta.dirname, "../../../..");
const INDUS = join(ROOT, ".venv", "bin", process.platform === "win32" ? "indus.exe" : "indus");
const runnable = existsSync(INDUS) && process.platform !== "win32";

async function until(check: () => boolean, ms: number, what: string): Promise<void> {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((done) => setTimeout(done, 50));
  }
}

describe.skipIf(!runnable)("real bridge child", () => {
  let model: Server;
  let home: string;
  let chatRequests = 0;

  beforeAll(async () => {
    // One SSE chunk, then silence: the turn stays running until the child is killed.
    model = createServer((req, res) => {
      if (req.method === "GET") {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ object: "list", data: [{ id: "fake-model", object: "model" }] }));
        return;
      }
      chatRequests += 1;
      req.resume();
      res.writeHead(200, { "content-type": "text/event-stream" });
      const chunk = {
        id: "c1",
        object: "chat.completion.chunk",
        created: 1,
        model: "fake-model",
        choices: [{ index: 0, delta: { role: "assistant", content: "Partial an" } }],
      };
      res.write(`data: ${JSON.stringify(chunk)}\n\n`);
    });
    await new Promise<void>((done) => model.listen(0, "127.0.0.1", done));
    const port = (model.address() as AddressInfo).port;
    home = mkdtempSync(join(tmpdir(), "indus-tui-child-"));
    writeFileSync(
      join(home, "config.yaml"),
      [
        "model:",
        "  default: fake-model",
        "  provider: custom",
        `  base_url: http://127.0.0.1:${port}/v1`,
        "",
      ].join("\n"),
    );
  });

  afterAll(() => {
    model?.closeAllConnections();
    model?.close();
    if (home) rmSync(home, { recursive: true, force: true });
  });

  it("kill mid-turn: the same stored session is reopened, the prompt not re-sent", async () => {
    resetStores();
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      INDUS_HOME: home,
      NO_PROXY: "127.0.0.1,localhost",
      no_proxy: "127.0.0.1,localhost",
    };
    for (const name of Object.keys(env)) {
      if (/_API_KEY$|^INDUS_TUI_|^INDUS_MODEL$/.test(name)) delete env[name];
    }
    const pids: number[] = [];
    const links = new LinkWatch();
    const client = new IndusClient({
      connect: links.wrap((hooks) => {
        const transport = spawnStdioTransport({
          command: INDUS,
          cwd: ROOT,
          env,
          logs: hooks.logs,
          onEvent: hooks.onEvent,
        });
        if (transport.pid !== undefined) pids.push(transport.pid);
        return transport;
      }),
    });
    const active = join(home, "active.json");
    const runtime = new AppRuntime({
      client,
      links,
      env: { INDUS_TUI_ACTIVE_SESSION_FILE: active },
      exit: () => {},
      history: { entries: () => [], push: () => true },
    });
    try {
      runtime.start();
      await until(() => uiStore.getState().sid !== null, 30_000, "the first session");
      const sid = String(uiStore.getState().sid);
      expect(existsSync(active)).toBe(true);

      runtime.composer.setText("hello from the test");
      runtime.submitter.submit();
      await until(() => chatRequests === 1, 30_000, "the model request");
      await until(() => uiStore.getState().busy, 10_000, "the turn to start");

      process.kill(pids[0] as number, "SIGKILL");
      await until(() => pids.length === 2, 10_000, "the restart");
      expect(transcriptStore.getState().messages.map((m) => m.text)).toContain(RESTART_LINE);
      await until(
        () => uiStore.getState().sid === sid && uiStore.getState().connected,
        30_000,
        "the reopened session",
      );
      const users = transcriptStore
        .getState()
        .messages.filter((m) => m.role === "user" && m.text === "hello from the test");
      expect(users).toHaveLength(1);
      expect(uiStore.getState().busy).toBe(false);
      // nothing went to the model again
      await new Promise((done) => setTimeout(done, 500));
      expect(chatRequests).toBe(1);
    } finally {
      runtime.dispose();
      await client.close();
    }
  }, 90_000);
});
