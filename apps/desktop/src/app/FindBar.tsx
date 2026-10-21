// The find bar (P20-39): Ctrl/Cmd+F opens it over the chat (on Linux main claims Ctrl+F and sends
// `find.onOpen`), typing searches the rendered transcript, Enter / Shift+Enter step through the
// matches, Esc closes it and clears the highlights. Rows streaming in re-run the search.

import { ChevronDown, ChevronUp, X } from "lucide-react";
import {
  type KeyboardEvent,
  StrictMode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";
import type { IndusDesktopBridge } from "../global.d.ts";
import { I18nProvider, useT } from "../i18n/index.tsx";
import { desktopBridge } from "./bridge.ts";
import {
  cssHighlights,
  EMPTY_FIND,
  type FindState,
  type HighlightRegistry,
  TranscriptFinder,
} from "./findInTranscript.ts";

/** Debounce for re-searching while the transcript changes. */
export const FIND_REFRESH_MS = 150;

export const defaultFindRoot = (): Element | null => document.querySelector(".transcript");

export interface FindBarProps {
  root?: () => Element | null;
  highlights?: HighlightRegistry | null;
  bridge?: Pick<IndusDesktopBridge, "find"> | null;
}

export function isFindKey(
  event: Pick<globalThis.KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey">,
): boolean {
  return (
    (event.ctrlKey || event.metaKey) &&
    !event.altKey &&
    !event.shiftKey &&
    event.key.toLowerCase() === "f"
  );
}

export function FindBar(props: FindBarProps) {
  const t = useT();
  const root = props.root ?? defaultFindRoot;
  const finder = useMemo(
    () =>
      new TranscriptFinder({
        root,
        highlights: props.highlights === undefined ? cssHighlights() : props.highlights,
      }),
    [root, props.highlights],
  );
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [state, setState] = useState<FindState>(EMPTY_FIND);
  const input = useRef<HTMLInputElement | null>(null);

  // Each open request (also while already open) focuses and selects the query.
  const [focusRequest, setFocusRequest] = useState(0);
  const show = useCallback(() => {
    setOpen(true);
    setFocusRequest((n) => n + 1);
  }, []);

  useEffect(() => {
    if (!open || focusRequest === 0) return;
    input.current?.focus();
    input.current?.select();
  }, [open, focusRequest]);

  const close = useCallback(() => {
    setOpen(false);
    setState(finder.clear());
  }, [finder]);

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (!isFindKey(event)) return;
      event.preventDefault();
      show();
    };
    window.addEventListener("keydown", onKey);
    const bridge = props.bridge === undefined ? desktopBridge() : props.bridge;
    const unsubscribe = bridge?.find?.onOpen(() => show());
    return () => {
      window.removeEventListener("keydown", onKey);
      unsubscribe?.();
    };
  }, [show, props.bridge]);

  // Streaming rows: search again (debounced) while the bar is open.
  useEffect(() => {
    if (!open) return;
    const target = root();
    if (target === null || typeof MutationObserver === "undefined") return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const observer = new MutationObserver(() => {
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => setState(finder.refresh()), FIND_REFRESH_MS);
    });
    observer.observe(target, { childList: true, subtree: true, characterData: true });
    return () => {
      observer.disconnect();
      if (timer !== null) clearTimeout(timer);
    };
  }, [open, root, finder]);

  useEffect(() => () => void finder.clear(), [finder]);

  if (!open) return null;

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else if (event.key === "Enter") {
      event.preventDefault();
      setState(event.shiftKey ? finder.previous() : finder.next());
    }
  };

  const count =
    state.query === ""
      ? ""
      : state.total === 0
        ? t("find.none")
        : t("find.count", { current: state.current, total: state.total });

  return (
    <search className="find-bar" data-find-skip="">
      <input
        ref={input}
        className="find-input"
        type="search"
        aria-label={t("find.label")}
        placeholder={t("find.placeholder")}
        value={query}
        spellCheck={false}
        onChange={(event) => {
          setQuery(event.target.value);
          setState(finder.search(event.target.value));
        }}
        onKeyDown={onKeyDown}
      />
      <span className="find-count" aria-live="polite">
        {count}
      </span>
      <button
        type="button"
        className="icon-button"
        aria-label={t("find.previous")}
        disabled={state.total === 0}
        onClick={() => setState(finder.previous())}
      >
        <ChevronUp aria-hidden="true" size={16} />
      </button>
      <button
        type="button"
        className="icon-button"
        aria-label={t("find.next")}
        disabled={state.total === 0}
        onClick={() => setState(finder.next())}
      >
        <ChevronDown aria-hidden="true" size={16} />
      </button>
      <button type="button" className="icon-button" aria-label={t("common.close")} onClick={close}>
        <X aria-hidden="true" size={16} />
      </button>
    </search>
  );
}

/** Mount the find bar in its own root (shell windows; called once from `main.tsx`). */
export function mountFindBar(host: HTMLElement = document.body): void {
  const element = document.createElement("div");
  element.className = "find-root";
  host.appendChild(element);
  createRoot(element).render(
    <StrictMode>
      <I18nProvider>
        <FindBar />
      </I18nProvider>
    </StrictMode>,
  );
}
