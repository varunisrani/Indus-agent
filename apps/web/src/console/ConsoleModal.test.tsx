import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CONSOLE_PATH, ConsoleModal, type ConsoleSocket, connectConsole } from "./ConsoleModal.tsx";
import type { ConsoleTerminal } from "./consoleSession.ts";

class FakeSocket implements ConsoleSocket {
  sent: Record<string, unknown>[] = [];
  closed: number | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number; reason?: string }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;

  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }

  close(code?: number): void {
    this.closed = code ?? 1000;
  }

  push(frame: Record<string, unknown>): void {
    act(() => this.onmessage?.({ data: JSON.stringify(frame) }));
  }
}

/** A screen that records what is written and lets the test type. */
class FakeTerminal implements ConsoleTerminal {
  written = "";
  disposed = false;
  private listener: ((data: string) => void) | null = null;
  open(): void {}
  write(text: string): void {
    this.written += text;
  }
  onData(listener: (data: string) => void): () => void {
    this.listener = listener;
    return () => {
      this.listener = null;
    };
  }
  fit(): void {}
  focus(): void {}
  dispose(): void {
    this.disposed = true;
  }
  type(data: string): void {
    act(() => this.listener?.(data));
  }
  /** What the screen shows, colours stripped. */
  get text(): string {
    // biome-ignore lint/suspicious/noControlCharactersInRegex: strips the terminal's colour codes
    return this.written.replace(/\x1b\[[0-9;]*[A-Za-z]/g, "");
  }
}

async function openWith(socket: FakeSocket, onClose = vi.fn()) {
  const connect = vi.fn(async () => socket);
  const term = new FakeTerminal();
  render(
    <ConsoleModal open onClose={onClose} connect={connect} createTerminal={async () => term} />,
  );
  await act(async () => {});
  socket.push({ type: "ready", profile: "work", prompt: "indus> " });
  return { connect, onClose, term };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ConsoleModal", () => {
  it("shows the panel with its status and profile, and the prompt in the terminal", async () => {
    const socket = new FakeSocket();
    const { term } = await openWith(socket);
    const dialog = screen.getByRole("dialog", { name: "Indus console" });
    expect(dialog.textContent).toContain("ready");
    expect(dialog.textContent).toContain("work");
    expect(term.text).toBe("indus> ");
  });

  it("echoes typing, sends one input frame on Enter and prints the output", async () => {
    const socket = new FakeSocket();
    const { term } = await openWith(socket);
    term.type("statuz");
    term.type("\x7f");
    expect(term.written.endsWith("statuz\b \b")).toBe(true);
    term.written = "";
    term.type("s");
    term.type("\r");
    expect(socket.sent).toEqual([{ type: "input", line: "status" }]);
    expect(screen.getByRole("dialog").textContent).toContain("running");
    // typing is ignored while the command runs
    term.type("x");
    socket.push({ type: "output", id: 1, stream: "stdout", data: "all good\n", command: "status" });
    socket.push({ type: "complete", id: 1, status: "ok", command: "status", prompt: "indus> " });
    expect(term.text).toBe("s\r\nall good\r\nindus> ");
    expect(term.text).not.toContain("x");
    expect(screen.getByRole("dialog").textContent).toContain("ready");
  });

  it("asks y/N before a command that changes something and confirms the exact command", async () => {
    const socket = new FakeSocket();
    const { term } = await openWith(socket);
    term.type("config set agent.max_turns 3\r");
    socket.push({
      type: "confirm_required",
      id: 1,
      command: "config set agent.max_turns 3",
      message: "Change this setting?",
    });
    socket.push({ type: "complete", id: 1, status: "confirm_required" });
    expect(term.text).toContain("Change this setting? config set agent.max_turns 3");
    expect(term.text.endsWith("Confirm? [y/N] ")).toBe(true);
    term.type("y\r");
    expect(socket.sent.at(-1)).toEqual({
      type: "confirm",
      command: "config set agent.max_turns 3",
    });
    socket.push({ type: "complete", id: 1, status: "ok" });
    expect(term.text.endsWith("indus> ")).toBe(true);
  });

  it("any other answer cancels a pending confirmation", async () => {
    const socket = new FakeSocket();
    const { term } = await openWith(socket);
    term.type("cron pause nightly\r");
    socket.push({ type: "confirm_required", id: 1, command: "cron pause nightly", message: "?" });
    term.type("\r");
    expect(socket.sent.at(-1)).toEqual({ type: "cancel" });
    socket.push({ type: "complete", id: 1, status: "cancelled" });
    expect(term.text).toContain("Cancelled.");
  });

  it("Ctrl+C cancels a running command; clear wipes the screen; exit closes", async () => {
    const socket = new FakeSocket();
    const { onClose, term } = await openWith(socket);
    term.type("sessions list\r");
    term.type("\x03");
    expect(socket.sent.at(-1)).toEqual({ type: "cancel" });
    socket.push({ type: "error", id: 1, message: "The command was cancelled." });
    socket.push({ type: "complete", id: 1, status: "cancelled" });
    expect(term.text).toContain("The command was cancelled.");
    socket.push({ type: "clear" });
    expect(term.written).toContain("\x1b[2J");
    socket.push({ type: "complete", id: 2, status: "exit" });
    expect(onClose).toHaveBeenCalled();
  });

  it("walks the history with the arrow keys", async () => {
    const socket = new FakeSocket();
    const { term } = await openWith(socket);
    term.type("help\r");
    socket.push({ type: "complete", id: 1, status: "ok" });
    term.type("\x1b[A");
    term.type("\r");
    expect(socket.sent.at(-1)).toEqual({ type: "input", line: "help" });
  });

  it("explains a refused socket and offers to reconnect", async () => {
    const socket = new FakeSocket();
    const connect = vi.fn(async () => socket);
    const term = new FakeTerminal();
    render(
      <ConsoleModal open onClose={vi.fn()} connect={connect} createTerminal={async () => term} />,
    );
    await act(async () => {});
    act(() => socket.onclose?.({ code: 4404 }));
    expect(term.text).toContain("not enabled");
    expect(screen.getByRole("dialog").textContent).toContain("closed");
    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
    await act(async () => {});
    expect(connect).toHaveBeenCalledTimes(2);
  });

  it("the real dial puts the credential in the subprotocols, never the URL", async () => {
    const made: { url: string; protocols: string[] }[] = [];
    class RecordingSocket {
      constructor(url: string, protocols: string[]) {
        made.push({ url, protocols });
      }
    }
    vi.stubGlobal("WebSocket", RecordingSocket);
    const token = ["tok", "en", "-value-for-the-test"].join("");
    window.__INDUS_SESSION_TOKEN__ = token;
    await connectConsole("work");
    expect(made).toHaveLength(1);
    const first = made[0];
    expect(first?.url).toContain(CONSOLE_PATH);
    expect(first?.url).toContain("profile=work");
    expect(first?.url).not.toContain(token);
    expect(first?.protocols).toEqual(["indus-dashboard-v1", `indus-dashboard-token.${token}`]);
  });
});
