// The HUD window (P20-35): one conversation in a small floating card — a grip bar (press and hold
// to move), the transcript and the composer of the chat slots, and edge handles to resize. Moving
// and resizing are main's job: this window only says "held" and "let go" (`hud.drag`). When main
// points the HUD at another session (`hud.onChanged`) the route follows.

import "../ambient/ambient.css";
import { GripHorizontal, LayoutGrid, X } from "lucide-react";
import { type PointerEvent, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import type { AppSlots } from "../app/App.tsx";
import { desktopBridge } from "../app/bridge.ts";
import { classifyPath, NEW_CHAT_PATH, sessionPath } from "../app/routes.ts";
import { ChatView } from "../chat/ChatView.tsx";
import { useT } from "../i18n/index.tsx";

/** A press on the grip turns into a move after this long (a click stays a click). */
export const HUD_HOLD_MS = 150;

type Edge = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
const EDGES: readonly Edge[] = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];

export function Hud(props: { slots?: AppSlots }) {
  const t = useT();
  const bridge = desktopBridge();
  const location = useLocation();
  const navigate = useNavigate();
  const [dragging, setDragging] = useState(false);
  const hold = useRef<ReturnType<typeof setTimeout> | null>(null);
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

  const onGripDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || bridge === null) return;
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
  return (
    <section className="hud" aria-label={t("hud.label")}>
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
      <div className="hud-bar">
        <div
          className="hud-grip"
          data-dragging={dragging ? "true" : "false"}
          title={t("hud.grip")}
          onPointerDown={onGripDown}
          onPointerUp={end}
          onPointerCancel={end}
          onLostPointerCapture={end}
        >
          <GripHorizontal size={14} aria-hidden="true" />
          <span>{t("hud.label")}</span>
        </div>
        <button
          type="button"
          className="icon-button"
          title={t("hud.reset")}
          aria-label={t("hud.reset")}
          onClick={() => void bridge?.hud.resetLayout().catch(() => {})}
        >
          <LayoutGrid size={14} aria-hidden="true" />
        </button>
        <button
          type="button"
          className="icon-button"
          title={t("hud.close")}
          aria-label={t("hud.close")}
          onClick={() => void bridge?.hud.close().catch(() => {})}
        >
          <X size={14} aria-hidden="true" />
        </button>
      </div>
      <div className="hud-body">
        {sessionId === null ? (
          <p className="hud-row">{t("hud.empty")}</p>
        ) : (
          <ChatView
            sessionId={sessionId}
            composer={slots.composer?.(sessionId)}
            cards={slots.cards?.(sessionId)}
          />
        )}
      </div>
    </section>
  );
}
