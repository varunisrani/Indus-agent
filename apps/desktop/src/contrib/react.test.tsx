// @vitest-environment jsdom
import { act, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { ContributionArea } from "./react.tsx";
import { ContributionRegistry } from "./registry.ts";

describe("ContributionArea", () => {
  it("renders an area in order, follows changes, and drops a contribution that throws", () => {
    const registry = new ContributionRegistry();
    const quiet = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    render(createElement(ContributionArea, { area: "status-bar", registry }));
    act(() => {
      registry.register({
        id: "b",
        area: "status-bar",
        source: "plugin:x",
        render: () => createElement("span", null, "second"),
      });
      registry.register({
        id: "a",
        area: "status-bar",
        source: "plugin:x",
        order: -1,
        render: () => createElement("span", null, "first"),
      });
      registry.register({
        id: "c",
        area: "status-bar",
        source: "plugin:y",
        render: () => {
          throw new Error("broken plugin");
        },
      });
    });
    expect([...document.querySelectorAll("span")].map((s) => s.textContent)).toEqual([
      "first",
      "second",
    ]);
    act(() => registry.removeSource("plugin:x"));
    expect(screen.queryByText("first")).toBeNull();
    quiet.mockRestore();
    warn.mockRestore();
  });
});
