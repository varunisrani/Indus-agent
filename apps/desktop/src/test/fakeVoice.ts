// The voice port the composer's voice tests drive (P24-35). In the app the port is
// `rpcVoicePort` over the daemon's `voice.transcribe` / `voice.speak` (indus.service.voice_rpc).

import type { AudioClip, Recipient, VoicePort } from "../chat/voice/dictation.ts";

interface Gate {
  release(text: string): void;
}

export class FakeVoice implements VoicePort {
  transcribed: Array<{ clip: AudioClip; recipient: Recipient }> = [];
  spoken: string[] = [];
  aborted = 0;
  nextTranscript = "hello";
  private held: Array<(text: string) => void> = [];
  private holding = false;
  private speechHeld: Array<() => void> = [];
  holdSpeechReplies = false;

  /** Transcripts wait until `release` (or forever, which a deadline then ends). */
  holdTranscripts(): Gate {
    this.holding = true;
    return {
      release: (text) => {
        this.holding = false;
        for (const resolve of this.held.splice(0)) resolve(text);
      },
    };
  }

  releaseSpeech(): void {
    for (const resolve of this.speechHeld.splice(0)) resolve();
  }

  transcribe(clip: AudioClip, options: { signal: AbortSignal; recipient: Recipient }) {
    this.transcribed.push({ clip, recipient: options.recipient });
    return new Promise<string>((resolve, reject) => {
      options.signal.addEventListener("abort", () => {
        this.aborted += 1;
        reject(new Error("aborted"));
      });
      if (this.holding) this.held.push(resolve);
      else resolve(this.nextTranscript);
    });
  }

  speak(text: string, options: { signal: AbortSignal }) {
    this.spoken.push(text);
    return new Promise<AudioClip>((resolve, reject) => {
      options.signal.addEventListener("abort", () => reject(new Error("aborted")));
      const clip = { bytes: new TextEncoder().encode(text), mime: "audio/wav" };
      if (this.holdSpeechReplies) this.speechHeld.push(() => resolve(clip));
      else resolve(clip);
    });
  }
}
