// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type TerminalBridge, TerminalPane } from "./TerminalPane.tsx";
import type { TerminalView } from "./xtermView.ts";

function fakeTerminal() {
  const data = new Set<(event: { terminalId: string; data: string }) => void>();
  const exits = new Set<
    (event: { terminalId: string; exitCode: number | null; signal: string | null }) => void
  >();
  let n = 0;
  const terminal = {
    start: vi.fn(async (_req: { cwd?: string; cols: number; rows: number }) => {
      n += 1;
      // output that races ahead of the answer
      for (const l of [...data]) l({ terminalId: `t${n}`, data: `hello from ${n}\r\n` });
      return { terminalId: `t${n}`, pid: 1000 + n, shell: "bash", cwd: "/home/me/proj" };
    }),
    write: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn(async () => ({ ok: true as const })),
    onData: vi.fn((listener: (event: { terminalId: string; data: string }) => void) => {
      data.add(listener);
      return () => data.delete(listener);
    }),
    onExit: vi.fn(
      (
        listener: (event: {
          terminalId: string;
          exitCode: number | null;
          signal: string | null;
        }) => void,
      ) => {
        exits.add(listener);
        return () => exits.delete(listener);
      },
    ),
  };
  const emitExit = (terminalId: string, exitCode: number) => {
    for (const l of [...exits]) l({ terminalId, exitCode, signal: null });
  };
  const emitData = (terminalId: string, text: string) => {
    for (const l of [...data]) l({ terminalId, data: text });
  };
  return { terminal, emitExit, emitData, bridge: { terminal } as unknown as TerminalBridge };
}

function fakeViews() {
  const views: Array<{
    written: string[];
    input: ((data: string) => void) | null;
    selection: ((text: string) => void) | null;
    disposed: boolean;
  }> = [];
  const createView = vi.fn(async (): Promise<TerminalView> => {
    const record = {
      written: [] as string[],
      input: null as ((d: string) => void) | null,
      selection: null as ((t: string) => void) | null,
      disposed: false,
    };
    views.push(record);
    return {
      open: () => {},
      write: (d) => record.written.push(d),
      onInput: (l) => {
        record.input = l;
      },
      onSelectionChange: (l) => {
        record.selection = l;
      },
      fit: () => ({ cols: 90, rows: 30 }),
      focus: () => {},
      dispose: () => {
        record.disposed = true;
      },
    };
  });
  return { views, createView };
}

describe("TerminalPane", () => {
  it("starts one shell in the session folder; early output is not lost; typing goes to it", async () => {
    const { terminal, bridge } = fakeTerminal();
    const { views, createView } = fakeViews();
    render(
      <TerminalPane cwd="/home/me/proj" mode="local" bridge={bridge} createView={createView} />,
    );
    await waitFor(() =>
      expect(terminal.start).toHaveBeenCalledWith({ cwd: "/home/me/proj", cols: 90, rows: 30 }),
    );
    await waitFor(() => expect(views[0]?.written).toEqual(["hello from 1\r\n"]));
    act(() => views[0]?.input?.("ls\r"));
    expect(terminal.write).toHaveBeenCalledWith({ terminalId: "t1", data: "ls\r" });
    expect(await screen.findByRole("tab", { name: "bash" })).toBeTruthy();
  });

  it("hide/show keeps the same terminal and pid (no restart, no dispose)", async () => {
    const { terminal, bridge } = fakeTerminal();
    const { createView } = fakeViews();
    const view = render(
      <TerminalPane cwd={null} mode="local" bridge={bridge} createView={createView} />,
    );
    await waitFor(() => expect(terminal.start).toHaveBeenCalledTimes(1));
    const started = await terminal.start.mock.results[0]?.value;
    view.rerender(
      <TerminalPane cwd={null} mode="local" bridge={bridge} createView={createView} hidden />,
    );
    expect(screen.getByTestId("terminal-pane").hasAttribute("inert")).toBe(true);
    view.rerender(<TerminalPane cwd={null} mode="local" bridge={bridge} createView={createView} />);
    await waitFor(() =>
      expect(terminal.resize).toHaveBeenCalledWith({ terminalId: "t1", cols: 90, rows: 30 }),
    );
    expect(terminal.start).toHaveBeenCalledTimes(1);
    expect(terminal.dispose).not.toHaveBeenCalled();
    expect(started).toMatchObject({ pid: 1001 });
    expect(createView).toHaveBeenCalledTimes(1);
  });

  it("several terminals in a rail; a background one keeps its screen", async () => {
    const { terminal, bridge, emitData } = fakeTerminal();
    const { views, createView } = fakeViews();
    render(<TerminalPane cwd={null} mode="local" bridge={bridge} createView={createView} />);
    await waitFor(() => expect(terminal.start).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "New terminal" }));
    await waitFor(() => expect(terminal.start).toHaveBeenCalledTimes(2));
    expect(screen.getAllByTestId("terminal-screen")).toHaveLength(2);
    act(() => emitData("t1", "still here"));
    expect(views[0]?.written).toContain("still here");
    expect(views[1]?.written).not.toContain("still here");
  });

  it("close is guarded while the shell runs; dispose kills it", async () => {
    const { terminal, bridge, emitExit } = fakeTerminal();
    const { createView, views } = fakeViews();
    render(<TerminalPane cwd={null} mode="local" bridge={bridge} createView={createView} />);
    await screen.findByRole("tab", { name: "bash" });
    fireEvent.click(screen.getByRole("button", { name: "Close bash" }));
    const dialog = screen.getByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Keep it" }));
    expect(terminal.dispose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Close bash" }));
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", { name: "Close it" }),
    );
    await waitFor(() => expect(terminal.dispose).toHaveBeenCalledWith({ terminalId: "t1" }));
    expect(views[0]?.disposed).toBe(true);
    // an exited shell closes without asking, and offers Restart while open
    fireEvent.click(screen.getByRole("button", { name: "New terminal" }));
    await screen.findByRole("tab", { name: "bash" });
    act(() => emitExit("t2", 0));
    expect(await screen.findByText("The shell exited (code 0).")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Restart" }));
    await waitFor(() => expect(terminal.start).toHaveBeenCalledTimes(3));
    act(() => emitExit("t3", 1));
    fireEvent.click(await screen.findByRole("button", { name: "Close bash" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("add selection to chat", async () => {
    const { bridge } = fakeTerminal();
    const { createView, views } = fakeViews();
    const onAddToChat = vi.fn();
    render(
      <TerminalPane
        cwd={null}
        mode="local"
        bridge={bridge}
        createView={createView}
        onAddToChat={onAddToChat}
      />,
    );
    await waitFor(() => expect(views[0]?.selection).not.toBeNull());
    expect(screen.queryByRole("button", { name: "Add selection to chat" })).toBeNull();
    act(() => views[0]?.selection?.("error: boom"));
    fireEvent.click(screen.getByRole("button", { name: "Add selection to chat" }));
    expect(onAddToChat).toHaveBeenCalledWith("error: boom");
  });

  it("not-available is shown as a message; remote mode has no local terminal", async () => {
    const { terminal, bridge } = fakeTerminal();
    terminal.start.mockRejectedValueOnce(
      new Error("not-available: terminals are not available in this build"),
    );
    const { createView } = fakeViews();
    const view = render(
      <TerminalPane cwd={null} mode="local" bridge={bridge} createView={createView} />,
    );
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Terminals are not available: terminals are not available in this build",
    );
    view.unmount();
    render(<TerminalPane cwd={null} mode="remote" bridge={bridge} createView={createView} />);
    expect(screen.getByText(/connected to a remote Indus/)).toBeTruthy();
  });

  it("unmounting the pane disposes every shell", async () => {
    const { terminal, bridge } = fakeTerminal();
    const { createView } = fakeViews();
    const view = render(
      <TerminalPane cwd={null} mode="local" bridge={bridge} createView={createView} />,
    );
    await screen.findByRole("tab", { name: "bash" });
    view.unmount();
    expect(terminal.dispose).toHaveBeenCalledWith({ terminalId: "t1" });
  });
});
