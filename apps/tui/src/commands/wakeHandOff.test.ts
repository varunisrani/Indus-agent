// The wake word's hand-off in the terminal (P24-20 wiring): `wake.detected` → one voice take →
// the user's next prompt → listening again.
import { afterEach, describe, expect, it } from "vitest";
import { type RuntimeRig, runtimeRig, settle } from "../app/testRuntime.ts";
import { transcriptStore } from "../state/transcriptStore.ts";
import { uiStore } from "../state/uiStore.ts";
import {
  type HandOffDeps,
  VOICE_LISTEN_TIMEOUT_MS,
  WAKE_DETECTED,
  WakeHandOff,
} from "./wakeHandOff.ts";

let rig: RuntimeRig | null = null;
afterEach(() => {
  rig?.runtime.dispose();
  rig = null;
});

const lines = (): string[] =>
  transcriptStore.getState().messages.map((m) => `${m.role}: ${m.text}`);

describe("wake word hand-off", () => {
  it("a detection records one take, sends it as the user's prompt and listens again", async () => {
    const order: string[] = [];
    const r = runtimeRig({
      handlers: {
        "voice.listen": () => {
          order.push("voice.listen");
          expect(uiStore.getState().statusText).toBe("listening…");
          return { text: "what is on my calendar", reason: "spoke" };
        },
        "prompt.submit": () => {
          order.push("prompt.submit");
          return { accepted: true };
        },
        "wake.resume": () => {
          order.push("wake.resume");
          return { ok: true };
        },
      },
    });
    rig = r;
    await r.ready();
    const sid = uiStore.getState().sid;
    r.server.emit({ type: WAKE_DETECTED, phrase: "hey indus", surface: "tui" });
    await settle(40);
    expect(r.calls("voice.listen")).toEqual([{ session_id: sid }]);
    expect(r.calls("prompt.submit").map((p) => p.text)).toEqual(["what is on my calendar"]);
    // the take first; then the prompt goes out and the listener listens again
    expect(order[0]).toBe("voice.listen");
    expect([...order].sort()).toEqual(["prompt.submit", "voice.listen", "wake.resume"]);
    expect(lines()).toContain("system: Heard “hey indus”. Speak now; a pause sends it.");
    expect(lines()).toContain("user: what is on my calendar");
    expect(uiStore.getState().statusText).not.toBe("listening…");
  });

  it("nothing said sends nothing; the listener still listens again", async () => {
    const r = runtimeRig({
      handlers: {
        "voice.listen": () => ({ text: "", reason: "no_speech" }),
        "wake.resume": () => ({ ok: true }),
      },
    });
    rig = r;
    await r.ready();
    r.server.emit({ type: WAKE_DETECTED, phrase: "hey indus" });
    await settle(40);
    expect(r.calls("prompt.submit")).toEqual([]);
    expect(lines()).toContain("system: Nothing was heard, so nothing was sent.");
    expect(r.calls("wake.resume")).toHaveLength(1);
  });
});

function deps(overrides: Partial<HandOffDeps> = {}) {
  const calls: Array<{ method: string; params: unknown; timeoutMs?: number | undefined }> = [];
  const said: string[] = [];
  const sent: string[] = [];
  const answers: Record<string, () => unknown> = {
    "voice.listen": () => ({ text: "hello", reason: "spoke" }),
    "wake.resume": () => ({ ok: true }),
  };
  const made: HandOffDeps = {
    rpc: {
      call: async (method, params, options) => {
        calls.push({ method, params, timeoutMs: options?.timeoutMs });
        const answer = answers[method];
        if (answer === undefined) throw new Error(`no ${method}`);
        return answer();
      },
    },
    system: (text) => said.push(text),
    send: (text) => sent.push(text),
    status: () => undefined,
    questionOpen: () => false,
    sessionId: () => "s-1",
    ...overrides,
  };
  return { made, calls, said, sent, answers };
}

describe("WakeHandOff", () => {
  it("the take gets its own long deadline; a second detection meanwhile is ignored", async () => {
    const { made, calls, sent } = deps();
    const handOff = new WakeHandOff(made);
    const first = handOff.detected({ phrase: "hey indus" });
    expect(handOff.capturing).toBe(true);
    await handOff.detected({ phrase: "hey indus" });
    await first;
    expect(calls.map((c) => c.method)).toEqual(["voice.listen", "wake.resume"]);
    expect(calls[0]?.timeoutMs).toBe(VOICE_LISTEN_TIMEOUT_MS);
    expect(calls[0]?.params).toEqual({ session_id: "s-1" });
    expect(sent).toEqual(["hello"]);
  });

  it("an open question card is never recorded over", async () => {
    const { made, calls, said, sent } = deps({ questionOpen: () => true });
    await new WakeHandOff(made).detected({ phrase: "hey indus" });
    expect(calls.map((c) => c.method)).toEqual(["wake.resume"]);
    expect(said[0]).toContain("a question is waiting");
    expect(sent).toEqual([]);
  });

  it("a failed take is reported and the listener is asked to listen again", async () => {
    const { made, calls, said, answers } = deps();
    answers["voice.listen"] = () => {
      throw new Error("the microphone could not record: busy");
    };
    answers["wake.resume"] = () => ({ ok: false, reason: "not_owner" });
    await new WakeHandOff(made).detected({});
    expect(said).toContain("Voice input failed: the microphone could not record: busy");
    expect(said.at(-1)).toBe("The wake word stopped listening; /wake on starts it again.");
    expect(calls.at(-1)?.method).toBe("wake.resume");
  });
});
