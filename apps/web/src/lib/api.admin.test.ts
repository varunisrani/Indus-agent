// The admin pages' REST calls (P18-23..25) use the real route shapes: every call the client makes
// is one of the generated routes (method and path template), and is profile-scoped exactly when
// the route is.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GENERATED_ROUTES, type GeneratedRoute } from "../test/routes.generated.ts";
import { createMockApi, type MockApi } from "../testing/mockApi.ts";
import { api, setSelectedProfile } from "./api.ts";

let mock: MockApi;
let restore: () => void;

beforeEach(() => {
  mock = createMockApi();
  mock.state.platforms = [
    {
      id: "telegram",
      name: "Telegram",
      description: "",
      installed: true,
      install_hint: null,
      enabled: true,
      explicitly_disabled: false,
      configured: true,
      state: "connected",
      reason: null,
      daemon_running: true,
      fields: [],
    },
  ];
  mock.state.deviceGrant = {
    user_code: "C",
    verification_url: "https://x.example.org",
    expires_in: 60,
    interval: 5,
  };
  mock.state.auxiliary.tasks = [
    { task: "vision", provider: "auto", model: "", base_url: "", description: "" },
  ];
  restore = mock.install();
  setSelectedProfile("work");
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  restore();
});

function template(route: GeneratedRoute): RegExp {
  const body = route.path
    .split("/")
    .map((part) => (/^\{.+\}$/.test(part) ? "[^/]+" : part.replace(/[.*+?^$()|[\]\\]/g, "\\$&")))
    .join("/");
  return new RegExp(`^${body}$`);
}

function routeOf(method: string, path: string): GeneratedRoute | undefined {
  const candidates = GENERATED_ROUTES.filter((r) => r.method === method && template(r).test(path));
  // A literal path beats a template (`/api/profiles/active` over `/api/profiles/{name}`).
  return candidates.find((r) => !r.path.includes("{")) ?? candidates[0];
}

async function settle(run: () => Promise<unknown>): Promise<void> {
  try {
    await run();
  } catch {
    // Refusals are fine here: only the shape of the request is checked.
  }
}

describe("admin page calls", () => {
  it("only use generated routes, scoped exactly when the route is", async () => {
    const calls: Array<() => Promise<unknown>> = [
      () => api.profile.create({ name: "new-one" }),
      () => api.profile.setSticky("default"),
      () => api.profile.rename("new-one", "renamed"),
      () => api.profile.persona("default"),
      () => api.profile.setPersona("default", "x"),
      () => api.profile.setDescription("default", "x"),
      () => api.profile.setModel("default", "fake", "m"),
      () => api.profile.exportArchive("default"),
      () => api.profile.importArchive("/tmp/a.zip", "imp"),
      () => api.profile.remove("renamed"),
      () => api.actions.status("doctor"),
      () => api.actions.status("profile-export", 10, "default"),
      () => api.daemon.get(),
      () => api.daemon.start(),
      () => api.daemon.stop(),
      () => api.daemon.restart(),
      () => api.messaging.platforms(),
      () => api.messaging.update("telegram", { enabled: true }),
      () => api.messaging.test("telegram"),
      () => api.pairing.list(),
      () => api.pairing.approve({ platform: "telegram", code: "X" }),
      () => api.pairing.revoke("telegram", "1"),
      () => api.pairing.clearPending(),
      () => api.webhooks.list(),
      () => api.webhooks.create({ name: "hook" }),
      () => api.webhooks.setEnabled("hook", false),
      () => api.webhooks.setReceiver(true),
      () => api.webhooks.remove("hook"),
      () => api.model.info(),
      () => api.model.options(),
      () => api.model.auxiliary(),
      () => api.model.set({ scope: "main", provider: "fake", model: "m" }),
      async () => {
        const started = await api.providerLogin.start("fake");
        await api.providerLogin.poll(started.session_id);
        await api.providerLogin.cancel(started.session_id);
      },
      () => api.system.stats(),
      () => api.system.update(true),
      () => api.system.applyUpdate(),
      () => api.system.memory(),
      () => api.system.setMemoryProvider(""),
      () => api.system.pool(),
      () => api.system.addToPool({ provider: "fake", api_key: "k-123456789" }),
      () => api.system.removeFromPool("fake", 1),
      () => api.system.doctor(),
      () => api.system.backup(),
      () => api.system.importArchive("/tmp/a.zip", false),
      () => api.system.importUpload(new File(["x"], "a.zip"), true),
      () => api.system.checkpoints(),
      () => api.system.pruneCheckpoints(7, 500),
      () => api.system.hooks(),
      () => api.system.addHook({ event: "pre_tool_call", command: "x", consent: true }),
      () => api.system.removeHook("pre_tool_call", "x"),
      () => api.status(),
      () => api.skills.toggle("s", false, "default"),
      () => api.mcp.add({ name: "m", url: "https://m.example.org" }, "default"),
    ];
    for (const call of calls) await settle(call);

    expect(mock.calls.length).toBeGreaterThanOrEqual(calls.length);
    for (const call of mock.calls) {
      const route = routeOf(call.method, call.path);
      expect(route, `${call.method} ${call.path}`).toBeDefined();
      const profile = call.query.get("profile");
      if (route?.scoped) expect(profile, `${call.method} ${call.path}`).not.toBeNull();
      else expect(profile, `${call.method} ${call.path}`).toBeNull();
    }
  });

  it("aims a builder write at the named profile, not the selected one", async () => {
    await settle(() => api.skills.toggle("s", false, "fresh"));
    await settle(() => api.mcp.add({ name: "m", url: "https://m.example.org" }, "fresh"));
    await api.actions.status("profile-export", 5, "fresh");
    for (const call of mock.calls) expect(call.query.getAll("profile")).toEqual(["fresh"]);
  });
});
