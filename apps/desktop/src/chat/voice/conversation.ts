// Read-aloud and spoken conversation (P24-35).
//
// `ReadAloud` speaks one reply at a time through one player: starting another stops the first; the
// text is turned into audio by the daemon's text-to-speech (`VoicePort.speak`).
//
// `Conversation` is a loop over the microphone's level frames (RMS, 0..1):
//
//   listening ──speech──▶ capturing ──silence ≥ VAD.silenceMs──▶ thinking ──reply──▶ speaking
//       ▲                    │ (or VAD.maxTurnMs)                 transcribe,          │
//       └────────────────────┴──────────── nothing heard ◀─────── submit, speak ◀──────┘
//
// Barge-in: a speech frame while the reply is being fetched or played stops the playback (and
// abandons the audio request) at once and starts capturing the new turn. The recipient is pinned
// when the conversation starts, like dictation's (R08.19): every turn goes to that session.
// `stop()` ends capture and playback.

import {
  type AudioClip,
  abortable,
  type Recipient,
  type Recorder,
  type RecorderFactory,
  type VoicePort,
} from "./dictation.ts";

export const VAD = Object.freeze({
  /** A frame at or above this level counts as speech. */
  speechRms: 0.02,
  /** This much quiet after speech ends the turn. */
  silenceMs: 900,
  /** A turn never runs longer than this. */
  maxTurnMs: 60_000,
});

export interface Player {
  /** Resolves when playback ends (also after `stop`). */
  play(clip: AudioClip): Promise<void>;
  stop(): void;
}

/** The microphone's level, one call per frame (`atMs` from a monotonic clock). */
export interface LevelSource {
  start(onFrame: (rms: number, atMs: number) => void): Promise<void>;
  stop(): void;
}

/** Strip markdown so the reply reads naturally. */
export function speakable(markdown: string): string {
  return markdown
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/[*_~>]+/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

export class ReadAloud {
  private current: AbortController | null = null;
  private readonly port: VoicePort;
  private readonly player: Player;

  constructor(port: VoicePort, player: Player) {
    this.port = port;
    this.player = player;
  }

  get speaking(): boolean {
    return this.current !== null;
  }

  async speak(text: string): Promise<void> {
    this.stop();
    const words = speakable(text);
    if (words === "") return;
    const controller = new AbortController();
    this.current = controller;
    try {
      const clip = await abortable(
        this.port.speak(words, { signal: controller.signal }),
        controller.signal,
      );
      if (controller.signal.aborted) return;
      await this.player.play(clip);
    } catch {
      // aborted or the voice failed: nothing to play
    } finally {
      if (this.current === controller) this.current = null;
    }
  }

  stop(): void {
    if (this.current === null) return;
    this.current.abort();
    this.current = null;
    this.player.stop();
  }
}

export type ConversationPhase = "off" | "listening" | "capturing" | "thinking" | "speaking";

export interface ConversationOptions {
  port: VoicePort;
  levels: LevelSource;
  recorder: RecorderFactory;
  player: Player;
  /** Send the words to `recipient` and resolve the reply's text ("" for none). */
  submit(recipient: Recipient, text: string): Promise<string>;
  vad?: Partial<typeof VAD>;
}

export class Conversation {
  private phaseNow: ConversationPhase = "off";
  private recipient: Recipient | null = null;
  private recorder: Recorder | null = null;
  private lastSpeechAt = 0;
  private turnStartedAt = 0;
  private turn = 0;
  private reply: ReadAloud;
  private readonly listeners = new Set<() => void>();
  private readonly vad: typeof VAD;
  private readonly options: ConversationOptions;

  constructor(options: ConversationOptions) {
    this.options = options;
    this.vad = { ...VAD, ...options.vad };
    this.reply = new ReadAloud(options.port, options.player);
  }

  get phase(): ConversationPhase {
    return this.phaseNow;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** Start the loop for `recipient` (asks for the microphone now, the first time). */
  async start(recipient: Recipient): Promise<void> {
    if (this.phaseNow !== "off") return;
    this.recipient = recipient;
    this.set("listening");
    try {
      await this.options.levels.start((rms, at) => this.frame(rms, at));
    } catch {
      this.stop();
      throw new Error("microphone not allowed");
    }
  }

  stop(): void {
    this.turn += 1;
    this.options.levels.stop();
    this.recorder?.cancel();
    this.recorder = null;
    this.reply.stop();
    this.recipient = null;
    this.set("off");
  }

  /** One level frame from the microphone. */
  frame(rms: number, at: number): void {
    const speech = rms >= this.vad.speechRms;
    switch (this.phaseNow) {
      case "off":
        return;
      case "listening":
        if (speech) void this.beginTurn(at);
        return;
      case "thinking":
      case "speaking":
        if (speech) {
          // barge-in: the person talks over the reply
          this.reply.stop();
          this.turn += 1;
          void this.beginTurn(at);
        }
        return;
      case "capturing":
        if (speech) this.lastSpeechAt = at;
        if (
          at - this.lastSpeechAt >= this.vad.silenceMs ||
          at - this.turnStartedAt >= this.vad.maxTurnMs
        ) {
          void this.endTurn();
        }
        return;
    }
  }

  private async beginTurn(at: number): Promise<void> {
    this.set("capturing");
    this.lastSpeechAt = at;
    this.turnStartedAt = at;
    const mine = ++this.turn;
    try {
      const recorder = await this.options.recorder();
      await recorder.start();
      if (mine !== this.turn || this.phaseNow === "off") {
        recorder.cancel();
        return;
      }
      this.recorder = recorder;
    } catch {
      if (mine === this.turn) this.set("listening");
    }
  }

  private async endTurn(): Promise<void> {
    const recorder = this.recorder;
    const recipient = this.recipient;
    this.recorder = null;
    if (recorder === null || recipient === null) {
      this.set("listening");
      return;
    }
    this.set("thinking");
    const mine = this.turn;
    const clip = await recorder.stop().catch(() => null);
    if (mine !== this.turn) return;
    if (clip === null || clip.bytes.byteLength === 0) {
      this.set("listening");
      return;
    }
    const controller = new AbortController();
    let text = "";
    try {
      text = (
        await this.options.port.transcribe(clip, { signal: controller.signal, recipient })
      ).trim();
    } catch {
      text = "";
    }
    if (mine !== this.turn) return;
    if (text === "") {
      this.set("listening");
      return;
    }
    let answer = "";
    try {
      answer = await this.options.submit(recipient, text);
    } catch {
      answer = "";
    }
    if (mine !== this.turn) return;
    if (answer.trim() !== "") {
      this.set("speaking");
      await this.reply.speak(answer);
    }
    if (mine === this.turn && this.phaseNow !== "off") this.set("listening");
  }

  private set(phase: ConversationPhase): void {
    this.phaseNow = phase;
    for (const listener of [...this.listeners]) {
      try {
        listener();
      } catch {
        // ignore
      }
    }
  }
}
