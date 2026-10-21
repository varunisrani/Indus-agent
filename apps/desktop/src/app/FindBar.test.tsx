// @vitest-environment jsdom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { I18nProvider } from "../i18n/index.tsx";
import { FindBar, isFindKey } from "./FindBar.tsx";
import {
  findRanges,
  HIGHLIGHT_ACTIVE,
  HIGHLIGHT_ALL,
  type HighlightRegistry,
  TranscriptFinder,
} from "./findInTranscript.ts";

function transcript(html: string): HTMLElement {
  const root = document.createElement("div");
  root.className = "transcript";
  root.innerHTML = html;
  document.body.appendChild(root);
  return root;
}

const FIXTURE = `
  <div class="msg msg-user"><span class="sr-only">You</span><div class="msg-bubble">Hello there, hello again</div></div>
  <div class="msg msg-assistant"><span class="sr-only">Assistant</span><p>Say <b>HELLO</b> back.</p></div>
  <div data-find-skip><input value="hello" /><span>hello in my own UI</span></div>
`;

function recorder() {
  const sets: Array<[string, number]> = [];
  const deletes: string[] = [];
  const registry: HighlightRegistry = {
    set: (name, ranges) => sets.push([name, ranges.length]),
    delete: (name) => deletes.push(name),
  };
  return { registry, sets, deletes };
}

describe("findRanges", () => {
  it("finds every visible match, ignoring case, hidden labels and skipped UI", () => {
    const root = transcript(FIXTURE);
    const ranges = findRanges(root, "hello");
    expect(ranges.map((r) => r.toString())).toEqual(["Hello", "hello", "HELLO"]);
    expect(findRanges(root, "you")).toEqual([]);
    expect(findRanges(root, "Hello", { matchCase: true })).toHaveLength(1);
    expect(findRanges(root, "   ")).toEqual([]);
    root.remove();
  });
});

describe("TranscriptFinder", () => {
  it("counts, steps with wrap-around, paints and clears", () => {
    const root = transcript(FIXTURE);
    const { registry, sets, deletes } = recorder();
    const finder = new TranscriptFinder({ root: () => root, highlights: registry });
    expect(finder.search("hello")).toEqual({ query: "hello", total: 3, current: 1 });
    expect(sets).toEqual([
      [HIGHLIGHT_ALL, 3],
      [HIGHLIGHT_ACTIVE, 1],
    ]);
    expect(finder.next().current).toBe(2);
    expect(finder.next().current).toBe(3);
    expect(finder.next().current).toBe(1);
    expect(finder.previous().current).toBe(3);
    finder.clear();
    expect(deletes).toEqual([HIGHLIGHT_ALL, HIGHLIGHT_ACTIVE]);
    root.remove();
  });

  it("refresh keeps the active match while new rows stream in", () => {
    const root = transcript(FIXTURE);
    const finder = new TranscriptFinder({ root: () => root, highlights: null });
    finder.search("hello");
    finder.next();
    const row = document.createElement("p");
    row.textContent = "and hello once more";
    root.appendChild(row);
    expect(finder.refresh()).toEqual({ query: "hello", total: 4, current: 2 });
    root.remove();
  });
});

describe("FindBar", () => {
  it("opens on Ctrl/Cmd+F only", () => {
    const base = { ctrlKey: false, metaKey: false, altKey: false, shiftKey: false };
    expect(isFindKey({ ...base, key: "f", ctrlKey: true })).toBe(true);
    expect(isFindKey({ ...base, key: "F", metaKey: true })).toBe(true);
    expect(isFindKey({ ...base, key: "f" })).toBe(false);
    expect(isFindKey({ ...base, key: "f", ctrlKey: true, shiftKey: true })).toBe(false);
  });

  it("highlights the count, steps with Enter / Shift+Enter and closes with Esc", async () => {
    const root = transcript(FIXTURE);
    const { registry, sets, deletes } = recorder();
    render(
      <I18nProvider>
        <FindBar root={() => root} highlights={registry} bridge={null} />
      </I18nProvider>,
    );
    expect(screen.queryByRole("searchbox")).toBeNull();
    fireEvent.keyDown(window, { key: "f", ctrlKey: true });
    const input = await screen.findByRole("searchbox", { name: "Find in conversation" });
    expect(document.activeElement).toBe(input);

    fireEvent.change(input, { target: { value: "hello" } });
    expect(screen.getByText("1 of 3")).toBeTruthy();
    expect(sets).toContainEqual([HIGHLIGHT_ALL, 3]);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByText("2 of 3")).toBeTruthy();
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(screen.getByText("3 of 3")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByText("1 of 3")).toBeTruthy();

    fireEvent.change(input, { target: { value: "nowhere" } });
    expect(screen.getByText("No results")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Next match" }) as HTMLButtonElement).disabled).toBe(
      true,
    );

    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("searchbox")).toBeNull();
    expect(deletes).toContain(HIGHLIGHT_ALL);
    root.remove();
  });

  it("opens when main forwards the Linux shortcut", async () => {
    let open: ((payload: Record<string, never>) => void) | null = null;
    const bridge = {
      find: {
        onOpen: (listener: (payload: Record<string, never>) => void) => {
          open = listener;
          return () => {
            open = null;
          };
        },
      },
    };
    render(
      <I18nProvider>
        <FindBar root={() => null} highlights={null} bridge={bridge} />
      </I18nProvider>,
    );
    expect(open).not.toBeNull();
    act(() => (open as ((payload: Record<string, never>) => void) | null)?.({}));
    expect(await screen.findByRole("searchbox")).toBeTruthy();
  });
});
