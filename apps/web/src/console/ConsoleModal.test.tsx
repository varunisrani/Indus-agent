import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CONSOLE_PATH, ConsoleModal, type ConsoleSocket, connectConsole } from "./ConsoleModal.tsx";

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

async function openWith(socket: FakeSocket, onClose = vi.fn()) {
  const connect = vi.fn(async () => socket);
  render(<ConsoleModal open onClose={onClose} connect={connect} />);
  await act(async () => {});
  socket.push({ type: "ready", profile: "work", prompt: "indus> " });
  return { connect, onClose };
}

function type(line: string) {
  fireEvent.change(screen.getByLabelText("Console command"), { target: { value: line } });
  fireEvent.submit(screen.getByLabelText("Console command").closest("form") as HTMLFormElement);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ConsoleModal", () => {
  it("sends one input frame and shows the output", async () => {
    const socket = new FakeSocket();
    await openWith(socket);
    expect(screen.getByText(/for work/)).toBeTruthy();
    type("status");
    expect(socket.sent).toEqual([{ type: "input", line: "status" }]);
    // a second line cannot be sent while the first runs: the input is disabled
    expect((screen.getByLabelText("Console command") as HTMLInputElement).disabled).toBe(true);
    socket.push({ type: "output", id: 1, stream: "stdout", data: "all good", command: "status" });
    socket.push({ type: "complete", id: 1, status: "ok", command: "status", prompt: "indus> " });
    expect(screen.getByRole("log").textContent).toContain("indus> status");
    expect(screen.getByRole("log").textContent).toContain("all good");
    expect((screen.getByLabelText("Console command") as HTMLInputElement).disabled).toBe(false);
  });

  it("asks before a command that changes something and confirms the exact command", async () => {
    const socket = new FakeSocket();
    await openWith(socket);
    type("config set agent.max_turns 3");
    socket.push({
      type: "confirm_required",
      id: 1,
      command: "config set agent.max_turns 3",
      message: "Change this setting?",
    });
    socket.push({ type: "complete", id: 1, status: "confirm_required" });
    expect(screen.getByRole("alertdialog").textContent).toContain("Change this setting?");
    fireEvent.click(screen.getByText("Run it"));
    expect(socket.sent.at(-1)).toEqual({
      type: "confirm",
      command: "config set agent.max_turns 3",
    });
    socket.push({ type: "complete", id: 1, status: "ok" });
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("cancel drops a pending confirmation", async () => {
    const socket = new FakeSocket();
    await openWith(socket);
    type("cron pause nightly");
    socket.push({ type: "confirm_required", id: 1, command: "cron pause nightly", message: "?" });
    fireEvent.click(screen.getByText("Cancel"));
    expect(socket.sent.at(-1)).toEqual({ type: "cancel" });
    socket.push({ type: "complete", id: 1, status: "cancelled" });
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.getByRole("log").textContent).toContain("Cancelled.");
  });

  it("stop sends cancel while a command runs; clear empties the log; exit closes", async () => {
    const socket = new FakeSocket();
    const { onClose } = await openWith(socket);
    type("sessions list");
    fireEvent.click(screen.getByText("Stop"));
    expect(socket.sent.at(-1)).toEqual({ type: "cancel" });
    socket.push({ type: "error", id: 1, message: "The command was cancelled." });
    socket.push({ type: "complete", id: 1, status: "cancelled" });
    expect(screen.getByRole("log").textContent).toContain("The command was cancelled.");
    socket.push({ type: "clear" });
    expect(screen.getByRole("log").textContent).toBe("");
    socket.push({ type: "complete", id: 2, status: "exit" });
    expect(onClose).toHaveBeenCalled();
  });

  it("explains a refused socket", async () => {
    const socket = new FakeSocket();
    const connect = vi.fn(async () => socket);
    render(<ConsoleModal open onClose={vi.fn()} connect={connect} />);
    await act(async () => {});
    act(() => socket.onclose?.({ code: 4404 }));
    expect(screen.getByRole("log").textContent).toContain("not enabled");
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
