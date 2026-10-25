// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { SessionEvent } from "../../gateway/connectionStore.ts";
import {
  cleanDraft,
  commitMessagePrompt,
  type DraftConnection,
  draftWithAgent,
  trackTurnBase,
} from "./agentDraft.ts";
import { type GitBridge, ReviewPane } from "./ReviewPane.tsx";

const ROOT = "/home/me/proj";

function fakeGit() {
  const files = [
    { path: "README.md", index: " ", worktree: "M", added: 2, removed: 1 },
    { path: "src/app.ts", index: "M", worktree: " ", added: 1, removed: 0 },
    { path: "new.txt", index: "?", worktree: "?" },
  ];
  const git = {
    status: vi.fn(async () => ({
      root: ROOT,
      branch: "feature",
      upstream: "origin/feature",
      ahead: 2,
      behind: 1,
      head: "abc1234def",
      files,
      truncated: false,
    })),
    changes: vi.fn(async ({ scope }: { scope: string }) => ({
      files:
        scope === "branch"
          ? [{ path: "src/app.ts", index: "M", worktree: " ", added: 4, removed: 0 }]
          : [],
      base: scope === "branch" ? "0000aaa" : null,
      readOnly: true,
      truncated: false,
    })),
    diff: vi.fn(async () => ({
      diff: "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n",
      readOnly: false,
      truncated: false,
    })),
    stage: vi.fn(async () => ({ ok: true as const })),
    unstage: vi.fn(async () => ({ ok: true as const })),
    revert: vi.fn(async () => ({ ok: true as const })),
    commit: vi.fn(async () => ({ commit: "1234567890", pushed: false })),
    commitContext: vi.fn(async () => ({
      diff: "+new",
      untracked: ["new.txt"],
      recent: ["Add things"],
    })),
    createPr: vi.fn(async () => ({ url: null, ghAvailable: false })),
  };
  return { git, bridge: { git } as unknown as GitBridge };
}

describe("ReviewPane", () => {
  it("shows branch, ahead/behind and the uncommitted files; stage / unstage / revert call main", async () => {
    const { git, bridge } = fakeGit();
    render(<ReviewPane repo={`${ROOT}/src`} bridge={bridge} />);
    expect(await screen.findByText("On feature")).toBeTruthy();
    expect(screen.getByText("2 ahead, 1 behind origin/feature")).toBeTruthy();
    const readme = (await screen.findByText("README.md")).closest("li") as HTMLElement;
    fireEvent.click(within(readme).getByRole("button", { name: "Stage" }));
    await waitFor(() =>
      expect(git.stage).toHaveBeenCalledWith({ repo: ROOT, paths: ["README.md"] }),
    );
    // W52-m: a row shows the name, then its folder; the full path is the row's title
    const app = screen.getByTitle("src/app.ts").closest("li") as HTMLElement;
    fireEvent.click(within(app).getByRole("button", { name: "Unstage" }));
    await waitFor(() =>
      expect(git.unstage).toHaveBeenCalledWith({ repo: ROOT, paths: ["src/app.ts"] }),
    );
    // revert asks first
    fireEvent.click(within(readme).getByRole("button", { name: "Revert" }));
    const dialog = screen.getByRole("alertdialog");
    expect(git.revert).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Discard" }));
    await waitFor(() =>
      expect(git.revert).toHaveBeenCalledWith({ repo: ROOT, paths: ["README.md"] }),
    );
  });

  it("a file's diff, coloured by line", async () => {
    const { git, bridge } = fakeGit();
    render(<ReviewPane repo={ROOT} bridge={bridge} />);
    fireEvent.click(await screen.findByText("README.md"));
    const diff = await screen.findByTestId("review-diff");
    expect(git.diff).toHaveBeenCalledWith({ repo: ROOT, scope: "uncommitted", path: "README.md" });
    expect(within(diff).getByText("+new").className).toContain("text-ok");
    expect(within(diff).getByText("-old").className).toContain("text-danger");
    // a staged-only file asks for the staged diff
    fireEvent.click(screen.getByTitle("src/app.ts"));
    await waitFor(() =>
      expect(git.diff).toHaveBeenLastCalledWith({
        repo: ROOT,
        scope: "uncommitted",
        path: "src/app.ts",
        staged: true,
      }),
    );
  });

  it("branch and last-turn scopes are read-only (no stage, revert or commit)", async () => {
    const { git, bridge } = fakeGit();
    render(<ReviewPane repo={ROOT} bridge={bridge} lastTurnBase="feedbeef1" />);
    await screen.findByText("README.md");
    fireEvent.click(screen.getByRole("radio", { name: "Branch" }));
    await waitFor(() => expect(git.changes).toHaveBeenCalledWith({ repo: ROOT, scope: "branch" }));
    expect(await screen.findByText("Read-only view")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Stage" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Revert" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Commit" })).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "Last turn" }));
    await waitFor(() =>
      expect(git.changes).toHaveBeenCalledWith({
        repo: ROOT,
        scope: "last-turn",
        base: "feedbeef1",
      }),
    );
    expect(await screen.findByText("Nothing to compare against yet.")).toBeTruthy();
  });

  it("generate fills the message from the agent; commit & push", async () => {
    const { git, bridge } = fakeGit();
    const draft = vi.fn(async (_prompt: string) => "Tidy the readme");
    render(<ReviewPane repo={ROOT} bridge={bridge} draftMessage={draft} />);
    await screen.findByText("README.md");
    fireEvent.click(screen.getByRole("button", { name: "Write it for me" }));
    await waitFor(() =>
      expect(
        (screen.getByRole("textbox", { name: "Commit message" }) as HTMLTextAreaElement).value,
      ).toBe("Tidy the readme"),
    );
    expect(draft.mock.calls[0]?.[0]).toContain("new.txt");
    git.commit.mockResolvedValueOnce({ commit: "abcdef1234", pushed: true });
    // W52-m: the split button's menu holds "Commit & push"
    fireEvent.click(screen.getByRole("button", { name: "More commit options" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Commit & push" }));
    await waitFor(() =>
      expect(git.commit).toHaveBeenCalledWith({
        repo: ROOT,
        message: "Tidy the readme",
        push: true,
      }),
    );
    expect(await screen.findByText("Committed abcdef1 and pushed.")).toBeTruthy();
  });

  it("gh absent → offer shown to ask the agent", async () => {
    const { git, bridge } = fakeGit();
    const askAgent = vi.fn((_prompt: string) => {});
    render(<ReviewPane repo={ROOT} bridge={bridge} askAgent={askAgent} />);
    await screen.findByText("README.md");
    fireEvent.click(screen.getByRole("button", { name: "Create pull request" }));
    const title = screen.getByRole("textbox", { name: "Pull request title" });
    fireEvent.change(title, { target: { value: "Better readme" } });
    fireEvent.submit(title);
    await waitFor(() =>
      expect(git.createPr).toHaveBeenCalledWith({ repo: ROOT, title: "Better readme" }),
    );
    expect(await screen.findByText("The GitHub CLI is not installed here.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Ask the agent to open it" }));
    expect(askAgent.mock.calls[0]?.[0]).toContain("Better readme");
  });

  it("a folder outside git says so", async () => {
    const { git, bridge } = fakeGit();
    git.status.mockRejectedValueOnce(
      new Error("not-a-repo: that folder is not in a git repository"),
    );
    render(<ReviewPane repo="/home/me/plain" bridge={bridge} />);
    expect((await screen.findByRole("alert")).textContent).toBe(
      "This folder is not a git repository.",
    );
  });

  it("tree view groups by folder", async () => {
    const { bridge } = fakeGit();
    render(<ReviewPane repo={ROOT} bridge={bridge} />);
    await screen.findByTitle("src/app.ts");
    fireEvent.click(screen.getByRole("radio", { name: "Tree" }));
    expect(screen.getByText("src")).toBeTruthy();
    expect(screen.getByText("app.ts")).toBeTruthy();
  });
});

describe("asking the agent", () => {
  function fakeConnection() {
    const listeners = new Map<string, (event: SessionEvent) => void>();
    const request = vi.fn(async (method: string, _params: unknown) => {
      if (method === "prompt.background") return { task_id: "side-1" };
      return { session_id: "side-1", replay_epoch: "e" };
    });
    const connection = {
      request,
      onSessionEvent: (id: string, listener: (event: SessionEvent) => void) => {
        listeners.set(id, listener);
        return () => listeners.delete(id);
      },
    } as unknown as DraftConnection;
    const emit = (id: string, event: Record<string, unknown>) =>
      listeners.get(id)?.(event as unknown as SessionEvent);
    return { connection, request, emit, listeners };
  }

  it("prompt.background, then the side session's message.complete (replayed from 0)", async () => {
    const { connection, request, emit, listeners } = fakeConnection();
    const pending = draftWithAgent(connection, "s1", "write it");
    await waitFor(() => expect(listeners.has("side-1")).toBe(true));
    expect(request).toHaveBeenCalledWith("prompt.background", {
      session_id: "s1",
      text: "write it",
    });
    expect(request).toHaveBeenCalledWith("session.subscribe", { session_id: "side-1", cursor: 0 });
    act(() => emit("side-1", { type: "message.delta", text: "x" }));
    act(() => emit("side-1", { type: "message.complete", text: "```\nFix the thing\n```" }));
    expect(await pending).toBe("Fix the thing");
    expect(listeners.size).toBe(0);
  });

  it("gives up after its deadline", async () => {
    const { connection } = fakeConnection();
    let fire: (() => void) | null = null;
    const pending = draftWithAgent(connection, "s1", "x", {
      setTimeout: (fn) => {
        fire = fn;
        return 1;
      },
      clearTimeout: () => {},
    });
    await waitFor(() => expect(fire).not.toBeNull());
    (fire as unknown as () => void)();
    await expect(pending).rejects.toThrow(/did not answer/);
  });

  it("the prompt carries the diff, new files and recent subjects; drafts are cleaned", () => {
    const prompt = commitMessagePrompt({ diff: "+a\n", untracked: ["b.txt"], recent: ["Add a"] });
    expect(prompt).toContain("- b.txt");
    expect(prompt).toContain("- Add a");
    expect(prompt).toContain("```diff\n+a\n```");
    expect(cleanDraft("  Subject\n\nBody  ")).toBe("Subject\n\nBody");
  });

  it("last-turn base: HEAD recorded at each turn.started", async () => {
    const { connection, emit } = fakeConnection();
    const bases: string[] = [];
    const stop = trackTurnBase(
      connection,
      "s1",
      async () => "abc1234",
      (head) => bases.push(head),
    );
    emit("s1", { type: "message.delta" });
    emit("s1", { type: "turn.started" });
    await waitFor(() => expect(bases).toEqual(["abc1234"]));
    stop();
  });
});
