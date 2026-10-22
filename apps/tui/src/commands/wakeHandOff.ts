// The wake word's hand-off in the terminal (P24-20 wiring, D-16.799): `wake.detected` → one voice
// take → a user turn.
//
// The bridge (or the attached daemon) owns the microphone the wake word listens on; a detection
// already paused that listener. Here the renderer:
//   1. says it heard the phrase and shows "listening…" in the status rule;
//   2. asks for one take on that same microphone (`voice.listen`: a pause after speech ends it,
//      silence gives up) — unless a question card is open: nothing is recorded over a question;
//   3. sends the transcript as the user's next prompt, exactly as if typed (queued while a reply
//      runs), or says why nothing was sent;
//   4. always asks the listener to listen again (`wake.resume`).
// A second detection while a take runs is ignored (the listener is paused then anyway).

import { errorText, type RpcLike } from "../cards/rpcLike.ts";

export const WAKE_DETECTED = "wake.detected";
/** `wake.start` may load a model: the server allows 180 s, the call a little more. */
export const WAKE_START_TIMEOUT_MS = 190_000;
/** `voice.listen`: the longest take `voice.max_seconds` allows, then the transcription. */
export const VOICE_LISTEN_TIMEOUT_MS = 3_910_000;

export interface TimedRpc extends RpcLike {
  call(method: string, params: unknown, options?: { timeoutMs?: number }): Promise<unknown>;
}

export interface HandOffDeps {
  rpc: TimedRpc;
  /** A system line in the transcript. */
  system(text: string): void;
  /** Send `text` as the user's prompt (as typed). */
  send(text: string): void;
  /** The status rule's text while listening (null: back to normal). */
  status(text: string | null): void;
  /** A question card (approval, clarify, secret) is open. */
  questionOpen(): boolean;
  /** The session the take is for. */
  sessionId(): string | null;
}

interface ListenAnswer {
  text?: unknown;
  reason?: unknown;
}

const NOTHING: Readonly<Record<string, string>> = {
  no_speech: "Nothing was heard, so nothing was sent.",
  inaudible: "No words could be made out, so nothing was sent.",
  cancel: "The recording stopped before anything was said.",
};

export class WakeHandOff {
  private running = false;
  private readonly deps: HandOffDeps;

  constructor(deps: HandOffDeps) {
    this.deps = deps;
  }

  get capturing(): boolean {
    return this.running;
  }

  /** Handle one `wake.detected`; resolves once the listener was asked to listen again. */
  async detected(event: { phrase?: unknown }): Promise<void> {
    if (this.running) return;
    this.running = true;
    const { deps } = this;
    const phrase = typeof event.phrase === "string" && event.phrase ? event.phrase : "hey indus";
    try {
      if (deps.questionOpen()) {
        deps.system(`Heard “${phrase}”, but a question is waiting: answer it first.`);
        return;
      }
      deps.system(`Heard “${phrase}”. Speak now; a pause sends it.`);
      deps.status("listening…");
      const sid = deps.sessionId();
      let answer: ListenAnswer;
      try {
        answer = ((await deps.rpc.call("voice.listen", sid === null ? {} : { session_id: sid }, {
          timeoutMs: VOICE_LISTEN_TIMEOUT_MS,
        })) ?? {}) as ListenAnswer;
      } catch (error) {
        deps.system(`Voice input failed: ${errorText(error)}`);
        return;
      } finally {
        deps.status(null);
      }
      const text = typeof answer.text === "string" ? answer.text.trim() : "";
      if (text === "") {
        deps.system(NOTHING[String(answer.reason)] ?? NOTHING.no_speech ?? "");
        return;
      }
      deps.send(text);
    } finally {
      this.running = false;
      await this.resume();
    }
  }

  private async resume(): Promise<void> {
    try {
      const answer = (await this.deps.rpc.call("wake.resume", {})) as { ok?: unknown } | null;
      if (answer?.ok === false) {
        this.deps.system("The wake word stopped listening; /wake on starts it again.");
      }
    } catch (error) {
      this.deps.system(`The wake word could not listen again: ${errorText(error)}`);
    }
  }
}
