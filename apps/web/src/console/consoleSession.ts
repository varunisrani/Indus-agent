// The admin console's line discipline (W52-f). The terminal is the input line: this class prints
// the prompt, echoes what is typed, keeps a history, and turns Enter / Ctrl+C / the confirm answer
// into frames for `/api/console`. Frames from the server are printed back. It knows nothing about
// React or xterm: the modal gives it a `ConsoleTerminal` (a fake one in tests) and a `send`.

export interface ConsoleTerminal {
  open(parent: HTMLElement): void;
  write(text: string): void;
  onData(listener: (data: string) => void): () => void;
  /** Resize to the parent's box. */
  fit(): void;
  focus(): void;
  dispose(): void;
}

export type ConsolePhase = "connecting" | "ready" | "running" | "closed" | "error";

export const HISTORY_LIMIT = 200;
export const CONFIRM_PROMPT = "Confirm? [y/N] ";

const RED = "\x1b[31m";
const YELLOW = "\x1b[33m";
const DIM = "\x1b[2m";
const RESET = "\x1b[0m";
const CLEAR_SCREEN = "\x1b[2J\x1b[3J\x1b[H";

function textOf(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** "\n" → "\r\n" so the terminal returns to column 0. */
export function terminalText(text: string): string {
  return text.replace(/\r?\n/g, "\r\n");
}

export class ConsoleSession {
  phase: ConsolePhase = "connecting";
  profile = "";
  prompt = "indus> ";
  private buffer = "";
  private readonly history: string[] = [];
  private cursor = 0;
  private confirming: string | null = null;
  private term: ConsoleTerminal | null = null;
  private queued: string[] = [];
  private atLineStart = true;

  private readonly send: (frame: Record<string, unknown>) => void;
  private readonly changed: () => void;
  private readonly exit: () => void;

  constructor(
    send: (frame: Record<string, unknown>) => void,
    changed: () => void,
    exit: () => void,
  ) {
    this.send = send;
    this.changed = changed;
    this.exit = exit;
  }

  /** Give the session its screen; anything printed before is replayed. */
  attach(term: ConsoleTerminal): void {
    this.term = term;
    for (const text of this.queued) term.write(text);
    this.queued = [];
  }

  private out(text: string): void {
    if (!text) return;
    const normal = terminalText(text);
    this.atLineStart = normal.endsWith("\n");
    if (this.term) this.term.write(normal);
    else this.queued.push(normal);
  }

  private newLine(): void {
    if (!this.atLineStart) this.out("\n");
  }

  private setPhase(phase: ConsolePhase): void {
    this.phase = phase;
    this.changed();
  }

  private showPrompt(): void {
    this.newLine();
    this.out(this.confirming !== null ? CONFIRM_PROMPT : this.prompt);
    this.atLineStart = false;
  }

  /** A red line (refusals, close reasons, connect failures). */
  fail(message: string, phase: ConsolePhase = "closed"): void {
    this.newLine();
    this.out(`${RED}${message}${RESET}\n`);
    this.confirming = null;
    this.setPhase(phase);
  }

  /** Back to the start for a new connection. */
  reset(): void {
    this.buffer = "";
    this.confirming = null;
    this.setPhase("connecting");
  }

  frame(frame: Record<string, unknown>): void {
    switch (frame.type) {
      case "ready":
        this.profile = textOf(frame.profile);
        if (typeof frame.prompt === "string") this.prompt = frame.prompt;
        this.setPhase("ready");
        this.showPrompt();
        return;
      case "output": {
        const data = textOf(frame.data);
        this.out(frame.stream === "stderr" ? `${RED}${data}${RESET}` : data);
        if (data.endsWith("\n")) this.atLineStart = true;
        return;
      }
      case "error":
        this.newLine();
        this.out(`${RED}${textOf(frame.message)}${RESET}\n`);
        return;
      case "confirm_required":
        this.confirming = textOf(frame.command);
        this.newLine();
        this.out(`${YELLOW}${textOf(frame.message)} ${textOf(frame.command)}${RESET}\n`);
        this.buffer = "";
        this.setPhase("ready");
        this.showPrompt();
        return;
      case "clear":
        this.out(CLEAR_SCREEN);
        this.atLineStart = true;
        return;
      case "complete":
        if (typeof frame.prompt === "string") this.prompt = frame.prompt;
        if (frame.status === "exit") {
          this.setPhase("closed");
          this.exit();
          return;
        }
        if (frame.status === "confirm_required") return; // already asking
        if (frame.status === "cancelled") {
          this.newLine();
          this.out(`${DIM}Cancelled.${RESET}\n`);
        }
        this.confirming = null;
        this.buffer = "";
        this.setPhase("ready");
        this.showPrompt();
        return;
      default:
        return;
    }
  }

  private replaceLine(next: string): void {
    this.buffer = next;
    this.out(`\r\x1b[K${this.confirming !== null ? CONFIRM_PROMPT : this.prompt}${next}`);
    this.atLineStart = false;
  }

  private enter(): void {
    const line = this.buffer.trim();
    this.buffer = "";
    this.out("\n");
    if (this.confirming !== null) {
      const command = this.confirming;
      this.confirming = null;
      const yes = line.toLowerCase() === "y" || line.toLowerCase() === "yes";
      this.send(yes ? { type: "confirm", command } : { type: "cancel" });
      this.setPhase("running");
      return;
    }
    if (!line) {
      this.showPrompt();
      return;
    }
    if (this.history.at(-1) !== line) this.history.push(line);
    if (this.history.length > HISTORY_LIMIT) this.history.shift();
    this.cursor = this.history.length;
    this.send({ type: "input", line });
    this.setPhase("running");
  }

  private interrupt(): void {
    this.out("^C\n");
    this.buffer = "";
    if (this.phase === "running" || this.confirming !== null) {
      this.confirming = null;
      this.send({ type: "cancel" });
      return;
    }
    this.showPrompt();
  }

  /** Keys and pasted text from the terminal. */
  input(data: string): void {
    if (data === "\x03") {
      if (this.phase === "ready" || this.phase === "running") this.interrupt();
      return;
    }
    if (this.phase !== "ready") return;
    if (data === "\x0c") {
      this.out(CLEAR_SCREEN);
      this.replaceLine(this.buffer);
      return;
    }
    if (data === "\x1b[A" || data === "\x1b[B") {
      if (this.confirming !== null || this.history.length === 0) return;
      const step = data === "\x1b[A" ? -1 : 1;
      this.cursor = Math.max(0, Math.min(this.history.length, this.cursor + step));
      this.replaceLine(this.history[this.cursor] ?? "");
      return;
    }
    if (data.startsWith("\x1b")) return; // other keys (arrows sideways, function keys)
    for (const char of data) {
      if (this.phase !== "ready") return;
      if (char === "\r" || char === "\n") {
        this.enter();
      } else if (char === "\x7f" || char === "\b") {
        if (this.buffer) {
          this.buffer = this.buffer.slice(0, -1);
          this.out("\b \b");
        }
      } else if (char >= " ") {
        this.buffer += char;
        this.out(char);
      }
    }
  }
}
