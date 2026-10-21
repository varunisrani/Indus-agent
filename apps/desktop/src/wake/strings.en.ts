// English strings of the wake word (P24-20): the indicator window and the Settings toggle.
// Spread into `src/i18n/en.ts`.

export const WAKE_STRINGS = {
  "wake.indicator.listening": "Listening for “{phrase}”",
  "wake.indicator.heard": "Heard you",
  "wake.indicator.capturing": "Recording",
  "wake.toggle.label": "Wake word",
  "wake.toggle.hint":
    "Say “{phrase}” to start talking. Turning this on lets Indus listen to your microphone while it is on; a light at the top of the screen shows when it listens.",
  "wake.toggle.starting": "Starting the listener (the first start can take a few minutes)",
  "wake.reason.disabled": "Turn the wake word on to start listening.",
  "wake.reason.consent": "Turn the wake word on here to agree to listening.",
  "wake.reason.owned": "Another Indus window or terminal is listening already.",
  "wake.reason.unavailable": "The wake word cannot run here: {hint}",
  "wake.reason.timeout": "The listener did not start within 3 minutes. Try again.",
  "wake.reason.failed": "The listener could not start: {message}",
} as const;
