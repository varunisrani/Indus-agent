// English strings of the composer's voice buttons (P24-35).

export const VOICE_STRINGS = {
  "voice.mic": "Mic",
  "voice.stop": "Stop",
  "voice.dictate": "Dictate into the message",
  "voice.stopDictation": "Stop dictating",
  "voice.listening": "Listening",
  "voice.transcribing": "Writing it down",
  "voice.converse": "Talk",
  "voice.endConversation": "End talk",
  "voice.error.mic-denied": "The microphone is not allowed.",
  "voice.error.timeout": "Transcription timed out.",
  "voice.error.stt-failed": "Transcription failed.",
  "voice.error.nothing-heard": "Nothing heard.",
} as const satisfies Record<string, string>;
