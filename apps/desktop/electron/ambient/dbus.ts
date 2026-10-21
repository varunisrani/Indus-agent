// The few D-Bus conversations the Linux desktop needs (P20-36), spoken through glib's `gdbus`
// tool: is a StatusNotifier host registered (a tray object alone proves nothing), what can the
// notification service do, show / close a notification with actions, and which action the person
// picked. Every call is a child process without a shell, bounded by a timeout; a desktop without
// `gdbus` or a session bus simply answers "not available".

import { execFile, spawn } from "node:child_process";
import readline from "node:readline";

export const DBUS_CALL_TIMEOUT_MS = 2_000;

const WATCHER = "org.kde.StatusNotifierWatcher";
const WATCHER_PATH = "/StatusNotifierWatcher";
const NOTIFICATIONS = "org.freedesktop.Notifications";
const NOTIFICATIONS_PATH = "/org/freedesktop/Notifications";

export interface RunResult {
  code: number | null;
  stdout: string;
  stderr: string;
}

export interface LineStream {
  stop(): void;
}

/** How commands run (a fake in tests). */
export interface CommandRunner {
  run(file: string, args: readonly string[], timeoutMs: number): Promise<RunResult>;
  /** A long-running command whose stdout lines are handed over; `onExit` when it ends. */
  lines(
    file: string,
    args: readonly string[],
    onLine: (line: string) => void,
    onExit: () => void,
  ): LineStream;
}

export const nodeRunner: CommandRunner = {
  run: (file, args, timeoutMs) =>
    new Promise((resolve) => {
      try {
        execFile(
          file,
          [...args],
          { timeout: timeoutMs, windowsHide: true, encoding: "utf8", maxBuffer: 256 * 1024 },
          (error, stdout, stderr) => {
            const code =
              error === null ? 0 : typeof error.code === "number" ? error.code : (null as null);
            resolve({ code, stdout: String(stdout ?? ""), stderr: String(stderr ?? "") });
          },
        );
      } catch (error) {
        resolve({ code: null, stdout: "", stderr: error instanceof Error ? error.message : "" });
      }
    }),
  lines: (file, args, onLine, onExit) => {
    let ended = false;
    const end = () => {
      if (ended) return;
      ended = true;
      onExit();
    };
    let child: ReturnType<typeof spawn>;
    try {
      child = spawn(file, [...args], { stdio: ["ignore", "pipe", "ignore"], windowsHide: true });
    } catch {
      queueMicrotask(end);
      return { stop: () => {} };
    }
    child.on("error", end);
    child.on("exit", end);
    if (child.stdout !== null) {
      readline.createInterface({ input: child.stdout }).on("line", onLine);
    }
    return {
      stop: () => {
        if (!ended && child.exitCode === null) child.kill("SIGTERM");
      },
    };
  },
};

// -- GVariant text ---------------------------------------------------------------------------------

/** A GVariant text literal for `text` (single-quoted; quotes, backslashes and controls escaped). */
export function gvariantString(text: string): string {
  let out = "'";
  for (const char of text) {
    const code = char.codePointAt(0) ?? 0;
    if (char === "\\") out += "\\\\";
    else if (char === "'") out += "\\'";
    else if (code < 0x20 || code === 0x7f) out += `\\u${code.toString(16).padStart(4, "0")}`;
    else out += char;
  }
  return `${out}'`;
}

export function gvariantStringArray(items: readonly string[]): string {
  return items.length === 0 ? "@as []" : `[${items.map(gvariantString).join(", ")}]`;
}

/** `(<true>,)` / `(true,)` → true; anything else → null. */
export function parseBooleanReply(stdout: string): boolean | null {
  const match = /^\(\s*<?\s*(true|false)\s*>?\s*,?\s*\)$/.exec(stdout.trim());
  return match === null ? null : match[1] === "true";
}

/** `(uint32 7,)` → 7. */
export function parseUint32Reply(stdout: string): number | null {
  const match = /^\(\s*uint32\s+(\d+)\s*,?\s*\)$/.exec(stdout.trim());
  return match === null ? null : Number(match[1]);
}

/** `(['actions', 'body'],)` → ["actions", "body"]. */
export function parseStringArrayReply(stdout: string): string[] | null {
  const text = stdout.trim();
  if (!text.startsWith("([") && !text.startsWith("(@as [")) return null;
  return [...text.matchAll(/'((?:[^'\\]|\\.)*)'/g)].map((m) =>
    (m[1] ?? "").replace(/\\(.)/g, "$1"),
  );
}

export type NotificationSignal =
  | { kind: "action"; id: number; action: string }
  | { kind: "closed"; id: number };

/** One line of `gdbus monitor` on the notification service, if it is a signal we use. */
export function parseNotificationSignal(line: string): NotificationSignal | null {
  const action = /\.ActionInvoked\s*\(\s*uint32\s+(\d+)\s*,\s*'((?:[^'\\]|\\.)*)'\s*\)/.exec(line);
  if (action !== null) {
    return {
      kind: "action",
      id: Number(action[1]),
      action: (action[2] ?? "").replace(/\\(.)/g, "$1"),
    };
  }
  const closed = /\.NotificationClosed\s*\(\s*uint32\s+(\d+)\s*,/.exec(line);
  if (closed !== null) return { kind: "closed", id: Number(closed[1]) };
  return null;
}

// -- the client ------------------------------------------------------------------------------------

export type HostCheck = { ok: true } | { ok: false; reason: string };

export interface NotifyRequest {
  appName: string;
  title: string;
  body: string;
  /** [id, label] pairs; `default` is the click on the notification itself. */
  actions: ReadonlyArray<readonly [string, string]>;
  replacesId?: number;
}

export class GdbusClient {
  private readonly runner: CommandRunner;
  private readonly binary: string;
  private readonly timeoutMs: number;

  constructor(
    runner: CommandRunner = nodeRunner,
    options: { binary?: string; timeoutMs?: number } = {},
  ) {
    this.runner = runner;
    this.binary = options.binary ?? "gdbus";
    this.timeoutMs = options.timeoutMs ?? DBUS_CALL_TIMEOUT_MS;
  }

  private call(dest: string, objectPath: string, method: string, args: readonly string[]) {
    return this.runner.run(
      this.binary,
      [
        "call",
        "--session",
        "--dest",
        dest,
        "--object-path",
        objectPath,
        "--method",
        method,
        ...args,
      ],
      this.timeoutMs,
    );
  }

  /** Whether some panel shows StatusNotifier items right now. */
  async statusNotifierHost(): Promise<HostCheck> {
    const result = await this.call(WATCHER, WATCHER_PATH, "org.freedesktop.DBus.Properties.Get", [
      WATCHER,
      "IsStatusNotifierHostRegistered",
    ]);
    if (result.code !== 0) {
      return {
        ok: false,
        reason:
          result.code === null
            ? "the D-Bus session could not be asked (gdbus missing or timed out)"
            : "no system tray watcher is running",
      };
    }
    const registered = parseBooleanReply(result.stdout);
    if (registered === true) return { ok: true };
    return { ok: false, reason: "no system tray host is registered" };
  }

  async notificationCapabilities(): Promise<string[] | null> {
    const result = await this.call(
      NOTIFICATIONS,
      NOTIFICATIONS_PATH,
      `${NOTIFICATIONS}.GetCapabilities`,
      [],
    );
    return result.code === 0 ? parseStringArrayReply(result.stdout) : null;
  }

  /** The service's id for the notification, or null when it could not be shown. */
  async notify(request: NotifyRequest): Promise<number | null> {
    const result = await this.call(NOTIFICATIONS, NOTIFICATIONS_PATH, `${NOTIFICATIONS}.Notify`, [
      gvariantString(request.appName),
      String(request.replacesId ?? 0),
      gvariantString(""),
      gvariantString(request.title),
      gvariantString(request.body),
      gvariantStringArray(request.actions.flat()),
      // Our own completion sound plays in the window; the service stays quiet.
      "{'suppress-sound': <true>}",
      "-1",
    ]);
    return result.code === 0 ? parseUint32Reply(result.stdout) : null;
  }

  async close(id: number): Promise<void> {
    await this.call(NOTIFICATIONS, NOTIFICATIONS_PATH, `${NOTIFICATIONS}.CloseNotification`, [
      String(id),
    ]);
  }

  /** Follow the service's signals until `stop()` (or the monitor ends: `onEnd`). */
  watchNotifications(
    onSignal: (signal: NotificationSignal) => void,
    onEnd: () => void,
  ): LineStream {
    return this.runner.lines(
      this.binary,
      ["monitor", "--session", "--dest", NOTIFICATIONS, "--object-path", NOTIFICATIONS_PATH],
      (line) => {
        const signal = parseNotificationSignal(line);
        if (signal !== null) onSignal(signal);
      },
      onEnd,
    );
  }
}
