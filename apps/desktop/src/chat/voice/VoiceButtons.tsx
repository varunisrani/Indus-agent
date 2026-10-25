// The composer's voice buttons (P24-35): dictation (mic) and spoken conversation. Rendered only
// when the composer has a `VoicePort`. The engines live per window and per draft store, not per
// composer, so a session switch mid-capture keeps the capture and its pinned recipient. While a
// capture runs the button is pressed and a "Listening" label shows (never a silent recording).

import { AudioLines, Mic, MicOff } from "lucide-react";
import { useEffect, useSyncExternalStore } from "react";
import type { ChatConnection } from "../../gateway/connectionStore.ts";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import { useKeyAction } from "../../settings/keyActions.ts";
import { type DictateRequest, dictationDone, WAKE_DICTATE_EVENT } from "../../wake/appWake.ts";
import { type DraftStore, draftOf, FRESH, setDraft } from "../composer/goneSession.ts";
import { Conversation, type LevelSource, type Player } from "./conversation.ts";
import {
  browserRecorder,
  Dictation,
  type DictationError,
  type MediaRecorderLike,
  type Recipient,
  type VoicePort,
} from "./dictation.ts";

const ERROR_KEYS: Readonly<Record<DictationError, MessageKey>> = {
  "mic-denied": "voice.error.mic-denied",
  timeout: "voice.error.timeout",
  "stt-failed": "voice.error.stt-failed",
  "nothing-heard": "voice.error.nothing-heard",
};

const dictations = new WeakMap<VoicePort, WeakMap<DraftStore, Dictation>>();

function mediaDevices(): MediaDevices | null {
  return typeof navigator !== "undefined" && navigator.mediaDevices ? navigator.mediaDevices : null;
}

/** The microphone recorder; `getUserMedia` runs on the first capture only. */
export const defaultRecorder = browserRecorder({
  getUserMedia: async (constraints) => {
    const media = mediaDevices();
    if (media === null) throw new Error("no microphone in this window");
    return media.getUserMedia(constraints);
  },
  createRecorder: (stream) => new MediaRecorder(stream) as unknown as MediaRecorderLike,
});

/** Append `words` to the stored draft of `recipient` (whichever session is on screen). */
export function draftSink(store: DraftStore) {
  return (recipient: Recipient, words: string) => {
    const key = recipient.sessionId ?? FRESH;
    store.update((state) => {
      const before = draftOf(state, key);
      const joined =
        before === "" || /\s$/.test(before) ? `${before}${words}` : `${before} ${words}`;
      return setDraft(state, key, joined);
    });
  };
}

export function dictationFor(port: VoicePort, store: DraftStore): Dictation {
  let byStore = dictations.get(port);
  if (byStore === undefined) {
    byStore = new WeakMap();
    dictations.set(port, byStore);
  }
  let found = byStore.get(store);
  if (found === undefined) {
    found = new Dictation({ port, recorder: defaultRecorder, sink: draftSink(store) });
    byStore.set(store, found);
  }
  return found;
}

/** Send `text` to `sessionId` and resolve the reply's final text. */
export async function voiceTurn(
  connection: ChatConnection,
  sessionId: string,
  text: string,
): Promise<string> {
  let turnId: string | null = null;
  let early: Map<string, string> | null = new Map();
  return new Promise<string>((resolve, reject) => {
    const off = connection.onSessionEvent(sessionId, (event) => {
      if (event.type !== "message.complete") return;
      const done = event as { turn_id: string; text: string };
      if (turnId === null) early?.set(done.turn_id, done.text);
      else if (done.turn_id === turnId) {
        off();
        resolve(done.text);
      }
    });
    connection
      .request("prompt.submit", { session_id: sessionId, text })
      .then((answer) => {
        turnId = (answer as { turn_id?: string }).turn_id ?? null;
        const seen = turnId === null ? undefined : early?.get(turnId);
        early = null;
        if (seen !== undefined) {
          off();
          resolve(seen);
        }
      })
      .catch((error: unknown) => {
        off();
        reject(error);
      });
  });
}

/** The microphone level (RMS per animation frame) through Web Audio. */
export function browserLevels(): LevelSource {
  let stop: (() => void) | null = null;
  return {
    start: async (onFrame) => {
      const media = mediaDevices();
      if (media === null) throw new Error("no microphone in this window");
      const stream = await media.getUserMedia({ audio: true });
      const audio = new AudioContext();
      const analyser = audio.createAnalyser();
      analyser.fftSize = 1024;
      audio.createMediaStreamSource(stream).connect(analyser);
      const buffer = new Float32Array(analyser.fftSize);
      let frame = 0;
      const tick = () => {
        analyser.getFloatTimeDomainData(buffer);
        let sum = 0;
        for (const sample of buffer) sum += sample * sample;
        onFrame(Math.sqrt(sum / buffer.length), performance.now());
        frame = requestAnimationFrame(tick);
      };
      frame = requestAnimationFrame(tick);
      stop = () => {
        cancelAnimationFrame(frame);
        for (const track of stream.getTracks()) track.stop();
        void audio.close();
      };
    },
    stop: () => {
      stop?.();
      stop = null;
    },
  };
}

/** One `<audio>` player for replies. */
export function browserPlayer(): Player {
  let element: HTMLAudioElement | null = null;
  let url: string | null = null;
  let done: (() => void) | null = null;
  const end = () => {
    element?.pause();
    if (url !== null) URL.revokeObjectURL(url);
    url = null;
    element = null;
    const finish = done;
    done = null;
    finish?.();
  };
  return {
    play: (clip) =>
      new Promise<void>((resolve) => {
        end();
        done = resolve;
        url = URL.createObjectURL(new Blob([clip.bytes as BlobPart], { type: clip.mime }));
        element = new Audio(url);
        element.onended = end;
        element.onerror = end;
        void element.play().catch(end);
      }),
    stop: end,
  };
}

export function VoiceButtons(props: {
  port: VoicePort;
  store: DraftStore;
  sessionId: string | null;
  profile: string | null;
  connection: ChatConnection | null;
}) {
  const t = useT();
  const dictation = dictationFor(props.port, props.store);
  const snap = useSyncExternalStore(dictation.subscribe, dictation.snapshot, dictation.snapshot);
  const recording = snap.state === "recording" || snap.state === "starting";
  const recipient: Recipient = { sessionId: props.sessionId, profile: props.profile };

  // the rebindable dictation key (W59-K1-2) does what the microphone button does
  useKeyAction(["voice.dictation"], () => {
    void (recording ? dictation.stop() : dictation.start(recipient));
    return true;
  });

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && dictation.active) dictation.cancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dictation]);

  // The wake word heard its phrase (P24-20): dictate into this composer, then tell it we are done.
  const recipientKey = `${props.sessionId ?? ""}\u0000${props.profile ?? ""}`;
  // biome-ignore lint/correctness/useExhaustiveDependencies: the recipient is keyed by recipientKey
  useEffect(() => {
    let woken = false;
    const onDictate = (event: Event) => {
      const request = (event as CustomEvent<DictateRequest>).detail;
      if (request.taken || dictation.active) return;
      request.taken = true;
      woken = true;
      void dictation.start(recipient);
    };
    const off = dictation.subscribe(() => {
      if (woken && dictation.snapshot().state === "idle") {
        woken = false;
        dictationDone();
      }
    });
    window.addEventListener(WAKE_DICTATE_EVENT, onDictate);
    return () => {
      window.removeEventListener(WAKE_DICTATE_EVENT, onDictate);
      off();
      if (woken) dictationDone();
    };
  }, [dictation, recipientKey]);

  return (
    <span className="flex items-center gap-1">
      <button
        type="button"
        className="desk-icon-btn"
        aria-pressed={recording}
        aria-label={recording ? t("voice.stopDictation") : t("voice.dictate")}
        onClick={() => void (recording ? dictation.stop() : dictation.start(recipient))}
      >
        {recording ? <MicOff aria-hidden="true" /> : <Mic aria-hidden="true" />}
      </button>
      {recording ? (
        <span role="status" className="text-xs text-danger">
          {t("voice.listening")}
        </span>
      ) : snap.state === "transcribing" ? (
        <span role="status" className="text-xs text-muted">
          {t("voice.transcribing")}
        </span>
      ) : snap.error !== null ? (
        <span role="status" className="text-xs text-muted">
          {t(ERROR_KEYS[snap.error])}
        </span>
      ) : null}
      {props.connection !== null && props.sessionId !== null ? (
        <ConversationButton
          port={props.port}
          connection={props.connection}
          sessionId={props.sessionId}
          recipient={recipient}
        />
      ) : null}
    </span>
  );
}

const conversations = new WeakMap<VoicePort, Conversation>();

function ConversationButton(props: {
  port: VoicePort;
  connection: ChatConnection;
  sessionId: string;
  recipient: Recipient;
}) {
  const t = useT();
  let conversation = conversations.get(props.port);
  if (conversation === undefined) {
    const connection = props.connection;
    conversation = new Conversation({
      port: props.port,
      levels: browserLevels(),
      recorder: defaultRecorder,
      player: browserPlayer(),
      submit: (recipient, text) =>
        recipient.sessionId === null
          ? Promise.resolve("")
          : voiceTurn(connection, recipient.sessionId, text),
    });
    conversations.set(props.port, conversation);
  }
  const live = conversation;
  const phase = useSyncExternalStore(
    (listener) => live.subscribe(listener),
    () => live.phase,
    () => live.phase,
  );
  const on = phase !== "off";
  // the rebindable voice-conversation key (W59-K1-2) does what this button does
  useKeyAction(["voice.conversation"], () => {
    if (on) live.stop();
    else void live.start(props.recipient).catch(() => undefined);
    return true;
  });
  return (
    <button
      type="button"
      className="desk-icon-btn"
      aria-pressed={on}
      aria-label={on ? t("voice.endConversation") : t("voice.converse")}
      onClick={() => (on ? live.stop() : void live.start(props.recipient).catch(() => undefined))}
    >
      <AudioLines aria-hidden="true" />
    </button>
  );
}
