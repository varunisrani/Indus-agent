// The composer's completion drawer (W52-h): slash commands grouped by catalogue category, `@`
// references with a glyph per kind, an empty and a "looking" state. Keys stay in the composer
// (the textarea keeps focus; a mouse-down here never blurs it).

import type { CompletionItem } from "@indus/protocol";
import {
  Columns2,
  File,
  Folder,
  GitBranch,
  Globe,
  LoaderCircle,
  Puzzle,
  SquareTerminal,
} from "lucide-react";
import { Fragment, type ReactNode, useEffect, useRef } from "react";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import { useHoverTip } from "../../ui/components.tsx";
import { CATEGORY_ORDER, categoryOf } from "../slash.ts";
import type { CompletionState } from "./completion.ts";

export interface DrawerGroup {
  /** Catalogue category, "other", a mention scope, or null for an unlabelled list. */
  readonly key: string | null;
  readonly items: ReadonlyArray<{ item: CompletionItem; index: number }>;
}

/**
 * The rows in display groups. Slash items group by category in catalogue order (unknown names —
 * plugins, skills — last as "other"). The source already sorted the flat list the same way
 * (`orderByCategory`), so the arrow keys walk the rows in the order they are shown.
 */
export function groupItems(state: CompletionState): DrawerGroup[] {
  const trigger = state.trigger;
  if (trigger === null) return [];
  const indexed = state.items.map((item, index) => ({ item, index }));
  if (trigger.kind === "mention") {
    const word = trigger.word.toLowerCase();
    const scope = word.startsWith("@file:")
      ? "file"
      : word.startsWith("@folder:")
        ? "folder"
        : null;
    return [{ key: scope, items: indexed }];
  }
  const groups = new Map<string, Array<{ item: CompletionItem; index: number }>>();
  for (const entry of indexed) {
    const key = categoryOf(entry.item.text) ?? "other";
    const list = groups.get(key) ?? [];
    list.push(entry);
    groups.set(key, list);
  }
  const rank = (key: string) => {
    const at = CATEGORY_ORDER.indexOf(key);
    return at === -1 ? CATEGORY_ORDER.length : at;
  };
  return [...groups.entries()]
    .sort((a, b) => rank(a[0]) - rank(b[0]))
    .map(([key, items]) => ({ key, items }));
}

const GROUP_KEYS: Readonly<Record<string, MessageKey>> = {
  session: "drawer.group.session",
  config: "drawer.group.config",
  tools: "drawer.group.tools",
  info: "drawer.group.info",
  exit: "drawer.group.exit",
  other: "drawer.group.other",
  file: "drawer.scope.file",
  folder: "drawer.scope.folder",
};

/** The glyph and its tone for one row. */
export function iconFor(
  kind: "slash" | "mention",
  item: CompletionItem,
): {
  icon: ReactNode;
  tone: "accent" | "muted";
} {
  const size = { "aria-hidden": true as const };
  if (kind === "slash") {
    return categoryOf(item.text) === null
      ? { icon: <Puzzle {...size} />, tone: "muted" }
      : { icon: <SquareTerminal {...size} />, tone: "accent" };
  }
  const text = item.text.replace(/^@/, "").toLowerCase();
  if (text.startsWith("diff")) return { icon: <Columns2 {...size} />, tone: "accent" };
  if (text.startsWith("git:")) return { icon: <GitBranch {...size} />, tone: "accent" };
  if (text.startsWith("url:")) return { icon: <Globe {...size} />, tone: "accent" };
  if (text.startsWith("folder:") || text.endsWith("/"))
    return { icon: <Folder {...size} />, tone: "muted" };
  return { icon: <File {...size} />, tone: "muted" };
}

function Row(props: {
  item: CompletionItem;
  index: number;
  kind: "slash" | "mention";
  listId: string;
  selected: boolean;
  onPick: (item: CompletionItem) => void;
}) {
  const { icon, tone } = iconFor(props.kind, props.item);
  const ref = useRef<HTMLDivElement>(null);
  const hover = useHoverTip(props.item.meta ? props.item.meta : null);
  useEffect(() => {
    // keep the highlighted row in view inside the drawer only (never scroll the transcript)
    const row = ref.current;
    const box = row?.parentElement?.closest(".desk-popover");
    if (!props.selected || row === null || !(box instanceof HTMLElement)) return;
    const top = row.offsetTop;
    if (top < box.scrollTop) box.scrollTop = top - 4;
    else if (top + row.offsetHeight > box.scrollTop + box.clientHeight)
      box.scrollTop = top + row.offsetHeight - box.clientHeight + 4;
  }, [props.selected]);
  return (
    <div
      ref={ref}
      id={`${props.listId}-${props.index}`}
      role="option"
      tabIndex={-1}
      aria-selected={props.selected}
      className="desk-popover-row"
      onMouseDown={(event) => {
        event.preventDefault();
        props.onPick(props.item);
      }}
      {...hover.bind}
    >
      <span className="desk-popover-icon" data-tone={tone}>
        {icon}
      </span>
      <span className="desk-popover-name">{props.item.display}</span>
      {props.item.meta ? <span className="desk-popover-meta">{props.item.meta}</span> : null}
      {hover.tip}
    </div>
  );
}

export function CompletionDrawer(props: {
  state: CompletionState;
  listId: string;
  onPick: (item: CompletionItem) => void;
}) {
  const t = useT();
  const { state } = props;
  const trigger = state.trigger;
  if (trigger === null) return null;
  const groups = groupItems(state);
  const empty = state.items.length === 0;
  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: keeps the editor's focus on a click
    <div
      className="desk-popover"
      data-kind={trigger.kind}
      onMouseDown={(event) => event.preventDefault()}
    >
      {empty ? (
        state.loading ? (
          <p className="desk-popover-empty" role="status">
            <LoaderCircle aria-hidden="true" size={12} className="spin" /> {t("drawer.loading")}
          </p>
        ) : (
          <p className="desk-popover-empty" role="status">
            {t("drawer.empty")}
            <span>
              {t(trigger.kind === "mention" ? "drawer.emptyMention" : "drawer.emptySlash")}
            </span>
          </p>
        )
      ) : (
        <div role="listbox" id={props.listId} aria-label={t("composer.completions")}>
          {groups.map((group) => (
            <Fragment key={group.key ?? "all"}>
              {group.key !== null && GROUP_KEYS[group.key] !== undefined ? (
                <div className="desk-popover-head" role="presentation">
                  {t(GROUP_KEYS[group.key] as MessageKey)}
                </div>
              ) : null}
              {group.items.map(({ item, index }) => (
                <Row
                  key={`${item.text}\u0000${item.display}`}
                  item={item}
                  index={index}
                  kind={trigger.kind}
                  listId={props.listId}
                  selected={index === state.selected}
                  onPick={props.onPick}
                />
              ))}
            </Fragment>
          ))}
        </div>
      )}
    </div>
  );
}
