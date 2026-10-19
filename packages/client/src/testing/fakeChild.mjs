// Stand-in for the bridge child in stdio transport tests: `node fakeChild.mjs <mode>`.
// Modes shape stdout/stderr the ways a real child can (split reads, CRLF, huge lines, noise,
// silence, exit codes, a grandchild that outlives stdin).

import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

const mode = process.argv[2] ?? "echo";
const out = (text) => process.stdout.write(text);
const frame = (value) => `${JSON.stringify(value)}\n`;
const ready = frame({ jsonrpc: "2.0", method: "gateway.ready", params: { type: "gateway.ready" } });
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function answerRequests() {
  const lines = createInterface({ input: process.stdin });
  lines.on("line", (line) => {
    const request = JSON.parse(line);
    if (request.id !== undefined) {
      out(frame({ jsonrpc: "2.0", id: request.id, result: { method: request.method } }));
    }
  });
  lines.on("close", () => process.exit(0));
}

async function main() {
  switch (mode) {
    case "echo":
      out(ready);
      answerRequests();
      break;
    case "chunks": {
      // A frame cut in three reads, one cut inside a multi-byte character.
      const text = frame({
        jsonrpc: "2.0",
        method: "notice",
        params: { type: "notice", text: "héllo wörld" },
      });
      const bytes = Buffer.from(text);
      const cut = bytes.indexOf(Buffer.from("é")) + 1;
      out(ready);
      await wait(20);
      process.stdout.write(bytes.subarray(0, 10));
      await wait(20);
      process.stdout.write(bytes.subarray(10, cut));
      await wait(20);
      process.stdout.write(bytes.subarray(cut));
      answerRequests();
      break;
    }
    case "crlf":
      out(ready.replace("\n", "\r\n"));
      out(
        frame({ jsonrpc: "2.0", method: "notice", params: { type: "notice", text: "a" } }).replace(
          "\n",
          "\r\n",
        ),
      );
      answerRequests();
      break;
    case "big": {
      const text = "x".repeat(1024 * 1024);
      out(ready);
      const line = frame({ jsonrpc: "2.0", method: "notice", params: { type: "notice", text } });
      for (let at = 0; at < line.length; at += 65_536) out(line.slice(at, at + 65_536));
      answerRequests();
      break;
    }
    case "garbage":
      out("Traceback (most recent call last): not a frame\n");
      out("[1, 2, 3]\n");
      out(ready);
      answerRequests();
      break;
    case "silent":
      for (let i = 1; i <= 25; i += 1) process.stderr.write(`starting step ${i}\n`);
      setInterval(() => undefined, 1_000);
      break;
    case "exit":
      out(ready);
      process.stderr.write("first problem\nlast words\n");
      await wait(20);
      process.exit(Number(process.argv[3] ?? "3"));
      break;
    case "stubborn": {
      // Ignores stdin EOF and leaves a grandchild running in its process group.
      const grandchild = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
        stdio: "ignore",
      });
      process.stdin.resume();
      process.stdin.on("end", () => undefined);
      out(
        frame({
          jsonrpc: "2.0",
          method: "gateway.ready",
          params: { type: "gateway.ready", pids: [process.pid, grandchild.pid] },
        }),
      );
      setInterval(() => undefined, 1_000);
      break;
    }
    default:
      process.stderr.write(`unknown mode ${mode}\n`);
      process.exit(2);
  }
}

await main();
