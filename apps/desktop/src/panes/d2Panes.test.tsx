// @vitest-environment jsdom
import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { previewTargets, registerWorkingPanes } from "./d2Panes.tsx";
import { type PaneRenderContext, paneRegistry } from "./PaneShell.tsx";

const context: PaneRenderContext = {
  sessionId: null,
  profile: null,
  active: true,
  workspace: null,
  mode: "local",
  hidden: false,
  connection: null,
};

const paneDefinition = (id: string) => paneRegistry.get(id);

describe("working panes registration", () => {
  it("registers preview, files, review and terminal; preview and terminal are stateful", () => {
    registerWorkingPanes();
    registerWorkingPanes();
    expect(paneRegistry.ids()).toEqual(["chat", "preview", "files", "review", "terminal"]);
    expect(
      ["preview", "files", "review", "terminal"].map((id) => [id, paneRegistry.isStateful(id)]),
    ).toEqual([
      ["preview", true],
      ["files", false],
      ["review", false],
      ["terminal", true],
    ]);
  });

  it("each pane renders from the shell's context (no bridge in a plain page)", () => {
    registerWorkingPanes();
    for (const kind of ["files", "review"] as const) {
      const view = render(<div>{paneDefinition(kind)?.render(context)}</div>);
      expect(screen.getByText("This conversation has no working folder yet.")).toBeTruthy();
      view.unmount();
    }
    render(<div>{paneDefinition("terminal")?.render({ ...context, mode: "remote" })}</div>);
    expect(screen.getByText(/connected to a remote Indus/)).toBeTruthy();
  });

  it("the preview follows the shared target when the shell has no store of its own", () => {
    registerWorkingPanes();
    render(<div>{paneDefinition("preview")?.render(context)}</div>);
    expect(screen.getByText(/Nothing to preview yet/)).toBeTruthy();
    act(() => previewTargets.set({ kind: "file", path: "/srv/remote.txt" }));
    expect((screen.getByRole("textbox", { name: "Address" }) as HTMLInputElement).value).toBe(
      "/srv/remote.txt",
    );
    act(() => previewTargets.set(null));
  });
});
