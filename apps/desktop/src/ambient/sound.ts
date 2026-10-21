// The completion sound (P20-36): our own two-note chime, synthesised with Web Audio when it plays
// (no sound file ships). Quiet, short (≈ 0.35 s), and silent when audio is unavailable.

export interface AudioContextLike {
  readonly currentTime: number;
  readonly destination: unknown;
  createOscillator(): {
    type: string;
    frequency: { setValueAtTime(value: number, at: number): void };
    connect(node: unknown): unknown;
    start(at: number): void;
    stop(at: number): void;
  };
  createGain(): {
    gain: {
      setValueAtTime(value: number, at: number): void;
      linearRampToValueAtTime(value: number, at: number): void;
      exponentialRampToValueAtTime(value: number, at: number): void;
    };
    connect(node: unknown): unknown;
  };
}

/** The notes: [frequency Hz, start offset s, length s]. */
export const CHIME_NOTES: ReadonlyArray<readonly [number, number, number]> = [
  [784, 0, 0.16],
  [1046.5, 0.12, 0.22],
];
const PEAK = 0.08;

let shared: AudioContextLike | null = null;

function defaultContext(): AudioContextLike | null {
  const Ctor = (globalThis as { AudioContext?: new () => AudioContextLike }).AudioContext;
  if (Ctor === undefined) return null;
  shared ??= new Ctor();
  return shared;
}

/** Play the chime; false when there is no audio. */
export function playChime(context: AudioContextLike | null = defaultContext()): boolean {
  if (context === null) return false;
  try {
    const start = context.currentTime;
    for (const [frequency, offset, length] of CHIME_NOTES) {
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(frequency, start + offset);
      gain.gain.setValueAtTime(0.0001, start + offset);
      gain.gain.linearRampToValueAtTime(PEAK, start + offset + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + offset + length);
      oscillator.connect(gain);
      gain.connect(context.destination);
      oscillator.start(start + offset);
      oscillator.stop(start + offset + length + 0.02);
    }
    return true;
  } catch {
    return false;
  }
}
