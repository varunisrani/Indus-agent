// The HUD window (P20-35; W52-m look): one conversation as a Spotlight-style bar — the chat slots'
// composer on top, with reset / leave buttons at its right end — and the transcript on a dark
// translucent sheet hanging under it. The sheet shows while the composer has focus and for a
// moment after the transcript changes (a turn streaming), then fades. Press and hold on the bar's
// empty space to move the window; the invisible edges resize it. Moving and resizing are main's
// job: this window only says "held" and "let go" (`hud.drag`). When main points the HUD at another
// session (`hud.onChanged`) the route follows.

import "../ambient/ambient.css";
import { Minimize2, Undo2 } from "lucide-react";
import { type PointerEvent, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import type { AppSlots } from "../app/App.tsx";
import { desktopBridge } from "../app/bridge.ts";
import { classifyPath, NEW_CHAT_PATH, sessionPath } from "../app/routes.ts";
import { ChatView } from "../chat/ChatView.tsx";
import { useT } from "../i18n/index.tsx";

/** A press on the grip turns into a move after this long (a click stays a click). */
export const HUD_HOLD_MS = 150;
/** The sheet stays this long after the transcript last changed. */
export const HUD_LINGER_MS = 1100;

/** Pointer presses on these start their own action, never a move. */
const INTERACTIVE =
  "button, a, input, textarea, select, [contenteditable], [role='menu'], [role='dialog']";

type Edge = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
const EDGES: readonly Edge[] = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];

export function Hud(props: { slots?: AppSlots }) {
  const t = useT();
  const bridge = desktopBridge();
  const location = useLocation();
  const navigate = useNavigate();
  const [dragging, setDragging] = useState(false);
  const hold = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [focused, setFocused] = useState(false);
  const [recent, setRecent] = useState(false);
  const body = useRef<HTMLDivElement>(null);
  const view = classifyPath(location.pathname);
  const sessionId = view.kind === "chat" ? view.sessionId : null;

  useEffect(() => {
    document.documentElement.dataset.window = "hud";
    if (bridge === null) return;
    return bridge.hud.onChanged((state) => {
      if (!state.open) return;
      navigate(state.sessionId === null ? NEW_CHAT_PATH : sessionPath(state.sessionId), {
        replace: true,
      });
    });
  }, [bridge, navigate]);

  const end = () => {
    if (hold.current !== null) {
      clearTimeout(hold.current);
      hold.current = null;
    }
    if (dragging) {
      setDragging(false);
      bridge?.hud.drag({ phase: "end", mode: "move" });
    }
  };

  // activity on the transcript keeps the sheet up for a moment
  useEffect(() => {
    const host = body.current;
    if (host === null || typeof MutationObserver === "undefined") return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const observer = new MutationObserver((records) => {
      if (!records.some((r) => (r.target as Element).closest?.(".transcript") !== null)) return;
      setRecent(true);
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => setRecent(false), HUD_LINGER_MS);
    });
    observer.observe(host, { subtree: true, childList: true, characterData: true });
    return () => {
      observer.disconnect();
      if (timer !== null) clearTimeout(timer);
    };
  }, []);

  // the composer gets the focus when the HUD opens
  useEffect(() => {
    body.current?.querySelector<HTMLElement>(".chat-composer textarea")?.focus();
  }, []);

  const onGripDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || bridge === null) return;
    const target = event.target as Element;
    // only the bar's empty space moves the window
    if (target.closest(INTERACTIVE) !== null || target.closest(".chat-composer") === null) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    hold.current = setTimeout(() => {
      hold.current = null;
      setDragging(true);
      bridge.hud.drag({ phase: "start", mode: "move" });
    }, HUD_HOLD_MS);
  };

  const onEdgeDown = (edge: Edge) => (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || bridge === null) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setDragging(true);
    bridge.hud.drag({ phase: "start", mode: edge });
  };

  const slots = props.slots ?? {};
  const sheet = sessionId === null || focused || recent;
  return (
    <section
      className="hud"
      aria-label={t("hud.label")}
      data-sheet={sheet ? "shown" : "hidden"}
      data-dragging={dragging ? "true" : undefined}
      onFocus={() => setFocused(true)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false);
      }}
    >
      {EDGES.map((edge) => (
        <div
          key={edge}
          className="hud-edge"
          data-edge={edge}
          title={t("hud.resize")}
          onPointerDown={onEdgeDown(edge)}
          onPointerUp={end}
          onPointerCancel={end}
        />
      ))}
      <div
        className="hud-body"
        ref={body}
        onPointerDown={onGripDown}
        onPointerUp={end}
        onPointerCancel={end}
        onLostPointerCapture={end}
      >
        {sessionId === null ? (
          <>
            <div className="chat-composer hud-placeholder-bar" title={t("hud.grip")}>
              <span>{t("hud.placeholder")}</span>
            </div>
            <p className="hud-row">{t("hud.empty")}</p>
          </>
        ) : (
          <ChatView
            sessionId={sessionId}
            composer={slots.composer?.(sessionId)}
            cards={slots.cards?.(sessionId)}
          />
        )}
        <div className="hud-tools">
          <button
            type="button"
            className="hud-tool"
            title={t("hud.reset")}
            aria-label={t("hud.reset")}
            onClick={() => void bridge?.hud.resetLayout().catch(() => {})}
          >
            <Undo2 size={14} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="hud-tool"
            title={t("hud.leave")}
            aria-label={t("hud.close")}
            onClick={() => void bridge?.hud.close().catch(() => {})}
          >
            <Minimize2 size={14} aria-hidden="true" />
          </button>
        </div>
      </div>
    </section>
  );
}
