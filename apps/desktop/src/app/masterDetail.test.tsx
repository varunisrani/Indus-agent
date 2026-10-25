// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { visibleSkills } from "./capabilities/SkillsTab.tsx";
import { BottomSection, clamp, MasterDetail } from "./masterDetail.tsx";

afterEach(() => window.localStorage.clear());

describe("master-detail frame", () => {
  it("clamps the seam between the list and detail minimums", () => {
    expect(clamp(100, 180, 900)).toBe(180);
    expect(clamp(1200, 180, 900)).toBe(900);
    expect(clamp(400, 180, 900)).toBe(400);
    expect(clamp(400, 180, 100)).toBe(180); // an empty range keeps the list minimum
  });

  it("uses a remembered list width and forgets it on double-click", () => {
    window.localStorage.setItem("indus.masterDetail.t1", "300");
    const view = render(<MasterDetail id="t1" list={<p>list</p>} detail={<p>detail</p>} />);
    const frame = view.container.querySelector(".md-frame") as HTMLElement;
    expect(frame.style.gridTemplateColumns).toBe("300px minmax(0, 1fr)");
    fireEvent.doubleClick(view.container.querySelector(".md-sash") as HTMLElement);
    expect(frame.style.gridTemplateColumns).toBe("minmax(0, 0.75fr) minmax(0, 1fr)");
    expect(window.localStorage.getItem("indus.masterDetail.t1")).toBeNull();
  });

  it("the rail variant has a fixed list and no seam", () => {
    const view = render(
      <MasterDetail id="t2" variant="rail" list={<p>list</p>} detail={<p>detail</p>} />,
    );
    expect(view.container.querySelector(".md-sash")).toBeNull();
    expect(
      (view.container.querySelector(".md-frame") as HTMLElement).style.gridTemplateColumns,
    ).toBe("224px minmax(0, 1fr)");
  });

  it("a bottom section collapses and remembers it", () => {
    const section = () => (
      <BottomSection id="hub" label="Hub" hideLabel="Hide it" showLabel="Show it">
        <p>inside</p>
      </BottomSection>
    );
    const first = render(section());
    expect(screen.getByText("inside")).toBeTruthy();
    fireEvent.click(screen.getByText("Hide it"));
    expect(screen.queryByText("inside")).toBeNull();
    first.unmount();
    render(section());
    expect(screen.queryByText("inside")).toBeNull();
    expect(screen.getByText("Show it")).toBeTruthy();
  });

  it("skills sort by use, then name, and filter by name, description and category", () => {
    const rows = [
      { name: "b", description: "", category: "notes", enabled: true, source: "local" },
      { name: "a", description: "brew", category: "", enabled: true, source: "local" },
      { name: "c", description: "", category: "", enabled: false, source: "hub" },
    ];
    const usage = new Map([["c", { useCount: 5, createdBy: "agent" as const }]]);
    expect(visibleSkills(rows, usage, "", true).map((r) => r.name)).toEqual(["c", "a", "b"]);
    expect(visibleSkills(rows, usage, "", false).map((r) => r.name)).toEqual(["a", "b", "c"]);
    expect(visibleSkills(rows, usage, "brew", true).map((r) => r.name)).toEqual(["a"]);
    expect(visibleSkills(rows, usage, "NOTES", true).map((r) => r.name)).toEqual(["b"]);
  });
});
