import { describe, expect, it, vi } from "vitest";
import { FakeVoice } from "../../test/fakeVoice.ts";
import {
  Conversation,
  type LevelSource,
  type Player,
  ReadAloud,
  speakable,
  VAD,
} from "./conversation.ts";
import { type AudioClip, type Recipient, type Recorder, rpcVoicePort } from "./dictation.ts";

const A: Recipient = { sessionId: "session-a", profile: "work" };

class FakePlayer implements Player {
  playing: AudioClip | null = null;
  played: string[] = [];
  stops = 0;
  private end: (() => void) | null = null;

  play(clip: AudioClip): Promise<void> {
    this.playing = clip;
    this.played.push(new TextDecoder().decode(clip.bytes));
    return new Promise((resolve) => {
      this.end = () => {
        this.playing = null;
        resolve();
      };
    });
  }

  finish(): void {
    this.end?.();
  }

  stop(): void {
    this.stops += 1;
    this.end?.();
  }
}

class FakeLevels implements LevelSource {
  onFrame: ((rms: number, at: number) => void) | null = null;
  stopped = 0;
  async start(onFrame: (rms: number, at: number) => void) {
    this.onFrame = onFrame;
  }
  stop() {
    this.stopped += 1;
  }
}

function recorder(): Recorder {
  return {
    start: async () => undefined,
    stop: async () => ({ bytes: new Uint8Array([9, 9]), mime: "audio/webm" }),
    cancel: () => undefined,
  };
}

async function settle(): Promise<void> {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
}

function rig() {
  const voice = new FakeVoice();
  const player = new FakePlayer();
  const levels = new FakeLevels();
  const submitted: Array<[string | null, string]> = [];
  const conversation = new Conversation({
    port: voice,
    levels,
    recorder: async () => recorder(),
    player,
    submit: async (recipient, text) => {
      submitted.push([recipient.sessionId, text]);
      return `**Reply** to ${text}`;
    },
  });
  return { voice, player, levels, conversation, submitted };
}

/** Speech for `ms`, then silence long enough to end the turn. */
async function sayAndPause(levels: FakeLevels, from: number, ms = 400): Promise<number> {
  let at = from;
  for (; at < from + ms; at += 50) levels.onFrame?.(0.2, at);
  await settle();
  for (; at <= from + ms + VAD.silenceMs; at += 100) levels.onFrame?.(0.001, at);
  await settle();
  return at;
}

describe("spoken conversation", () => {
  it("VAD ends the turn after silence, submits to the pinned session and speaks the reply", async () => {
    const { voice, player, levels, conversation, submitted } = rig();
    await conversation.start(A);
    expect(conversation.phase).toBe("listening");
    await sayAndPause(levels, 0);
    await settle();
    expect(submitted).toEqual([["session-a", "hello"]]);
    expect(voice.spoken).toEqual(["Reply to hello"]);
    expect(conversation.phase).toBe("speaking");
    player.finish();
    await settle();
    expect(conversation.phase).toBe("listening");
  });

  it("barge-in stops playback", async () => {
    const { player, levels, conversation } = rig();
    await conversation.start(A);
    const at = await sayAndPause(levels, 0);
    await settle();
    expect(player.playing).not.toBeNull();
    levels.onFrame?.(0.3, at + 10); // the person talks over the reply
    await settle();
    expect(player.stops).toBe(1);
    expect(player.playing).toBeNull();
    expect(conversation.phase).toBe("capturing");
  });

  it("barge-in while the reply's audio is still being made abandons it", async () => {
    const { voice, player, levels, conversation } = rig();
    voice.holdSpeechReplies = true;
    await conversation.start(A);
    const at = await sayAndPause(levels, 0);
    await settle();
    expect(conversation.phase).toBe("speaking");
    levels.onFrame?.(0.3, at + 10);
    voice.releaseSpeech();
    await settle();
    expect(player.played).toEqual([]);
    expect(conversation.phase).toBe("capturing");
  });

  it("quiet frames never start a turn; stop ends capture and playback", async () => {
    const { levels, conversation, submitted } = rig();
    await conversation.start(A);
    for (let at = 0; at < 3000; at += 100) levels.onFrame?.(0.005, at);
    await settle();
    expect(submitted).toEqual([]);
    conversation.stop();
    expect(conversation.phase).toBe("off");
    expect(levels.stopped).toBe(1);
    levels.onFrame?.(0.5, 4000);
    expect(conversation.phase).toBe("off");
  });

  it("a turn never runs past the maximum", async () => {
    const { levels, conversation, submitted } = rig();
    await conversation.start(A);
    levels.onFrame?.(0.2, 0);
    await settle();
    for (let at = 500; at <= VAD.maxTurnMs; at += 500) levels.onFrame?.(0.2, at);
    await settle();
    expect(submitted).toHaveLength(1);
  });
});

describe("read-aloud", () => {
  it("speaks markdown as plain words; a second reply stops the first", async () => {
    const voice = new FakeVoice();
    const player = new FakePlayer();
    const reader = new ReadAloud(voice, player);
    void reader.speak("# Title\n\nSee [the docs](https://x.test) and `code`.");
    await settle();
    expect(player.played).toEqual(["Title See the docs and code."]);
    void reader.speak("Second");
    await settle();
    expect(player.stops).toBe(1);
    expect(player.played.at(-1)).toBe("Second");
    reader.stop();
    expect(reader.speaking).toBe(false);
    expect(speakable("```js\nx()\n```\n**done**")).toBe("done");
  });
});

describe("rpcVoicePort", () => {
  it("sends audio as base64 with the pinned session and decodes speech", async () => {
    const calls: Array<[string, unknown]> = [];
    const port = rpcVoicePort(async (method, params) => {
      calls.push([method, params]);
      return method === "voice.transcribe"
        ? { text: "hi" }
        : { audio_b64: btoa("RIFF"), mime: "audio/wav" };
    });
    const signal = new AbortController().signal;
    expect(
      await port.transcribe(
        { bytes: new Uint8Array([104, 105]), mime: "audio/webm" },
        { signal, recipient: A },
      ),
    ).toBe("hi");
    expect(calls[0]).toEqual([
      "voice.transcribe",
      { session_id: "session-a", audio_b64: btoa("hi"), mime: "audio/webm" },
    ]);
    const clip = await port.speak("hello", { signal });
    expect(new TextDecoder().decode(clip.bytes)).toBe("RIFF");
    const failing = rpcVoicePort(async () => ({}));
    await expect(failing.speak("x", { signal })).rejects.toThrow();
    expect(vi.isMockFunction(port.speak)).toBe(false);
  });
});
