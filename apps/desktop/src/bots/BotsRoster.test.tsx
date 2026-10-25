// @vitest-environment jsdom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { I18nProvider } from "../i18n/index.tsx";
import { BotsRoster, shortAgo } from "./BotsRoster.tsx";
import type { RpcLike } from "./canonicalChat.ts";

function fakeRpc() {
  const calls: Array<[string, Record<string, unknown>]> = [];
  const rpc: RpcLike = {
    request: async (method, params) => {
      calls.push([method, params]);
      if (method === "bots.list") {
        return {
          bots: [
            {
              profile: "scout",
              avatar: { kind: "face", seed: "s" },
              hidden: false,
              sections: [],
              routines: [],
              canonical: { session_id: "sess-1" },
            },
          ],
        };
      }
      if (method === "groups.list") return { rooms: [] };
      if (method === "bots.open") return { session_id: "sess-1" };
      return {};
    },
  };
  return { rpc, calls };
}

const flush = async () => {
  await act(async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });
};

describe("bots roster", () => {
  it("shows the preview from the sidebar, opens the chat, hides through the row menu", async () => {
    const { rpc, calls } = fakeRpc();
    const opened: string[] = [];
    render(
      <I18nProvider>
        <BotsRoster
          rpc={rpc}
          activeSessionId={null}
          onOpenChat={(id) => opened.push(id)}
          previewOf={(id) =>
            id === "sess-1" ? { text: "on it", lastActive: Date.now() - 15 * 60_000 } : null
          }
        />
      </I18nProvider>,
    );
    await flush();
    expect(screen.getByText("on it")).toBeTruthy();
    expect(screen.getByText("15m")).toBeTruthy();
    fireEvent.click(screen.getByText("scout"));
    await flush();
    expect(calls.some(([m]) => m === "bots.open")).toBe(true);
    expect(opened).toEqual(["sess-1"]);
    fireEvent.click(screen.getByRole("button", { name: "Bot actions for scout" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Hide" }));
    await flush();
    expect(calls.find(([m]) => m === "bots.update")?.[1]).toEqual({
      profile: "scout",
      hidden: true,
    });
  });

  it("short times", () => {
    const now = 10 * 86_400_000;
    expect(shortAgo(now - 30_000, now)).toBe("1m");
    expect(shortAgo(now - 3 * 3_600_000, now)).toBe("3h");
    expect(shortAgo(now - 2 * 86_400_000, now)).toBe("2d");
    expect(shortAgo(null, now)).toBe("");
  });
});
