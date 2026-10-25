// Client for the profile daemon's owner-only control socket (P15-27 wire, P17-11 verbs), the
// Node twin of `indus.service.gateway_control`. One exchange per connection: one JSON line
// `{protocol: 1, verb, id, params?}` out, one line `{ok, protocol, id, result | error, detail?}`
// back. The socket is `<profile home>/gateway.sock`; when that path is too long for a Unix socket
// the daemon binds in a private directory and leaves `<home>/gateway.sock.path`, which is followed
// only when the pointer is our own regular file with no group/other bits and its target's
// directory is ours with mode 0700. A home other users can write to is refused before connecting
// (they could swap the socket). On Windows the daemon serves the same wire on a named pipe
// (`\\.\pipe\indus-gw-<32 hex>`) whose name the pointer file holds; a pointer holding anything
// else is ignored, and the home only has to be a directory: Windows has no uids or mode bits (the
// user profile's ACL protects it). See docs/notes/P25/windows-gateway.md.

import fs from "node:fs";
import net from "node:net";
import path from "node:path";

export const SOCKET_FILE = "gateway.sock";
export const POINTER_FILE = "gateway.sock.path";
export const MAX_SOCKET_PATH = 100;
export const CONTROL_TIMEOUT_MS = 2_000;
export const TICKET_TIMEOUT_MS = 5_000;
export const WS_INTERACTIVE = "ws-interactive";
const MAX_ANSWER_BYTES = 512 * 1024;
const PIPE_NAME = /^\\\\\.\\pipe\\indus-gw-[0-9a-f]{32}$/;

export interface ControlReply {
  ok: boolean;
  result?: Record<string, unknown>;
  /** `unsafe_home`, `no_socket`, `unreachable`, `timeout`, `bad_response`, or the daemon's code. */
  error?: string;
  detail?: string;
}

export interface MintedTicket {
  ticket: string;
  wsUrl: string;
  profileId: string;
  instanceId: string;
  expiresInMs: number;
}

export type MintReply =
  | ({ ok: true } & MintedTicket)
  | { ok: false; error: string; detail: string };

function uid(): number | null {
  return typeof process.getuid === "function" ? process.getuid() : null;
}

function ownedPrivate(info: fs.Stats, kind: "file" | "dir"): boolean {
  const me = uid();
  const right = kind === "file" ? info.isFile() : info.isDirectory();
  return right && (me === null || info.uid === me) && (info.mode & 0o077) === 0;
}

/** We own `home` and nobody else may write to it (Windows: it is a directory). */
export function homeIsSafe(home: string, platform: string = process.platform): boolean {
  let info: fs.Stats;
  try {
    info = fs.statSync(home);
  } catch {
    return false;
  }
  if (platform === "win32") return info.isDirectory();
  const me = uid();
  if (!info.isDirectory() || (me !== null && info.uid !== me)) return false;
  return (info.mode & 0o022) === 0;
}

/** Where to connect for `home`: the direct socket, a trusted pointer's target, or null. */
export function socketPathFor(home: string): string | null {
  const direct = path.join(home, SOCKET_FILE);
  try {
    if (fs.lstatSync(direct).isSocket()) return direct;
  } catch {
    // no direct socket; try the pointer
  }
  const pointer = path.join(home, POINTER_FILE);
  try {
    if (!ownedPrivate(fs.lstatSync(pointer), "file")) return null;
    const target = fs.readFileSync(pointer, "utf8").trim();
    if (!path.isAbsolute(target)) return null;
    if (!ownedPrivate(fs.lstatSync(path.dirname(target)), "dir")) return null;
    return fs.lstatSync(target).isSocket() ? target : null;
  } catch {
    return null;
  }
}

/** A well-formed control pipe name (`\\.\pipe\indus-gw-` and 32 lowercase hex digits). */
export function isPipeName(text: string): boolean {
  return PIPE_NAME.test(text);
}

/** Windows: the pipe the pointer in `home` names, or null. Whether it is served is the dial's
 * question. */
export function pipeNameFor(home: string): string | null {
  const pointer = path.join(home, POINTER_FILE);
  try {
    if (!fs.lstatSync(pointer).isFile()) return null;
    const name = fs.readFileSync(pointer, "utf8").trim();
    return isPipeName(name) ? name : null;
  } catch {
    return null;
  }
}

function connect(file: string): net.Socket {
  if (Buffer.byteLength(file) <= MAX_SOCKET_PATH) return net.connect(file);
  // A direct socket in a long home (the test fake binds like this): connect by a relative name
  // from its directory, switching the working directory for the one synchronous connect call.
  const before = process.cwd();
  process.chdir(path.dirname(file));
  try {
    return net.connect(path.basename(file));
  } finally {
    process.chdir(before);
  }
}

let ids = 0;

/** Ask the daemon serving `home`. Never throws: local problems come back as error codes. */
export function controlQuery(
  home: string,
  verb: string,
  params?: Record<string, unknown>,
  timeoutMs = CONTROL_TIMEOUT_MS,
): Promise<ControlReply> {
  const windows = process.platform === "win32";
  if (!homeIsSafe(home)) return Promise.resolve({ ok: false, error: "unsafe_home" });
  const file = windows ? pipeNameFor(home) : socketPathFor(home);
  if (file === null) return Promise.resolve({ ok: false, error: "no_socket" });
  return new Promise((resolve) => {
    let data = "";
    let done = false;
    let socket: net.Socket;
    const finish = (reply: ControlReply) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      socket?.destroy();
      resolve(reply);
    };
    const timer = setTimeout(() => finish({ ok: false, error: "timeout" }), timeoutMs);
    try {
      socket = windows ? net.connect(file) : connect(file);
    } catch {
      finish({ ok: false, error: "unreachable" });
      return;
    }
    socket.setEncoding("utf8");
    socket.on("connect", () => {
      ids += 1;
      const request: Record<string, unknown> = { protocol: 1, verb, id: `d${ids}` };
      if (params !== undefined) request.params = params;
      socket.write(`${JSON.stringify(request)}\n`);
    });
    socket.on("data", (chunk: string) => {
      data += chunk;
      if (data.length > MAX_ANSWER_BYTES) {
        finish({ ok: false, error: "bad_response", detail: "the answer is too long" });
        return;
      }
      const newline = data.indexOf("\n");
      if (newline >= 0) finish(decode(data.slice(0, newline)));
    });
    socket.on("error", (error: NodeJS.ErrnoException) =>
      // a pipe that no longer exists: the daemon is gone (the pointer outlived a crash)
      finish({
        ok: false,
        error: windows && error.code === "ENOENT" ? "no_socket" : "unreachable",
      }),
    );
    socket.on("close", () =>
      finish(data.trim() === "" ? { ok: false, error: "unreachable" } : decode(data)),
    );
  });
}

function decode(line: string): ControlReply {
  let answer: unknown;
  try {
    answer = JSON.parse(line);
  } catch {
    return { ok: false, error: "bad_response", detail: "the answer is not JSON" };
  }
  if (answer === null || typeof answer !== "object") return { ok: false, error: "bad_response" };
  const record = answer as Record<string, unknown>;
  if (record.ok === true) {
    const result = record.result;
    if (result === null || typeof result !== "object") return { ok: false, error: "bad_response" };
    return { ok: true, result: result as Record<string, unknown> };
  }
  return {
    ok: false,
    error: typeof record.error === "string" && record.error !== "" ? record.error : "failed",
    detail: typeof record.detail === "string" ? record.detail : "",
  };
}

const LOOPBACK = new Set(["127.0.0.1", "[::1]", "localhost"]);

/** `ws://` on a loopback address with a port, no user-info, query or fragment. */
export function wsUrlIsSafe(url: unknown): url is string {
  if (typeof url !== "string" || url === "") return false;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  return (
    (parsed.protocol === "ws:" || parsed.protocol === "wss:") &&
    LOOPBACK.has(parsed.hostname) &&
    parsed.port !== "" &&
    parsed.username === "" &&
    parsed.password === "" &&
    parsed.search === "" &&
    parsed.hash === ""
  );
}

export function looksLikeTicket(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{43}$/.test(value);
}

/** A live daemon's identity (an answer IS liveness), or null. */
export async function identify(home: string, timeoutMs = CONTROL_TIMEOUT_MS) {
  const reply = await controlQuery(home, "identify", undefined, timeoutMs);
  if (!reply.ok || reply.result === undefined) return null;
  const pid = reply.result.pid;
  return typeof pid === "number" && Number.isInteger(pid) && pid > 0 ? reply.result : null;
}

/** Mint one `ws-interactive` ticket; the answer must name the profile and instance we expect. */
export async function mintTicket(
  home: string,
  expect: { profileId?: string; instanceId?: string },
  query: typeof controlQuery = controlQuery,
): Promise<MintReply> {
  const params: Record<string, unknown> = { purpose: WS_INTERACTIVE };
  if (expect.profileId !== undefined) params.profile_id = expect.profileId;
  if (expect.instanceId !== undefined) params.instance_id = expect.instanceId;
  const reply = await query(home, "session-ticket", params, TICKET_TIMEOUT_MS);
  if (!reply.ok || reply.result === undefined) {
    return { ok: false, error: reply.error ?? "failed", detail: reply.detail ?? "" };
  }
  const r = reply.result;
  if (!looksLikeTicket(r.ticket)) return { ok: false, error: "bad_response", detail: "no ticket" };
  if (!wsUrlIsSafe(r.ws_url)) {
    return { ok: false, error: "bad_response", detail: "the ws_url is not a loopback URL" };
  }
  if (typeof r.profile_id !== "string" || typeof r.instance_id !== "string") {
    return { ok: false, error: "bad_response", detail: "the answer names no profile" };
  }
  if (expect.profileId !== undefined && r.profile_id !== expect.profileId) {
    return { ok: false, error: "profile_mismatch", detail: "another profile answered" };
  }
  if (expect.instanceId !== undefined && r.instance_id !== expect.instanceId) {
    return { ok: false, error: "stale_instance", detail: "the daemon restarted" };
  }
  const seconds =
    typeof r.expires_in === "number" && Number.isFinite(r.expires_in) ? r.expires_in : 30;
  return {
    ok: true,
    ticket: r.ticket,
    wsUrl: r.ws_url,
    profileId: r.profile_id,
    instanceId: r.instance_id,
    expiresInMs: Math.round(seconds * 1000),
  };
}
