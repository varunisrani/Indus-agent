// @vitest-environment jsdom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { fakeRest } from "../settings/fakeRest.ts";
import { isProviderFailure, Onboarding, type Timers } from "./Onboarding.tsx";
import { requestOnboarding } from "./requests.ts";

const OPTIONS = (configured: boolean) => ({
  providers: [
    {
      name: "openrouter",
      label: "OpenRouter",
      auth_type: "api_key",
      configured,
      device_login: false,
      family: "vendor",
    },
    {
      name: "acme",
      label: "Acme Cloud",
      auth_type: "oauth",
      configured: false,
      device_login: true,
      family: "vendor",
    },
  ],
  current: { provider: null, model: null },
});

/** Timers that run only when the test says so. */
function manualTimers() {
  const queue: Array<{ fn: () => void; id: number }> = [];
  let next = 1;
  const timers: Timers = {
    setTimeout: (fn) => {
      const id = next++;
      queue.push({ fn, id });
      return id;
    },
    clearTimeout: (handle) => {
      const index = queue.findIndex((item) => item.id === handle);
      if (index >= 0) queue.splice(index, 1);
    },
  };
  return {
    timers,
    async flush() {
      const items = queue.splice(0);
      await act(async () => {
        for (const item of items) item.fn();
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
    },
  };
}

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function events() {
  const listeners = new Set<(event: unknown) => void>();
  return {
    source: (listener: (event: unknown) => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    emit: (event: unknown) => {
      for (const listener of listeners) listener(event);
    },
  };
}

describe("onboarding overlay", () => {
  it("stays out of the way when a provider is configured", async () => {
    const api = fakeRest();
    api.route("GET", "/api/model/options", OPTIONS(true));
    render(<Onboarding ready api={api} saveKey={null} />);
    await settle();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("waits for a usable backend before asking", async () => {
    const api = fakeRest();
    api.route("GET", "/api/model/options", OPTIONS(false));
    const { rerender } = render(<Onboarding ready={false} api={api} saveKey={null} />);
    await settle();
    expect(api.calls).toEqual([]);
    rerender(<Onboarding ready api={api} saveKey={null} />);
    await settle();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("choose later dismisses; it comes back only on a provider error", async () => {
    const api = fakeRest();
    api.route("GET", "/api/model/options", OPTIONS(false));
    const bus = events();
    render(<Onboarding ready api={api} saveKey={null} events={bus.source} />);
    await settle();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.getByRole("dialog")).toBeTruthy(); // Esc does not dismiss the first run
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Choose later" }));
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    await act(async () => {
      bus.emit({ type: "turn.done", session_id: "s", reason: "completed", failure: null });
      bus.emit({ type: "message.delta", session_id: "s" });
    });
    await settle();
    expect(screen.queryByRole("dialog")).toBeNull();
    await act(async () => {
      bus.emit({
        type: "turn.done",
        session_id: "s",
        reason: "provider_failed",
        failure: { reason: "No API key is configured for this provider" },
      });
    });
    await settle();
    expect(screen.getByRole("dialog")).toBeTruthy();
    // nothing about the dismissal was stored
    expect(window.localStorage.length).toBe(0);
  });

  it("Settings can open it even when configured, and it can then be closed", async () => {
    const api = fakeRest();
    api.route("GET", "/api/model/options", OPTIONS(true));
    render(<Onboarding ready api={api} saveKey={null} />);
    await settle();
    await act(async () => {
      requestOnboarding({ reason: "manual" });
    });
    await settle();
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Choose later" })).toBeNull();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Close" }));
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("a key is saved through the backend, never in renderer storage, then the model is confirmed", async () => {
    const api = fakeRest();
    api.route("GET", "/api/model/options", OPTIONS(false));
    api.route("POST", "/api/model/set", { ok: true });
    const saveKey = vi.fn(async () => undefined);
    render(<Onboarding ready api={api} saveKey={saveKey} />);
    await settle();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Paste an API key/ }));
    });
    const key = ["fake", "provider", "key", String(Date.now())].join("-");
    const input = screen.getByLabelText("API key") as HTMLInputElement;
    fireEvent.change(input, { target: { value: key } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save the key" }));
    });
    await settle();
    expect(saveKey).toHaveBeenCalledWith("openrouter", key);
    expect(JSON.stringify(window.localStorage)).not.toContain(key);
    expect(JSON.stringify(window.sessionStorage)).not.toContain(key);
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "some-model" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Start chatting" }));
    });
    await settle();
    const set = api.calls.find((c) => c.path === "/api/model/set");
    expect(set?.body).toEqual({ scope: "main", provider: "openrouter", model: "some-model" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("device sign-in shows the code with copy, polls, then confirms the model", async () => {
    const api = fakeRest();
    const clock = manualTimers();
    api.route("GET", "/api/model/options", OPTIONS(false));
    api.route("POST", "/api/providers/login/start", {
      session_id: "login-1",
      user_code: "WXYZ-1234",
      verification_url: "https://acme.example/activate",
      interval: 5,
    });
    let status = "pending";
    api.route("GET", "/api/providers/login/login-1", () => ({
      status: 200,
      body: { status, message: "" },
    }));
    const copy = vi.fn(async () => undefined);
    const openExternal = vi.fn();
    render(
      <Onboarding
        ready
        api={api}
        saveKey={null}
        copy={copy}
        openExternal={openExternal}
        timers={clock.timers}
      />,
    );
    await settle();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Acme Cloud/ }));
    });
    await settle();
    expect(screen.getByText("WXYZ-1234")).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy the code" }));
    });
    expect(copy).toHaveBeenCalledWith("WXYZ-1234");
    fireEvent.click(screen.getByRole("button", { name: "Open the sign-in page" }));
    expect(openExternal).toHaveBeenCalledWith("https://acme.example/activate");
    await clock.flush();
    expect(screen.getByText("WXYZ-1234")).toBeTruthy();
    status = "approved";
    await clock.flush();
    expect(screen.getByRole("button", { name: "Start chatting" })).toBeTruthy();
    const polls = api.calls.filter((c) => c.path === "/api/providers/login/login-1");
    expect(polls.map((c) => c.method)).toEqual(["GET", "GET"]);
  });

  it("leaving a device sign-in cancels it on the backend", async () => {
    const api = fakeRest();
    const clock = manualTimers();
    api.route("GET", "/api/model/options", OPTIONS(false));
    api.route("POST", "/api/providers/login/start", {
      session_id: "login-2",
      user_code: "C",
      verification_url: "https://acme.example/a",
    });
    api.route("DELETE", "/api/providers/login/login-2", { status: "cancelled" });
    render(<Onboarding ready api={api} saveKey={null} timers={clock.timers} />);
    await settle();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Acme Cloud/ }));
    });
    await settle();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    });
    await settle();
    expect(
      api.calls.some((c) => c.method === "DELETE" && c.path === "/api/providers/login/login-2"),
    ).toBe(true);
    expect(screen.getByRole("button", { name: /Paste an API key/ })).toBeTruthy();
  });

  it("a local runtime endpoint is tested through the backend before the model is saved", async () => {
    const api = fakeRest();
    api.route("GET", "/api/model/options", OPTIONS(false));
    api.route("POST", "/api/model/test-endpoint", {
      ok: true,
      status: 200,
      models: ["llama-3"],
      message: "1 model",
    });
    api.route("POST", "/api/model/set", { ok: true });
    render(<Onboarding ready api={api} saveKey={null} />);
    await settle();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /model running on this computer/ }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Test and continue" }));
    });
    await settle();
    expect(api.calls.find((c) => c.path === "/api/model/test-endpoint")?.body).toEqual({
      base_url: "http://127.0.0.1:11434/v1",
    });
    expect((screen.getByLabelText("Model") as HTMLInputElement).value).toBe("llama-3");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Start chatting" }));
    });
    await settle();
    expect(api.calls.find((c) => c.path === "/api/model/set")?.body).toEqual({
      scope: "main",
      provider: "custom",
      model: "llama-3",
      base_url: "http://127.0.0.1:11434/v1",
    });
  });

  it("the parity flow's data-ui hooks walk the custom endpoint path (W46, RM21)", async () => {
    const hook = (name: string) => document.querySelector(`[data-ui="${name}"]`) as HTMLElement;
    const api = fakeRest();
    api.route("GET", "/api/model/options", OPTIONS(false));
    api.route("POST", "/api/model/test-endpoint", {
      ok: true,
      status: 200,
      models: ["parity-model"],
      message: "1 model",
    });
    api.route("POST", "/api/model/set", { ok: true });
    render(<Onboarding ready api={api} saveKey={null} />);
    await settle();
    expect(hook("onboarding-dialog").getAttribute("role")).toBe("dialog");
    // W52-m: the custom endpoint row sits behind the "Other providers" disclosure
    fireEvent.click(screen.getByRole("button", { name: "Other providers" }));
    await act(async () => {
      fireEvent.click(hook("onboarding-custom-option"));
    });
    const url = hook("onboarding-endpoint-url") as HTMLInputElement;
    expect(url.tagName).toBe("INPUT");
    fireEvent.change(url, { target: { value: "http://127.0.0.1:24001/v1" } });
    await act(async () => {
      fireEvent.click(hook("onboarding-test-continue"));
    });
    await settle();
    expect((hook("onboarding-model-select") as HTMLInputElement).value).toBe("parity-model");
    await act(async () => {
      fireEvent.click(hook("onboarding-start-chatting"));
    });
    await settle();
    expect(api.calls.find((c) => c.path === "/api/model/set")?.body).toMatchObject({
      provider: "custom",
      model: "parity-model",
      base_url: "http://127.0.0.1:24001/v1",
    });
  });

  it("recognises provider and auth failures only", () => {
    expect(
      isProviderFailure({
        type: "turn.done",
        reason: "provider_failed",
        failure: { reason: "boom" },
      }),
    ).toBe(true);
    expect(isProviderFailure({ type: "turn.done", reason: "completed", failure: null })).toBe(
      false,
    );
    expect(isProviderFailure({ type: "turn.started" })).toBe(false);
  });
});

describe("onboarding look (W52-m)", () => {
  it("first run: a notice box, key providers behind the disclosure, tiles on the key screen", async () => {
    const api = fakeRest();
    api.route("GET", "/api/model/options", OPTIONS(false));
    render(<Onboarding ready api={api} saveKey={null} />);
    await settle();
    expect(screen.getByText(/No model provider is connected yet/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /OpenRouter/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Other providers" }));
    expect(screen.getByRole("button", { name: /OpenRouter/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Collapse" })).toBeTruthy();
    expect(screen.queryByText(/recommended/i)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Paste an API key/ }));
    expect(screen.queryByRole("combobox")).toBeNull();
    const tile = screen.getByRole("button", { name: /OpenRouter/ });
    expect(tile.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: /Another key/ }));
    expect(screen.getByLabelText("Variable name")).toBeTruthy();
  });

  it("opened from Settings: no notice box", async () => {
    const api = fakeRest();
    api.route("GET", "/api/model/options", OPTIONS(true));
    render(<Onboarding ready api={api} saveKey={null} />);
    await settle();
    await act(async () => {
      requestOnboarding({ reason: "manual" });
    });
    await settle();
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.queryByText(/No model provider is connected yet/)).toBeNull();
  });

  it("a provider failure brings it back with its own notice", async () => {
    const api = fakeRest();
    api.route("GET", "/api/model/options", OPTIONS(false));
    const bus = events();
    render(<Onboarding ready api={api} saveKey={null} events={bus.source} />);
    await settle();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Choose later" }));
    });
    await act(async () => {
      bus.emit({
        type: "turn.done",
        session_id: "s",
        reason: "provider_failed",
        failure: { reason: "No API key is configured for this provider" },
      });
    });
    await settle();
    expect(screen.getByText(/failed at the provider/)).toBeTruthy();
  });

  describe("Sign in with ChatGPT (W61-M2)", () => {
    const CHATGPT = {
      name: "openai-codex",
      label: "OpenAI (ChatGPT sign-in)",
      auth_type: "oauth",
      configured: false,
      device_login: true,
      family: "openai",
    };
    const withChatgpt = (configured: boolean) => {
      const options = OPTIONS(false);
      return { ...options, providers: [...options.providers, { ...CHATGPT, configured }] };
    };
    // A code like the sign-in page's, built at runtime.
    const code = () => ["K4TR", "77QZ"].join("-");

    it("lives under Other providers, not among the visible sign-ins", async () => {
      const api = fakeRest();
      api.route("GET", "/api/model/options", withChatgpt(false));
      render(<Onboarding ready api={api} saveKey={null} />);
      await settle();
      expect(screen.queryByRole("button", { name: /Sign in with ChatGPT/ })).toBeNull();
      expect(screen.queryByRole("button", { name: /OpenAI \(ChatGPT sign-in\)/ })).toBeNull();
      expect(screen.getByRole("button", { name: /Acme Cloud/ })).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Other providers" }));
      const row = screen.getByRole("button", { name: /Sign in with ChatGPT/ });
      expect(row.getAttribute("data-ui")).toBe("onboarding-signin-openai-codex");
      expect(row.textContent).toContain("ChatGPT plan");
      expect(row.textContent).not.toContain("Connected");
    });

    it("shows Connected on the row once signed in", async () => {
      const api = fakeRest();
      api.route("GET", "/api/model/options", withChatgpt(true));
      render(<Onboarding ready api={api} saveKey={null} />);
      await settle();
      await act(async () => {
        requestOnboarding({ reason: "manual" });
      });
      await settle();
      fireEvent.click(screen.getByRole("button", { name: "Other providers" }));
      expect(screen.getByRole("button", { name: /Sign in with ChatGPT/ }).textContent).toContain(
        "Connected",
      );
    });

    it("runs the device flow in the card: code, copy, page, polls at the interval, then the model step", async () => {
      const api = fakeRest();
      const clock = manualTimers();
      api.route("GET", "/api/model/options", withChatgpt(false));
      api.route("POST", "/api/providers/login/start", {
        session_id: "login-c",
        provider: "openai-codex",
        status: "pending",
        message: "",
        user_code: code(),
        verification_url: "https://signin.example.org/codex/device",
        expires_in: 900,
        interval: 6,
      });
      let status = "pending";
      api.route("GET", "/api/providers/login/login-c", () => ({
        status: 200,
        body: { session_id: "login-c", provider: "openai-codex", status, message: "" },
      }));
      const copy = vi.fn(async () => undefined);
      const openExternal = vi.fn();
      const delays: number[] = [];
      const timers: Timers = {
        setTimeout: (fn, ms) => {
          delays.push(ms);
          return clock.timers.setTimeout(fn, ms);
        },
        clearTimeout: clock.timers.clearTimeout,
      };
      render(
        <Onboarding
          ready
          api={api}
          saveKey={null}
          copy={copy}
          openExternal={openExternal}
          timers={timers}
        />,
      );
      await settle();
      fireEvent.click(screen.getByRole("button", { name: "Other providers" }));
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /Sign in with ChatGPT/ }));
      });
      await settle();
      const start = api.calls.find((c) => c.path === "/api/providers/login/start");
      expect(start?.body).toEqual({ provider: "openai-codex" });
      expect(screen.getByRole("heading", { name: "Sign in with ChatGPT" })).toBeTruthy();
      expect(screen.getByText(code())).toBeTruthy();
      expect(screen.getByText("https://signin.example.org/codex/device")).toBeTruthy();
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Copy the code" }));
      });
      expect(copy).toHaveBeenCalledWith(code());
      fireEvent.click(screen.getByRole("button", { name: "Open the sign-in page" }));
      expect(openExternal).toHaveBeenCalledWith("https://signin.example.org/codex/device");
      expect(delays).toEqual([6_000]);
      await clock.flush();
      expect(screen.getByText(code())).toBeTruthy();
      status = "approved";
      await clock.flush();
      expect(delays).toEqual([6_000, 6_000]);
      expect(screen.getByRole("button", { name: "Start chatting" })).toBeTruthy();
    });

    it.each([
      ["denied", "The sign-in was refused."],
      ["expired", "The code expired before it was approved."],
    ])("a %s sign-in says so and offers Try again", async (end, words) => {
      const api = fakeRest();
      const clock = manualTimers();
      api.route("GET", "/api/model/options", withChatgpt(false));
      api.route("POST", "/api/providers/login/start", {
        session_id: "login-d",
        user_code: code(),
        verification_url: "https://signin.example.org/codex/device",
        interval: 5,
      });
      api.route("GET", "/api/providers/login/login-d", {
        session_id: "login-d",
        provider: "openai-codex",
        status: end,
        message: "",
      });
      render(<Onboarding ready api={api} saveKey={null} timers={clock.timers} />);
      await settle();
      fireEvent.click(screen.getByRole("button", { name: "Other providers" }));
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /Sign in with ChatGPT/ }));
      });
      await settle();
      await clock.flush();
      expect(screen.getByRole("alert").textContent).toContain(words);
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      });
      await settle();
      expect(api.calls.filter((c) => c.path === "/api/providers/login/start")).toHaveLength(2);
      expect(screen.getByText(code())).toBeTruthy();
    });
  });
});
