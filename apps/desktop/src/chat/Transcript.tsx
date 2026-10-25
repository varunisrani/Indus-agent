// The transcript of one session (P20-18). Only a window of the newest items is in the DOM; older
// ones page in when the reader scrolls to the top (or presses "Show earlier"). While the reader
// is at the bottom the view follows the stream; scrolling up stops following and shows "Scroll
// to bottom". Each session's reading position is kept for when it is shown again.

import { ArrowDown } from "lucide-react";
import {
  type ReactNode,
  type UIEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { useT } from "../i18n/index.tsx";
import { ErrorCard } from "./ErrorCard.tsx";
import { type FailureAction, failureEcho } from "./failure.ts";
import { Markdown } from "./Markdown.tsx";
import type {
  AssistantItem,
  AsyncItem,
  ErrorItem,
  NoticeItem,
  TranscriptItem,
  UserItem,
} from "./messageStream.ts";
import { Timeline } from "./Timeline.tsx";
import { ToolRow } from "./ToolRow.tsx";
import { TurnActivity } from "./TurnActivity.tsx";
import { useShowReasoning, useTranscript } from "./transcriptReact.tsx";
import "./transcript.css";

/** Items rendered at first, and added per page. */
export const TRANSCRIPT_PAGE = 60;
/** Within this many pixels of the end counts as "at the bottom". */
const BOTTOM_SLACK = 32;
/** Within this many pixels of the top pages older items in. */
const TOP_SLACK = 48;

interface ReadingPosition {
  windowSize: number;
  following: boolean;
  scrollTop: number;
}

const positions = new Map<string, ReadingPosition>();

/** Forget saved reading positions (tests). */
export function resetReadingPositions(): void {
  positions.clear();
}

export interface TranscriptProps {
  sessionId: string;
  onFailureAction?: (action: FailureAction, item: ErrorItem) => void;
  /** Rendered after the last item, inside the scroll area (the card stack host). */
  footer?: ReactNode;
}

/**
 * When this window sent a prompt (W52-g). Only prompts typed here have a time: the stored history
 * carries none, so a reloaded row shows no time rather than a guessed one.
 */
const sentTimes = new Map<string, number>();

function sentAt(sessionId: string, item: UserItem): number | null {
  const key = `${sessionId}\u0000${item.text}`;
  if (!item.persisted && item.localId !== null && !sentTimes.has(key)) {
    sentTimes.set(key, Date.now());
  }
  return sentTimes.get(key) ?? null;
}

/** Forget prompt times (tests). */
export function resetSentTimes(): void {
  sentTimes.clear();
}

function UserRow(props: { item: UserItem; sessionId: string }) {
  const t = useT();
  const time = sentAt(props.sessionId, props.item);
  const label =
    time === null
      ? null
      : new Date(time).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return (
    <div className="msg msg-user" data-key={props.item.key} data-persisted={props.item.persisted}>
      <span className="sr-only">{t("transcript.you")}</span>
      <div className="msg-bubble">{props.item.text}</div>
      {label !== null && (
        <span className="msg-user-meta">
          <time
            dateTime={new Date(time ?? 0).toISOString()}
            title={t("transcript.sentAt", { time: label })}
          >
            {label}
          </time>
        </span>
      )}
    </div>
  );
}

function AssistantRow(props: { item: AssistantItem; showReasoning: boolean; hideEcho: boolean }) {
  const { item, showReasoning } = props;
  const t = useT();
  const lastTool = item.parts.findLastIndex((part) => part.type === "tool");
  const lastIndex = item.parts.length - 1;
  return (
    <div className="msg msg-assistant" data-key={item.key} data-streaming={item.streaming}>
      <span className="sr-only">{t("transcript.assistant")}</span>
      {item.parts.map((part, index) => {
        const key = part.type === "tool" ? `tool-${part.callId}` : `${part.type}-${index}`;
        if (part.type === "tool")
          return <ToolRow key={key} part={part} settled={!item.streaming} />;
        if (props.hideEcho && index === lastIndex) return null;
        if (part.type === "reasoning") {
          if (!showReasoning) return null;
          return (
            <details
              key={key}
              className="reasoning"
              open={item.streaming && index === item.parts.length - 1}
            >
              <summary>{t("transcript.reasoning")}</summary>
              <p>{part.text}</p>
            </details>
          );
        }
        return (
          <div key={key} className={index < lastTool ? "msg-text msg-interim" : "msg-text"}>
            <Markdown text={part.text} />
          </div>
        );
      })}
      {item.streaming && <TurnActivity turnKey={item.key} />}
      {item.interrupted && <p className="msg-note">{t("transcript.interrupted")}</p>}
    </div>
  );
}

function AsyncRow(props: { item: AsyncItem }) {
  const t = useT();
  const title =
    props.item.title === "review" ? t("transcript.async.review") : t("transcript.async.heartbeat");
  return (
    <details className="msg msg-async" data-key={props.item.key}>
      <summary>{title}</summary>
      <ul>
        {props.item.lines.map((line, index) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: summary lines are positional
          <li key={index}>{line}</li>
        ))}
      </ul>
    </details>
  );
}

function NoticeRow(props: { item: NoticeItem }) {
  return (
    <p className="msg msg-notice" data-level={props.item.level} data-key={props.item.key}>
      {props.item.text}
    </p>
  );
}

function Row(props: {
  item: TranscriptItem;
  sessionId: string;
  showReasoning: boolean;
  /** The failure sentence the turn's reply repeated (shown in the card's details, not above it). */
  echo: { assistantKey: string; text: string } | null;
  onFailureAction?: ((action: FailureAction, item: ErrorItem) => void) | undefined;
}) {
  const { item } = props;
  switch (item.kind) {
    case "user":
      return <UserRow item={item} sessionId={props.sessionId} />;
    case "assistant":
      return (
        <AssistantRow
          item={item}
          showReasoning={props.showReasoning}
          hideEcho={props.echo?.assistantKey === item.key}
        />
      );
    case "error":
      return (
        <ErrorCard item={item} echo={props.echo?.text ?? null} onAction={props.onFailureAction} />
      );
    case "async":
      return <AsyncRow item={item} />;
    case "notice":
      return <NoticeRow item={item} />;
  }
}

/**
 * A failed turn's reply often ends with the backend's own failure sentence; the error card says the
 * same thing, so that last text moves into the card's details instead of showing twice.
 */
function echoOf(group: readonly TranscriptItem[]): { assistantKey: string; text: string } | null {
  const error = group.find((item): item is ErrorItem => item.kind === "error");
  if (error === undefined) return null;
  const reply = group.find(
    (item): item is AssistantItem => item.kind === "assistant" && item.turnId === error.turnId,
  );
  const last = reply?.parts[reply.parts.length - 1];
  if (reply === undefined || last === undefined || last.type !== "text") return null;
  return failureEcho(last.text) ? { assistantKey: reply.key, text: last.text.trim() } : null;
}

/** Group items into turns so the prompt row can stick to the top while its answer scrolls. */
function turns(items: readonly TranscriptItem[]): TranscriptItem[][] {
  const groups: TranscriptItem[][] = [];
  for (const item of items) {
    if (item.kind === "user" || groups.length === 0) groups.push([item]);
    else groups[groups.length - 1]?.push(item);
  }
  return groups;
}

export function Transcript(props: TranscriptProps) {
  const { sessionId } = props;
  const t = useT();
  const transcript = useTranscript(sessionId);
  const showReasoning = useShowReasoning();
  const saved = positions.get(sessionId);
  const [windowSize, setWindowSize] = useState(saved?.windowSize ?? TRANSCRIPT_PAGE);
  const [following, setFollowing] = useState(saved?.following ?? true);
  const [jumpTo, setJumpTo] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const keepOffset = useRef<number | null>(null);
  const state = useRef({ windowSize, following, scrollTop: saved?.scrollTop ?? 0 });
  state.current.windowSize = windowSize;
  state.current.following = following;

  const items = transcript.items;
  const start = Math.max(0, items.length - windowSize);
  const visible = items.slice(start);
  const prompts = items.filter((item): item is UserItem => item.kind === "user");

  // Restore this session's reading position once; save it when the view goes away.
  // biome-ignore lint/correctness/useExhaustiveDependencies: restore on mount only
  useLayoutEffect(() => {
    const element = scroller.current;
    if (element !== null && saved !== undefined && !saved.following) {
      element.scrollTop = saved.scrollTop;
    }
    return () => {
      positions.set(sessionId, { ...state.current });
    };
  }, [sessionId]);

  // Follow the stream, or keep the reader's place when older items were paged in above.
  useLayoutEffect(() => {
    const element = scroller.current;
    if (element === null) return;
    if (keepOffset.current !== null) {
      element.scrollTop = element.scrollHeight - keepOffset.current;
      keepOffset.current = null;
      return;
    }
    if (following) element.scrollTop = element.scrollHeight;
  });

  useEffect(() => {
    if (jumpTo === null) return;
    const rows = scroller.current?.querySelectorAll<HTMLElement>("[data-key]") ?? [];
    const element = [...rows].find((row) => row.dataset.key === jumpTo);
    if (element !== undefined) {
      element.scrollIntoView({ block: "start" });
      setJumpTo(null);
    }
  }, [jumpTo]);

  const showEarlier = useCallback(() => {
    const element = scroller.current;
    if (element !== null) keepOffset.current = element.scrollHeight - element.scrollTop;
    setWindowSize((size) => size + TRANSCRIPT_PAGE);
  }, []);

  const onScroll = (event: UIEvent<HTMLDivElement>) => {
    const element = event.currentTarget;
    state.current.scrollTop = element.scrollTop;
    const atBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight <= BOTTOM_SLACK;
    if (atBottom !== following) setFollowing(atBottom);
    if (element.scrollTop <= TOP_SLACK && start > 0 && keepOffset.current === null) showEarlier();
  };

  const jump = (key: string) => {
    const index = items.findIndex((item) => item.key === key);
    if (index < 0) return;
    if (index < start) setWindowSize(items.length - index);
    setFollowing(false);
    setJumpTo(key);
  };

  const toBottom = () => {
    setFollowing(true);
    const element = scroller.current;
    if (element !== null) element.scrollTop = element.scrollHeight;
  };

  return (
    <div className="transcript">
      <div
        ref={scroller}
        className="transcript-scroll"
        role="log"
        aria-label={t("transcript.label")}
        aria-live="polite"
        aria-relevant="additions"
        onScroll={onScroll}
        tabIndex={-1}
      >
        {start > 0 && (
          <button type="button" className="text-button transcript-earlier" onClick={showEarlier}>
            {t("transcript.showEarlier")}
          </button>
        )}
        {turns(visible).map((group) => {
          const echo = echoOf(group);
          return (
            <section key={group[0]?.key} className="turn">
              {group.map((item) => (
                <Row
                  key={item.key}
                  item={item}
                  sessionId={sessionId}
                  showReasoning={showReasoning}
                  echo={echo}
                  onFailureAction={props.onFailureAction}
                />
              ))}
            </section>
          );
        })}
        {props.footer}
      </div>
      <Timeline prompts={prompts} onJump={jump} />
      {!following && (
        <button
          type="button"
          className="button button-floating transcript-bottom"
          onClick={toBottom}
        >
          <ArrowDown aria-hidden="true" size={14} />
          {t("transcript.scrollToBottom")}
        </button>
      )}
    </div>
  );
}
