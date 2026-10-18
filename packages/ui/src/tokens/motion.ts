export const durations = {
  quick: 120,
  base: 200,
  slow: 320,
} as const;
export type DurationName = keyof typeof durations;

export const easings = {
  /** Most movement: starts briskly, settles gently. */
  standard: "cubic-bezier(0.25, 0.1, 0.2, 1)",
  /** Elements leaving the screen: picks up speed towards the end. */
  exit: "cubic-bezier(0.45, 0, 0.9, 0.6)",
} as const;
export type EasingName = keyof typeof easings;

/** Durations to use; all zero when the viewer asked for reduced motion. */
export function motionDurations(reducedMotion: boolean): Readonly<Record<DurationName, number>> {
  if (!reducedMotion) return durations;
  return { quick: 0, base: 0, slow: 0 };
}
