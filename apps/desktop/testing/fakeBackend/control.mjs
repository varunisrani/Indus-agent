// Client side of the fake daemon's control socket, for ensure.mjs and tests: one JSON line out,
// one JSON line back (the P15-27 wire). A home too long for a socket path is reached through a
// relative name from the home (see server.mjs), with the working directory switched for the one
// synchronous `connect` call.

import net from "node:net";
import path from "node:path";

export const MAX_SOCKET_PATH = 100;

export function socketPathFits(file) {
  return Buffer.byteLength(file) <= MAX_SOCKET_PATH;
}

function connectTo(home) {
  const sock = path.join(home, "gateway.sock");
  if (socketPathFits(sock)) return net.connect(sock);
  const before = process.cwd();
  process.chdir(home);
  try {
    return net.connect("gateway.sock");
  } finally {
    process.chdir(before);
  }
}

let ids = 0;

/** Send one request; resolves the parsed answer, or `{ ok: false, error: "unreachable" }`. */
export function controlQuery(home, verb, params, timeoutMs = 5_000) {
  return new Promise((resolve) => {
    let data = "";
    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      socket.destroy();
      resolve(value);
    };
    const socket = connectTo(home);
    const timer = setTimeout(() => finish({ ok: false, error: "timeout" }), timeoutMs);
    socket.setEncoding("utf8");
    socket.on("connect", () => {
      ids += 1;
      const request = { protocol: 1, verb, id: `q${ids}` };
      if (params !== undefined) request.params = params;
      socket.write(`${JSON.stringify(request)}\n`);
    });
    socket.on("data", (chunk) => {
      data += chunk;
      const newline = data.indexOf("\n");
      if (newline < 0) return;
      try {
        finish(JSON.parse(data.slice(0, newline)));
      } catch {
        finish({ ok: false, error: "bad_response" });
      }
    });
    socket.on("error", () => finish({ ok: false, error: "unreachable" }));
    socket.on("close", () => finish({ ok: false, error: "unreachable" }));
  });
}
