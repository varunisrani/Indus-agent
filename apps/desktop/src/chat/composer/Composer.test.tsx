// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModelPrefs } from "../ModelPicker.tsx";
import { FakeConnection, MemoryStorage, rpcError } from "../testFakes.ts";
import {
  attachmentFromPath,
  MAX_ATTACHMENT_BYTES,
  stageAttachment,
  transportFor,
} from "./attachments.ts";
import { Composer, type ComposerProps } from "./Composer.tsx";
import { PromptHistory } from "./completion.ts";
import { DraftStore } from "./goneSession.ts";

afterEach(cleanup);

function chatDaemon(mode: "local" | "remote" = "local") {
  return new FakeConnection({ mode })
    .on("prompt.submit", () => ({ turn_id: "adm-1", generation: 4, status: "streaming" }))
    .on("image.attach", (params) => ({ name: "x", path: params.path, attached: 1 }))
    .on("session.interrupt", () => ({ interrupted: true }))
    .on("model.options", () => ({ provider: "", model: "", providers: [] }));
}

function mount(connection: FakeConnection, extra: Partial<ComposerProps> = {}) {
  const drafts = extra.drafts ?? new DraftStore();
  const props: ComposerProps = {
    sessionId: "s1",
    connection,
    journal: null,
    drafts,
    history: new PromptHistory(),
    prefs: new ModelPrefs(new MemoryStorage()),
    completionDebounceMs: 0,
    ...extra,
  };
  const view = render(<Composer {...props} />);
  const box = screen.getByLabelText("Message") as HTMLTextAreaElement;
  return { view, box, drafts, props };
}

/** The + menu's "Files…" row (W52-h: the attach button opens a menu). */
async function openFiles() {
  fireEvent.click(screen.getByRole("button", { name: "Attach" }));
  await act(async () => {
    fireEvent.click(screen.getByRole("menuitem", { name: "Files…" }));
  });
}

function type(box: HTMLTextAreaElement, value: string) {
  fireEvent.change(box, { target: { value } });
}

describe("composer", () => {
  it("Enter sends through prompt.submit and clears the draft", async () => {
    const connection = chatDaemon();
    const { box } = mount(connection);
    type(box, "hello there");
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    expect(connection.called("prompt.submit")[0]?.params).toMatchObject({
      session_id: "s1",
      text: "hello there",
    });
    expect(box.value).toBe("");
  });

  it("a fresh draft creates its session in the folder the sidebar chose (P20-25)", async () => {
    const connection = chatDaemon().on("session.create", () => ({ session_id: "s-new" }));
    const created = vi.fn();
    const { box } = mount(connection, {
      sessionId: null,
      newSessionCwd: "/work/repo",
      onSessionCreated: created,
    });
    type(box, "start here");
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    expect(connection.called("session.create")[0]?.params).toMatchObject({ cwd: "/work/repo" });
    expect(created).toHaveBeenCalledWith("s-new");
  });

  it("without a folder the new chat stays detached (no cwd sent)", async () => {
    const connection = chatDaemon().on("session.create", () => ({ session_id: "s-new" }));
    const { box } = mount(connection, { sessionId: null });
    type(box, "detached");
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    expect(connection.called("session.create")[0]?.params).not.toHaveProperty("cwd");
  });

  it("history recall works only in an empty composer", async () => {
    const connection = chatDaemon();
    const history = new PromptHistory();
    history.push("first prompt");
    history.push("second prompt");
    const { box } = mount(connection, { history });
    type(box, "typing");
    fireEvent.keyDown(box, { key: "ArrowUp" });
    expect(box.value).toBe("typing");
    type(box, "");
    fireEvent.keyDown(box, { key: "ArrowUp" });
    expect(box.value).toBe("second prompt");
    fireEvent.keyDown(box, { key: "ArrowUp" });
    expect(box.value).toBe("first prompt");
    fireEvent.keyDown(box, { key: "ArrowDown" });
    expect(box.value).toBe("second prompt");
    // an edited recall is the person's text now: arrows leave it alone
    type(box, "second prompt, edited");
    fireEvent.keyDown(box, { key: "ArrowUp" });
    expect(box.value).toBe("second prompt, edited");
  });

  it("slash completion comes from the daemon, curated, and Tab takes it", async () => {
    const connection = chatDaemon().on("complete.slash", () => ({
      items: [
        { text: "/quit", display: "/quit" },
        { text: "/usage", display: "/usage", meta: "token use" },
      ],
    }));
    const { box } = mount(connection);
    await act(async () => {
      type(box, "/u");
    });
    const options = await screen.findAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["/usagetoken use"]);
    fireEvent.keyDown(box, { key: "Tab" });
    expect(box.value).toBe("/usage ");
  });

  it("slash rows are grouped by catalogue category, plugin commands last (W52-h)", async () => {
    const connection = chatDaemon().on("complete.slash", () => ({
      items: [
        { text: "/agents", display: "/agents", meta: "background tasks" },
        { text: "/deploy", display: "/deploy", meta: "a plugin command" },
        { text: "/new", display: "/new", meta: "fresh session" },
      ],
    }));
    const { box } = mount(connection);
    await act(async () => {
      type(box, "/");
    });
    const options = await screen.findAllByRole("option");
    expect(options.map((o) => o.querySelector(".desk-popover-name")?.textContent)).toEqual([
      "/new",
      "/agents",
      "/deploy",
    ]);
    expect(options[0]?.getAttribute("aria-selected")).toBe("true");
    const heads = [...document.querySelectorAll(".desk-popover-head")].map((h) => h.textContent);
    expect(heads[0]).toBe("Session");
    expect(heads.at(-1)).toBe("Other");
  });

  it("@ in a fresh draft searches the draft's folder and shows an empty state (W52-h)", async () => {
    const connection = chatDaemon().on("complete.path", () => ({ items: [] }));
    const { box } = mount(connection, { sessionId: null, newSessionCwd: "/work/repo" });
    await act(async () => {
      type(box, "look at @src");
    });
    expect(connection.called("complete.path")[0]?.params).toMatchObject({
      word: "@src",
      cwd: "/work/repo",
    });
    expect(screen.getByRole("status").textContent).toContain("No matches");
    fireEvent.keyDown(box, { key: "Escape" });
    expect(screen.queryByText("No matches")).toBeNull();
  });

  it("a lone ? shows the help card; deleting it closes the card (W52-h)", () => {
    const { box } = mount(chatDaemon());
    type(box, "?");
    const help = screen.getByRole("region", { name: "Composer help" });
    expect(help.textContent).toContain("/resume");
    expect(help.textContent).toContain("Hotkeys");
    type(box, "");
    expect(screen.queryByRole("region", { name: "Composer help" })).toBeNull();
  });

  it("the + menu inserts a URL reference and lists Paste image disabled (W52-h)", async () => {
    const { box } = mount(chatDaemon(), { pickFiles: async () => [] });
    fireEvent.click(screen.getByRole("button", { name: "Attach" }));
    expect(
      screen.getByRole("menuitem", { name: "Paste image" }).getAttribute("aria-disabled"),
    ).toBe("true");
    fireEvent.click(screen.getByRole("menuitem", { name: "URL…" }));
    fireEvent.change(screen.getByLabelText("Address"), {
      target: { value: "https://example.com/a" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Add" }));
    });
    expect(box.value).toBe("@url:https://example.com/a ");
  });

  it("a slow answer for an older word never replaces the newer list", async () => {
    const releases: Array<() => void> = [];
    const connection = chatDaemon().on(
      "complete.path",
      (params) =>
        new Promise((resolve) => {
          releases.push(() =>
            resolve({
              items: [
                {
                  text: `${String(params.word).slice(1)}-match`,
                  display: `${String(params.word).slice(1)}-match`,
                },
              ],
            }),
          );
        }),
    );
    const { box } = mount(connection);
    await act(async () => {
      type(box, "look at @sr");
    });
    await act(async () => {
      type(box, "look at @src");
    });
    // the newer answer first, then the stale one
    await act(async () => releases[1]?.());
    await act(async () => releases[0]?.());
    const options = screen.getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["src-match"]);
  });

  it("while a turn runs: Stop interrupts the exact generation, Esc too", async () => {
    const connection = chatDaemon();
    const { box } = mount(connection);
    act(() => {
      connection.emit("s1", { type: "turn.started", turn_id: "adm-1", generation: 4 });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    });
    expect(connection.called("session.interrupt")[0]?.params).toEqual({
      session_id: "s1",
      generation: 4,
    });
    await act(async () => {
      fireEvent.keyDown(box, { key: "Escape" });
    });
    expect(connection.called("session.interrupt")).toHaveLength(2);
  });

  it("remote mode uploads bytes: no client path reaches the remote host", async () => {
    const connection = chatDaemon("remote");
    const uploadBytes = vi.fn(async (target: string) => `/remote${target}`);
    const readLocalBytes = vi.fn(async () => new Uint8Array([1, 2, 3]));
    const guardLocalPath = vi.fn(async () => {});
    const { box } = mount(connection, {
      cwd: "/srv/work",
      pickFiles: async () => ["/Users/me/Desktop/shot.png"],
      attachmentDeps: { uploadBytes, readLocalBytes, guardLocalPath },
    });
    await openFiles();
    // and one pasted file (never has a path)
    const pasted = new File([new Uint8Array([9, 9])], "notes.txt", { type: "text/plain" });
    fireEvent.paste(box, { clipboardData: { files: [pasted] } });
    type(box, "see these");
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    expect(readLocalBytes).toHaveBeenCalledWith("/Users/me/Desktop/shot.png");
    expect(guardLocalPath).not.toHaveBeenCalled();
    expect(uploadBytes).toHaveBeenCalledTimes(2);
    const attachedPath = String(connection.called("image.attach")[0]?.params.path);
    expect(attachedPath.startsWith("/remote/srv/work/.indus/attachments/")).toBe(true);
    const sent = String(connection.called("prompt.submit")[0]?.params.text);
    expect(sent).toContain("see these");
    expect(sent).toContain("@/remote/srv/work/.indus/attachments/");
    for (const call of connection.calls) {
      expect(JSON.stringify(call.params)).not.toContain("/Users/me");
    }
  });

  it("local mode passes a picked path after main's guard, without an upload", async () => {
    const connection = chatDaemon("local");
    const uploadBytes = vi.fn(async (target: string) => target);
    const guardLocalPath = vi.fn(async () => {});
    const { box } = mount(connection, {
      pickFiles: async () => ["/home/me/shot.png"],
      attachmentDeps: { uploadBytes, guardLocalPath, readLocalBytes: async () => new Uint8Array() },
    });
    await openFiles();
    type(box, "this one");
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    expect(guardLocalPath).toHaveBeenCalledWith("/home/me/shot.png");
    expect(uploadBytes).not.toHaveBeenCalled();
    expect(connection.called("image.attach")[0]?.params.path).toBe("/home/me/shot.png");
  });

  it("a refused attachment keeps the message unsent", async () => {
    const connection = chatDaemon("local");
    const { box } = mount(connection, {
      pickFiles: async () => ["/home/me/.env"],
      attachmentDeps: {
        guardLocalPath: async () => {
          throw new Error("refused: key store or .env file");
        },
      },
    });
    await openFiles();
    type(box, "secret stuff");
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    expect(screen.getByRole("alert").textContent).toContain("refused");
    expect(connection.called("prompt.submit")).toHaveLength(0);
    expect(box.value).toBe("secret stuff");
  });

  it("a session that vanished hands its text to the fresh draft once, with Undo", async () => {
    const connection = chatDaemon()
      .on("prompt.submit", () => {
        throw rpcError("SESSION_NOT_FOUND");
      })
      .on("session.history", () => {
        throw rpcError("SESSION_NOT_FOUND");
      });
    const onSessionGone = vi.fn();
    const drafts = new DraftStore();
    const { box, view, props } = mount(connection, { drafts, onSessionGone });
    type(box, "my unsent words");
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    await waitFor(() => expect(onSessionGone).toHaveBeenCalledWith("s1"));
    // the route shows the fresh draft
    view.rerender(<Composer {...props} sessionId={null} />);
    const fresh = screen.getByLabelText("Message") as HTMLTextAreaElement;
    expect(fresh.value).toBe("my unsent words");
    expect(screen.getByRole("status").textContent).toContain("moved here");
    expect(document.activeElement).not.toBe(fresh);
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(fresh.value).toBe("");
  });
});

describe("attachment transport", () => {
  it("bytes unless local mode with a known path", () => {
    const picked = attachmentFromPath("/a/b.png");
    expect(transportFor(picked, "local")).toBe("path");
    expect(transportFor(picked, "remote")).toBe("bytes");
  });

  it("refuses an attachment over 256 MiB before reading it", async () => {
    const huge = attachmentFromPath("/a/big.bin", MAX_ATTACHMENT_BYTES + 1);
    const readLocalBytes = vi.fn(async () => new Uint8Array());
    await expect(
      stageAttachment(huge, {
        mode: "remote",
        sessionId: "s1",
        cwd: null,
        request: (async () => ({})) as never,
        guardLocalPath: async () => {},
        readLocalBytes,
        uploadBytes: async (target) => target,
      }),
    ).rejects.toThrow(/256 MiB/);
    expect(readLocalBytes).not.toHaveBeenCalled();
  });
});
