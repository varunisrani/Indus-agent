// Composer dictation (P24-35): record, send the audio to the daemon's speech-to-text, put the
// words into the draft of the session the capture started for.
//
// - The recipient (session + profile) is pinned when the capture starts. A session switch while
//   recording or transcribing does not move the text: it goes to the pinned session's draft
//   (R08.19). A newer capture makes an older pending result stale; a stale result is dropped.
// - Transcription has a deadline, `STT_TIMEOUT_MS` (60 s) unless the setting says otherwise; past
//   it the request is aborted and nothing is inserted.
// - The microphone is asked for only when the person starts the first capture (the recorder
//   factory runs then, never before); a refusal is an error state and nothing records.
// - While a capture runs the state is `recording`, which the composer shows (pressed button and a
//   "Listening" label): no capture without its indicator.

export const STT_TIMEOUT_MS = 60_000;

export interface Recipient {
  readonly sessionId: string | null;
  readonly profile: string | null;
}

export interface AudioClip {
  readonly bytes: Uint8Array;
  readonly mime: string;
}

/** The daemon's voice methods (speech-to-text and text-to-speech). */
export interface VoicePort {
  transcribe(
    clip: AudioClip,
    options: { signal: AbortSignal; recipient: Recipient },
  ): Promise<string>;
  speak(text: string, options: { signal: AbortSignal }): Promise<AudioClip>;
}

/** One capture. `stop` resolves the recorded audio (null when nothing was recorded). */
export interface Recorder {
  start(): Promise<void>;
  stop(): Promise<AudioClip | null>;
  cancel(): void;
}

export type RecorderFactory = () => Promise<Recorder>;

/** Where dictated text lands: the draft of `recipient`, whichever session is on screen. */
export type DraftSink = (recipient: Recipient, text: string) => void;

export type DictationState = "idle" | "starting" | "recording" | "transcribing";

export interface DictationSnapshot {
  readonly state: DictationState;
  readonly recipient: Recipient | null;
  /** The last failure, cleared by the next start. */
  readonly error: DictationError | null;
}

export type DictationError = "mic-denied" | "timeout" | "stt-failed" | "nothing-heard";

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

const REAL_TIMERS: Timers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export interface DictationOptions {
  port: VoicePort;
  recorder: RecorderFactory;
  sink: DraftSink;
  timers?: Timers;
  /** The configured deadline (`voice.stt_timeout_s` × 1000), else `STT_TIMEOUT_MS`. */
  timeoutMs?: () => number;
}

export class Dictation {
  private snap: DictationSnapshot = { state: "idle", recipient: null, error: null };
  private recorder: Recorder | null = null;
  private generation = 0;
  private inflight: AbortController | null = null;
  private readonly listeners = new Set<() => void>();
  private readonly options: DictationOptions;

  constructor(options: DictationOptions) {
    this.options = options;
  }

  snapshot = (): DictationSnapshot => this.snap;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  get active(): boolean {
    return this.snap.state === "starting" || this.snap.state === "recording";
  }

  /** Start a capture for `recipient` (pinned until its text is delivered or dropped). */
  async start(recipient: Recipient): Promise<void> {
    if (this.active) return;
    this.generation += 1;
    const mine = this.generation;
    this.inflight?.abort(); // an older transcription is now stale
    this.inflight = null;
    this.set({ state: "starting", recipient, error: null });
    let recorder: Recorder;
    try {
      recorder = await this.options.recorder();
      await recorder.start();
    } catch {
      if (mine === this.generation)
        this.set({ state: "idle", recipient: null, error: "mic-denied" });
      return;
    }
    if (mine !== this.generation) {
      recorder.cancel();
      return;
    }
    this.recorder = recorder;
    this.set({ state: "recording", recipient, error: null });
  }

  /** Stop the capture and transcribe it into the pinned recipient's draft. */
  async stop(): Promise<void> {
    const recorder = this.recorder;
    const recipient = this.snap.recipient;
    if (recorder === null || recipient === null || this.snap.state !== "recording") return;
    this.recorder = null;
    const mine = this.generation;
    this.set({ state: "transcribing", recipient, error: null });
    const clip = await recorder.stop().catch(() => null);
    if (mine !== this.generation) return;
    if (clip === null || clip.bytes.byteLength === 0) {
      this.set({ state: "idle", recipient: null, error: "nothing-heard" });
      return;
    }
    const controller = new AbortController();
    this.inflight = controller;
    const timers = this.options.timers ?? REAL_TIMERS;
    let timedOut = false;
    const timer = timers.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, this.options.timeoutMs?.() ?? STT_TIMEOUT_MS);
    let text: string | null = null;
    let error: DictationError | null = null;
    try {
      text = await abortable(
        this.options.port.transcribe(clip, { signal: controller.signal, recipient }),
        controller.signal,
      );
    } catch {
      error = timedOut ? "timeout" : "stt-failed";
    } finally {
      timers.clearTimeout(timer);
    }
    if (mine !== this.generation || (controller.signal.aborted && !timedOut)) return; // stale
    if (this.inflight === controller) this.inflight = null;
    const words = (text ?? "").trim();
    if (error === null && words === "") error = "nothing-heard";
    if (error === null) this.options.sink(recipient, words);
    this.set({ state: "idle", recipient: null, error });
  }

  /** Drop the capture (composer closed, Esc): nothing is sent. */
  cancel(): void {
    this.generation += 1;
    this.recorder?.cancel();
    this.recorder = null;
    this.inflight?.abort();
    this.inflight = null;
    this.set({ state: "idle", recipient: null, error: null });
  }

  private set(next: DictationSnapshot): void {
    this.snap = next;
    for (const listener of [...this.listeners]) {
      try {
        listener();
      } catch {
        // a broken listener does not stop dictation
      }
    }
  }
}

/** `promise`, rejected as soon as `signal` aborts (a port that ignores the signal cannot hang us). */
export function abortable<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(new Error("aborted"));
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(new Error("aborted"));
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(error);
      },
    );
  });
}

export interface MediaRecorderLike {
  start(): void;
  stop(): void;
  state: string;
  mimeType: string;
  ondataavailable: ((event: { data: Blob }) => void) | null;
  onstop: (() => void) | null;
}

/**
 * A recorder over the browser's microphone. `getUserMedia` (and so the permission prompt) runs
 * only when the capture starts; the tracks stop when it ends.
 */
export function browserRecorder(media: {
  getUserMedia(constraints: { audio: true }): Promise<MediaStream>;
  createRecorder(stream: MediaStream): MediaRecorderLike;
}): RecorderFactory {
  return async () => {
    let stream: MediaStream | null = null;
    let recorder: MediaRecorderLike | null = null;
    const chunks: Blob[] = [];
    const release = () => {
      for (const track of stream?.getTracks() ?? []) track.stop();
      stream = null;
    };
    return {
      start: async () => {
        stream = await media.getUserMedia({ audio: true });
        recorder = media.createRecorder(stream);
        recorder.ondataavailable = (event) => chunks.push(event.data);
        recorder.start();
      },
      stop: () =>
        new Promise<AudioClip | null>((resolve) => {
          const current = recorder;
          if (current === null || current.state === "inactive") {
            release();
            resolve(null);
            return;
          }
          current.onstop = () => {
            release();
            const blob = new Blob(chunks, { type: current.mimeType || "audio/webm" });
            void blob.arrayBuffer().then(
              (buffer) => resolve({ bytes: new Uint8Array(buffer), mime: blob.type }),
              () => resolve(null),
            );
          };
          current.stop();
        }),
      cancel: () => {
        if (recorder !== null && recorder.state !== "inactive") {
          recorder.onstop = null;
          recorder.stop();
        }
        release();
      },
    };
  };
}

function toBase64(bytes: Uint8Array): string {
  let text = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(text);
}

function fromBase64(text: string): Uint8Array {
  const raw = atob(text);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

/** A JSON-RPC call to the daemon (the connection's `request`, loosely typed). */
export type VoiceCall = (method: string, params: unknown, signal: AbortSignal) => Promise<unknown>;

/** The daemon's voice methods as a `VoicePort` (client-direct STT/TTS over the connection). */
export function rpcVoicePort(call: VoiceCall): VoicePort {
  return {
    transcribe: async (clip, { signal, recipient }) => {
      const answer = (await call(
        "voice.transcribe",
        { session_id: recipient.sessionId, audio_b64: toBase64(clip.bytes), mime: clip.mime },
        signal,
      )) as { text?: unknown };
      return typeof answer.text === "string" ? answer.text : "";
    },
    speak: async (text, { signal }) => {
      const answer = (await call("voice.speak", { text }, signal)) as {
        audio_b64?: unknown;
        mime?: unknown;
      };
      if (typeof answer.audio_b64 !== "string") throw new Error("no audio in the answer");
      return {
        bytes: fromBase64(answer.audio_b64),
        mime: typeof answer.mime === "string" ? answer.mime : "audio/wav",
      };
    },
  };
}
