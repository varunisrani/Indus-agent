import { afterEach, describe, expect, it, vi } from "vitest";
import { FakeVoice } from "../../test/fakeVoice.ts";
import {
  type AudioClip,
  Dictation,
  type Recipient,
  type Recorder,
  STT_TIMEOUT_MS,
} from "./dictation.ts";

const A: Recipient = { sessionId: "session-a", profile: "default" };
const B: Recipient = { sessionId: "session-b", profile: "default" };
const CLIP: AudioClip = { bytes: new Uint8Array([1, 2, 3]), mime: "audio/webm" };

function fakeRecorder(
  clip: AudioClip | null = CLIP,
): Recorder & { started: number; cancelled: number } {
  return {
    started: 0,
    cancelled: 0,
    async start() {
      this.started += 1;
    },
    async stop() {
      return clip;
    },
    cancel() {
      this.cancelled += 1;
    },
  };
}

function rig(options: { voice?: FakeVoice; deny?: boolean } = {}) {
  const voice = options.voice ?? new FakeVoice();
  const drafts: Array<[string | null, string]> = [];
  const factory = vi.fn(async () => {
    if (options.deny) throw new DOMException("denied", "NotAllowedError");
    return fakeRecorder();
  });
  const dictation = new Dictation({
    port: voice,
    recorder: factory,
    sink: (recipient, text) => drafts.push([recipient.sessionId, text]),
  });
  return { dictation, drafts, factory, voice };
}

async function settle(): Promise<void> {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
}

afterEach(() => {
  vi.useRealTimers();
});

describe("dictation", () => {
  it("asks for the microphone only on first use", async () => {
    const { dictation, factory } = rig();
    expect(factory).not.toHaveBeenCalled();
    expect(dictation.snapshot().state).toBe("idle");
    await dictation.start(A);
    expect(factory).toHaveBeenCalledOnce();
    expect(dictation.snapshot()).toMatchObject({ state: "recording", recipient: A });
  });

  it("a session switch during capture still delivers the text to the original session", async () => {
    const voice = new FakeVoice();
    const gate = voice.holdTranscripts();
    const { dictation, drafts } = rig({ voice });
    await dictation.start(A);
    // the person switches to session B while recording and while it transcribes: the composer's
    // current session changes, the capture's pinned recipient does not
    const stopping = dictation.stop();
    await settle();
    expect(dictation.snapshot()).toMatchObject({ state: "transcribing", recipient: A });
    gate.release("hello from A");
    await stopping;
    expect(drafts).toEqual([["session-a", "hello from A"]]);
    expect(voice.transcribed[0]?.recipient).toEqual(A);
  });

  it("transcription times out at 60 s and inserts nothing", async () => {
    vi.useFakeTimers();
    const voice = new FakeVoice();
    voice.holdTranscripts(); // never answers
    const { dictation, drafts } = rig({ voice });
    await dictation.start(A);
    const stopping = dictation.stop();
    await vi.advanceTimersByTimeAsync(STT_TIMEOUT_MS - 1);
    expect(dictation.snapshot().state).toBe("transcribing");
    await vi.advanceTimersByTimeAsync(1);
    await stopping;
    expect(dictation.snapshot()).toMatchObject({ state: "idle", error: "timeout" });
    expect(drafts).toEqual([]);
    expect(voice.aborted).toBe(1);
  });

  it("the configured deadline replaces the default", async () => {
    vi.useFakeTimers();
    const voice = new FakeVoice();
    voice.holdTranscripts();
    const drafts: string[] = [];
    const dictation = new Dictation({
      port: voice,
      recorder: async () => fakeRecorder(),
      sink: (_r, text) => drafts.push(text),
      timeoutMs: () => 5_000,
    });
    await dictation.start(A);
    const stopping = dictation.stop();
    await vi.advanceTimersByTimeAsync(5_000);
    await stopping;
    expect(dictation.snapshot().error).toBe("timeout");
  });

  it("a newer capture makes an older pending result stale", async () => {
    const voice = new FakeVoice();
    const first = voice.holdTranscripts();
    const { dictation, drafts } = rig({ voice });
    await dictation.start(A);
    const stopping = dictation.stop();
    await settle();
    await dictation.start(B); // a new capture while A still transcribes
    first.release("late words for A");
    await stopping;
    expect(drafts).toEqual([]);
    expect(dictation.snapshot()).toMatchObject({ state: "recording", recipient: B });
  });

  it("a denied microphone records nothing", async () => {
    const { dictation, drafts } = rig({ deny: true });
    await dictation.start(A);
    expect(dictation.snapshot()).toMatchObject({ state: "idle", error: "mic-denied" });
    await dictation.stop();
    expect(drafts).toEqual([]);
  });

  it("empty audio or an empty transcript inserts nothing", async () => {
    const empty = new Dictation({
      port: new FakeVoice(),
      recorder: async () => fakeRecorder({ bytes: new Uint8Array(), mime: "audio/webm" }),
      sink: () => {
        throw new Error("nothing should arrive");
      },
    });
    await empty.start(A);
    await empty.stop();
    expect(empty.snapshot().error).toBe("nothing-heard");
    const voice = new FakeVoice();
    voice.nextTranscript = "   ";
    const { dictation, drafts } = rig({ voice });
    await dictation.start(A);
    await dictation.stop();
    expect(drafts).toEqual([]);
    expect(dictation.snapshot().error).toBe("nothing-heard");
  });

  it("cancel drops the capture", async () => {
    const recorder = fakeRecorder();
    const dictation = new Dictation({
      port: new FakeVoice(),
      recorder: async () => recorder,
      sink: () => undefined,
    });
    await dictation.start(A);
    dictation.cancel();
    expect(recorder.cancelled).toBe(1);
    expect(dictation.snapshot().state).toBe("idle");
  });
});
