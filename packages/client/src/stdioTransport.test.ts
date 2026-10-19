import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import { IndusClient } from "./client.ts";
import type { ClientEvent, TransportEvent } from "./localEvents.ts";
import { LogRing } from "./logRing.ts";
import { StdioTransport } from "./stdioTransport.ts";

const FAKE_CHILD = fileURLToPath(new URL("./testing/fakeChild.mjs", import.meta.url));
const open: StdioTransport[] = [];

function child(mode: string, extra: Partial<ConstructorParameters<typeof StdioTransport>[0]> = {}) {
  const events: TransportEvent[] = [];
  const frames: Array<Record<string, unknown>> = [];
  const transport = new StdioTransport({
    command: process.execPath,
    args: [FAKE_CHILD, ...mode.split(" ")],
    onEvent: (event) => events.push(event),
    ...extra,
  });
  transport.onFrame((frame) => frames.push(frame as Record<string, unknown>));
  open.push(transport);
  return { transport, events, frames };
}

async function until(check: () => boolean, ms = 5_000): Promise<void> {
  const deadline = Date.now() + ms;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("condition not reached in time");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

function running(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

afterEach(async () => {
  await Promise.all(open.splice(0).map((transport) => transport.close()));
});

describe("StdioTransport framing", () => {
  it("reassembles frames split across reads and multi-byte characters", async () => {
    const { frames } = child("chunks");
    await until(() => frames.length === 2);
    expect(frames[1]).toMatchObject({ method: "notice", params: { text: "héllo wörld" } });
  });

  it("accepts CRLF line ends", async () => {
    const { frames } = child("crlf");
    await until(() => frames.length === 2);
    expect(frames.map((frame) => frame.method)).toEqual(["gateway.ready", "notice"]);
  });

  it("takes a 1 MiB line as one frame", async () => {
    const { frames } = child("big");
    await until(() => frames.length === 2);
    const params = frames[1]?.params as { text: string } | undefined;
    expect(params?.text).toHaveLength(1024 * 1024);
  });

  it("turns non-frame lines into protocol_error events and keeps them off the frame path", async () => {
    const { frames, events, transport } = child("garbage");
    await until(() => frames.length === 1);
    const errors = events.filter((event) => event.type === "transport.protocol_error");
    expect(errors).toEqual([
      {
        type: "transport.protocol_error",
        preview: "Traceback (most recent call last): not a frame",
      },
      { type: "transport.protocol_error", preview: "[1, 2, 3]" },
    ]);
    expect(frames.map((frame) => frame.method)).toEqual(["gateway.ready"]);
    expect(transport.logs.lines().some((line) => line.includes("[protocol] not a frame"))).toBe(
      true,
    );
  });
});

describe("StdioTransport lifecycle", () => {
  it("routes stderr to the log ring and as events, then reports the exit", async () => {
    const { events, transport } = child("exit 3");
    await until(() => events.some((event) => event.type === "transport.exit"));
    const stderr = events.filter((event) => event.type === "transport.stderr");
    expect(stderr.map((event) => (event as { line: string }).line)).toEqual([
      "first problem",
      "last words",
    ]);
    expect(events.at(-1)).toEqual({
      type: "transport.exit",
      code: 3,
      signal: null,
      stderr_last: "last words",
    });
    expect(transport.logs.lines()).toContain("last words");
    expect(() => transport.send({ jsonrpc: "2.0", method: "x" })).toThrow("not running");
  });

  it("emits start_timeout with the command, cwd and the last 20 log lines", async () => {
    const logs = new LogRing();
    const { events } = child("silent", { startupTimeoutMs: 300, logs, cwd: process.cwd() });
    await until(() => events.some((event) => event.type === "transport.start_timeout"));
    const timeout = events.find((event) => event.type === "transport.start_timeout") as Extract<
      TransportEvent,
      { type: "transport.start_timeout" }
    >;
    expect(timeout.python).toBe(`${process.execPath} ${FAKE_CHILD} silent`);
    expect(timeout.cwd).toBe(process.cwd());
    expect(timeout.stderr_tail).toHaveLength(20);
    expect(timeout.stderr_tail.at(-1)).toBe("starting step 25");
  });

  it("does not time out once the ready frame came", async () => {
    const { events, frames } = child("echo", { startupTimeoutMs: 200 });
    await until(() => frames.length === 1);
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(events.filter((event) => event.type === "transport.start_timeout")).toEqual([]);
  });

  it("close() ends stdin; a child that ignores it is killed with its whole tree after the grace", async () => {
    const { frames, transport } = child("stubborn", { closeGraceMs: 300 });
    await until(() => frames.length === 1);
    const pids = (frames[0]?.params as { pids?: number[] } | undefined)?.pids ?? [];
    expect(pids).toHaveLength(2);
    expect(pids.every(running)).toBe(true);
    const started = Date.now();
    await transport.close();
    expect(Date.now() - started).toBeGreaterThanOrEqual(290);
    await until(() => !pids.some(running), 3_000);
    expect(transport.alive).toBe(false);
  });

  it("a well-behaved child exits on stdin end without being killed", async () => {
    const { events, frames, transport } = child("echo");
    await until(() => frames.length === 1);
    await transport.close();
    expect(events.at(-1)).toMatchObject({ type: "transport.exit", code: 0, signal: null });
  });

  it("reports a command that cannot start as an exit", async () => {
    const events: TransportEvent[] = [];
    const closes: string[] = [];
    const transport = new StdioTransport({
      command: "indus-no-such-command-for-tests",
      onEvent: (event) => events.push(event),
    });
    transport.onClose((info) => closes.push(info.reason));
    open.push(transport);
    await until(() => closes.length === 1);
    expect(events.at(-1)).toMatchObject({ type: "transport.exit", code: null });
    expect(transport.logs.lines().some((line) => line.includes("could not run"))).toBe(true);
  });
});

describe("IndusClient over a stdio child", () => {
  it("calls through the child and sees its events in order", async () => {
    const seen: ClientEvent[] = [];
    const client = new IndusClient({
      connect: ({ logs, onEvent }) => {
        const transport = new StdioTransport({
          command: process.execPath,
          args: [FAKE_CHILD, "garbage"],
          logs,
          onEvent,
        });
        open.push(transport);
        return transport;
      },
    });
    client.subscribe((event) => seen.push(event));
    client.start();
    const result = await client.request("session.create", {});
    expect(result).toEqual({ method: "session.create" });
    await until(() => seen.some((event) => event.type === "gateway.ready"));
    expect(seen.map((event) => event.type)).toEqual([
      "transport.protocol_error",
      "transport.protocol_error",
      "gateway.ready",
    ]);
    await client.close();
    expect(client.logs.lines().some((line) => line.includes("exited code=0"))).toBe(true);
  });
});
