import { describe, expect, it } from "vitest";
import { createDispatcher } from "../slash/dispatch.ts";
import { slashSetup } from "../slash/testContext.ts";
import { petCard } from "./pet.ts";

describe("/pet", () => {
  it("shows the pet's card in a pane", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("pet.info", () => ({
      enabled: true,
      slug: "otto",
      name: "Otto",
      scale: 1,
      moods: { walk: "data:", rest: "data:" },
    }));
    await createDispatcher(ctx).run("/pet");
    expect(out.pages[0]?.title).toBe("Pet");
    expect(out.pages[0]?.text).toContain("Otto (otto)");
    expect(out.pages[0]?.text).toContain("moods: rest, walk");
  });

  it("says how to make one when none is shown", () => {
    expect(petCard({ enabled: false })).toContain("indus pet generate");
  });

  it("use, off and list call the pet methods", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("pet.select", () => ({ slug: "otto", name: "Otto", shown: true }));
    server.handle("pet.disable", () => ({ enabled: false }));
    server.handle("pet.list", () => ({ pets: [{ slug: "otto", name: "Otto", shown: true }] }));
    await createDispatcher(ctx).run("/pet use otto");
    expect(server.calls("pet.select")[0]?.params).toEqual({ slug: "otto" });
    await createDispatcher(ctx).run("/pet off");
    await createDispatcher(ctx).run("/pet list");
    const all = [...out.lines, ...out.pages.map((p) => p.text)].join("\n");
    expect(all).toContain("Otto is shown now.");
    expect(all).toContain("The pet is hidden.");
    expect(all).toContain("* otto  Otto");
    await createDispatcher(ctx).run("/pet use");
    expect(out.lines.at(-1)).toBe("Use /pet use <slug> (see /pet list).");
  });
});
