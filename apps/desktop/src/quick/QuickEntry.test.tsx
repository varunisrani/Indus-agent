// @vitest-environment jsdom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n/index.tsx";
import { UpdateNotice } from "../updates/UpdateNotice.tsx";
import { QuickEntry } from "./QuickEntry.tsx";

type Result = { ok: true; sessionId: string | null } | { ok: false; code: string; message: string };

function install(answers: Result[]) {
  const bridge = {
    quick: {
      submit: vi.fn(async () => answers.shift() as Result),
      dismiss: vi.fn(),
      onShown: () => () => undefined,
    },
    updates: { check: vi.fn(async () => ({})), apply: vi.fn(async () => ({ accepted: true })) },
    external: { open: vi.fn(async () => ({ ok: true })) },
  };
  (globalThis as { indusDesktop?: unknown }).indusDesktop = bridge;
  return bridge;
}

afterEach(() => {
  delete (globalThis as { indusDesktop?: unknown }).indusDesktop;
});

function renderQuick() {
  render(
    <I18nProvider>
      <QuickEntry />
    </I18nProvider>,
  );
  return screen.getByRole("textbox") as HTMLTextAreaElement;
}

describe("quick entry window", () => {
  it("Enter sends; a delivered message clears the box; the window is transparent", async () => {
    const bridge = install([{ ok: true, sessionId: "s1" }]);
    const box = renderQuick();
    expect(document.documentElement.dataset.window).toBe("quick");
    fireEvent.change(box, { target: { value: "hello" } });
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    expect(bridge.quick.submit).toHaveBeenCalledWith({ text: "hello" });
    expect(box.value).toBe("");
  });

  it("not confirmed yet: the text stays and the message says not to resend blindly", async () => {
    install([{ ok: false, code: "unconfirmed", message: "x" }]);
    const box = renderQuick();
    fireEvent.change(box, { target: { value: "keep me" } });
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    expect(box.value).toBe("keep me");
    expect(screen.getByRole("alert").textContent).toMatch(/not confirmed/);
  });

  it("Shift+Enter is a new line; Esc dismisses; an empty box is not sent", async () => {
    const bridge = install([]);
    const box = renderQuick();
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    expect(bridge.quick.submit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toMatch(/Type a message/);
    fireEvent.keyDown(box, { key: "Escape" });
    expect(bridge.quick.dismiss).toHaveBeenCalled();
  });
});

describe("update notice", () => {
  const status = (state: string, extra: Record<string, unknown> = {}) =>
    ({
      owner: "electron-updater",
      channel: "stable",
      state,
      currentVersion: "1.0.0",
      ...extra,
    }) as never;

  it("a stale renderer (lazy chunk 404) offers a reload instead of crashing", () => {
    install([]);
    render(
      <I18nProvider>
        <UpdateNotice
          status={null}
          skew={{ kind: "renderer-stale", detail: "" }}
          onDismissSkew={() => {}}
        />
      </I18nProvider>,
    );
    expect(screen.getByRole("alert").textContent).toMatch(/could not load/);
    expect(screen.getByRole("button", { name: "Reload window" })).toBeTruthy();
  });

  it("an available update applies on the person's click; the manual owner opens the page", () => {
    const bridge = install([]);
    const { rerender } = render(
      <I18nProvider>
        <UpdateNotice
          status={status("available", { version: "1.1.0" })}
          skew={null}
          onDismissSkew={() => {}}
        />
      </I18nProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Update and restart" }));
    expect(bridge.updates.apply).toHaveBeenCalled();
    rerender(
      <I18nProvider>
        <UpdateNotice
          status={status("manual", {
            owner: "manual",
            version: "1.1.0",
            downloadUrl: "https://example.invalid/r",
          })}
          skew={null}
          onDismissSkew={() => {}}
        />
      </I18nProvider>,
    );
    expect(screen.queryByRole("button", { name: "Update and restart" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open download page" }));
    expect(bridge.external.open).toHaveBeenCalledWith({ url: "https://example.invalid/r" });
    fireEvent.click(screen.getByRole("button", { name: "Later" }));
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("quick entry look (W52-m)", () => {
  it("one line, a Send to select with the current chat, no visible Send button", () => {
    install([]);
    const box = renderQuick();
    expect(box.tagName).toBe("INPUT");
    expect(screen.getByRole("combobox", { name: "Send to" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "Current chat" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
  });
});
