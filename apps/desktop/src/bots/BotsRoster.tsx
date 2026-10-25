// The sidebar's BOTS panel (W52-k): the bot roster in place of the sessions list. A row opens the
// bot's forever-chat (`CanonicalChats`, identity from the daemon); its menu holds the face, the
// picture, hide / unhide and the section. Group rooms are listed under the bots and open in the
// Bots page (deep link `/bots`). "Refresh gateway groups" reloads `bots.list` and `groups.list`.

import { Bell, BellOff, EllipsisVertical, Plus } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useT } from "../i18n/index.tsx";
import {
  Button,
  ContextMenuAt,
  IconButton,
  MenuLabel,
  MenuRow,
  MenuSeparator,
  SmallDialog,
} from "../ui/components.tsx";
import { type Avatar, AvatarView, readAvatarFile } from "./avatar.tsx";
import type { BotRow } from "./BotsPage.tsx";
import { CanonicalChats, type RpcLike } from "./canonicalChat.ts";
import "./roster.css";

interface RoomRow {
  room_id: string;
  name: string;
}

const TOASTS_KEY = "indus.bots.activityToasts";

function readToasts(): boolean {
  try {
    return window.localStorage.getItem(TOASTS_KEY) !== "0";
  } catch {
    return true;
  }
}

/** "15m", "3h", "2d" from epoch milliseconds. */
export function shortAgo(ms: number | null | undefined, now = Date.now()): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms <= 0) return "";
  const minutes = Math.max(0, Math.round((now - ms) / 60_000));
  if (minutes < 60) return `${Math.max(1, minutes)}m`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)}h`;
  return `${Math.round(minutes / (60 * 24))}d`;
}

export interface BotPreview {
  readonly text: string | null;
  readonly lastActive: number | null;
}

export function BotsRoster(props: {
  rpc: RpcLike;
  chats?: CanonicalChats;
  activeSessionId: string | null;
  onOpenChat: (sessionId: string) => void;
  /** The newest message of a session the sidebar knows (preview + time), if any. */
  previewOf?: (sessionId: string) => BotPreview | null;
  onNewBot?: () => void;
  onOpenRooms?: () => void;
}) {
  const t = useT();
  const { rpc } = props;
  const chats = useMemo(() => props.chats ?? new CanonicalChats(rpc), [props.chats, rpc]);
  const [bots, setBots] = useState<BotRow[] | null>(null);
  const [rooms, setRooms] = useState<RoomRow[]>([]);
  const [error, setError] = useState("");
  const [serial, setSerial] = useState(0);
  const [toasts, setToasts] = useState(readToasts);
  const [menu, setMenu] = useState<{ bot: BotRow; x: number; y: number } | null>(null);
  const [addMenu, setAddMenu] = useState<{ x: number; y: number } | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [naming, setNaming] = useState<{ bot: BotRow; name: string } | null>(null);
  const upload = useRef<HTMLInputElement>(null);
  const uploadFor = useRef<BotRow | null>(null);

  // biome-ignore lint/correctness/useExhaustiveDependencies: `serial` asks for a reload
  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const answer = (await rpc.request("bots.list", {})) as { bots: BotRow[] };
        if (!live) return;
        for (const bot of answer.bots) chats.remember(bot.profile, bot.canonical);
        setBots(answer.bots);
        setError("");
      } catch (failure) {
        if (live) setError(failure instanceof Error ? failure.message : t("manage.failed"));
      }
      try {
        const listed = (await rpc.request("groups.list", {})) as { rooms: RoomRow[] };
        if (live) setRooms(listed.rooms);
      } catch {
        if (live) setRooms([]);
      }
    })();
    return () => {
      live = false;
    };
  }, [rpc, chats, t, serial]);

  const update = async (
    bot: BotRow,
    change: Partial<Pick<BotRow, "hidden" | "sections" | "avatar">>,
  ) => {
    try {
      await rpc.request("bots.update", { profile: bot.profile, ...change });
      setSerial((n) => n + 1);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("manage.failed"));
    }
  };

  const open = async (bot: BotRow) => {
    try {
      props.onOpenChat(await chats.open(bot.profile));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("manage.failed"));
    }
  };

  const all = bots ?? [];
  const sectionNames = [...new Set(all.flatMap((bot) => bot.sections))].sort();
  const shown = all.filter((bot) => !bot.hidden);
  const hidden = all.filter((bot) => bot.hidden);
  const groups = new Map<string, BotRow[]>();
  for (const bot of shown) {
    const key = bot.sections[0] ?? "";
    groups.set(key, [...(groups.get(key) ?? []), bot]);
  }

  const row = (bot: BotRow) => {
    const sessionId = bot.canonical?.session_id ?? null;
    const preview = sessionId === null ? null : (props.previewOf?.(sessionId) ?? null);
    const name = bot.profile;
    return (
      <li
        key={bot.profile}
        className="roster-row"
        data-bot={bot.profile}
        data-selected={
          sessionId !== null && sessionId === props.activeSessionId ? "true" : undefined
        }
        onContextMenu={(event) => {
          event.preventDefault();
          setMenu({ bot, x: event.clientX, y: event.clientY });
        }}
      >
        <button type="button" className="roster-main" onClick={() => void open(bot)}>
          <AvatarView avatar={bot.avatar} fallbackSeed={bot.profile} label={bot.profile} />
          <span className="roster-text">
            <span className="roster-line">
              <span className="roster-ring" aria-hidden="true" />
              <span className="roster-name">{name}</span>
              <span className="roster-ago">{shortAgo(preview?.lastActive)}</span>
            </span>
            <span className="roster-preview">
              {preview?.text ?? t("manage.bots.routines", { count: bot.routines.length })}
            </span>
          </span>
        </button>
        <IconButton
          className="roster-kebab"
          label={t("manage.bots.rowMenu", { name })}
          icon={<EllipsisVertical aria-hidden="true" size={13} />}
          size="sm"
          onClick={(event) => {
            const box = event.currentTarget.getBoundingClientRect();
            setMenu({ bot, x: box.left, y: box.bottom });
          }}
        />
      </li>
    );
  };

  return (
    <section className="roster" aria-label={t("manage.bots.label")}>
      <button type="button" className="roster-refresh" onClick={() => setSerial((n) => n + 1)}>
        {t("manage.bots.refreshGroups")}
      </button>
      <div className="roster-head">
        <span className="roster-label">{t("manage.bots.label")}</span>
        <span className="roster-head-tools">
          <IconButton
            label={t(toasts ? "manage.bots.toastsOn" : "manage.bots.toastsOff")}
            icon={
              toasts ? (
                <Bell aria-hidden="true" size={13} />
              ) : (
                <BellOff aria-hidden="true" size={13} />
              )
            }
            size="sm"
            aria-pressed={toasts}
            onClick={() => {
              const next = !toasts;
              setToasts(next);
              try {
                window.localStorage.setItem(TOASTS_KEY, next ? "1" : "0");
              } catch {
                // not kept
              }
            }}
          />
          <IconButton
            label={t("manage.bots.add")}
            icon={<Plus aria-hidden="true" size={13} />}
            size="sm"
            onClick={(event) => {
              const box = event.currentTarget.getBoundingClientRect();
              setAddMenu({ x: box.left, y: box.bottom });
            }}
          />
        </span>
      </div>
      {error !== "" && (
        <p className="roster-error" role="alert">
          {error}
        </p>
      )}
      {bots !== null && all.length === 0 && <p className="roster-empty">{t("manage.bots.none")}</p>}
      {[...groups.entries()].map(([section, rows]) => (
        <div key={section || "-"} className="roster-group">
          {section !== "" && <p className="roster-section">{`${section} · ${rows.length}`}</p>}
          <ul className="roster-list">{rows.map(row)}</ul>
        </div>
      ))}
      {hidden.length > 0 && (
        <div className="roster-group">
          <button
            type="button"
            className="roster-section roster-section-toggle"
            aria-expanded={showHidden}
            onClick={() => setShowHidden(!showHidden)}
          >
            {t("manage.bots.hiddenGroup", { count: hidden.length })}
          </button>
          {showHidden && <ul className="roster-list">{hidden.map(row)}</ul>}
        </div>
      )}
      {rooms.length > 0 && (
        <div className="roster-group">
          <p className="roster-section">{t("manage.bots.groupsLabel")}</p>
          <ul className="roster-list">
            {rooms.map((room) => (
              <li key={room.room_id} className="roster-row">
                <button
                  type="button"
                  className="roster-main roster-room"
                  onClick={props.onOpenRooms}
                >
                  <span className="roster-name">{room.name}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
      <input
        ref={upload}
        type="file"
        accept="image/png,image/jpeg,image/webp"
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0];
          const bot = uploadFor.current;
          event.target.value = "";
          if (file === undefined || bot === null) return;
          void readAvatarFile(file).then(
            (data) => update(bot, { avatar: { kind: "upload", data } as Avatar }),
            (failure: Error) => setError(failure.message),
          );
        }}
      />
      {menu !== null && (
        <ContextMenuAt
          label={t("manage.bots.rowMenu", { name: menu.bot.profile })}
          x={menu.x}
          y={menu.y}
          onClose={() => setMenu(null)}
        >
          <MenuRow
            label={t("manage.bots.newFace")}
            onSelect={() => {
              const bot = menu.bot;
              setMenu(null);
              void update(bot, { avatar: { kind: "face", seed: `${bot.profile}-${Date.now()}` } });
            }}
          />
          <MenuRow
            label={t("manage.bots.upload")}
            onSelect={() => {
              uploadFor.current = menu.bot;
              setMenu(null);
              upload.current?.click();
            }}
          />
          <MenuRow
            label={t(menu.bot.hidden ? "manage.bots.unhide" : "manage.bots.hide")}
            onSelect={() => {
              const bot = menu.bot;
              setMenu(null);
              void update(bot, { hidden: !bot.hidden });
            }}
          />
          <MenuSeparator />
          <MenuLabel>{t("manage.bots.moveTo")}</MenuLabel>
          {sectionNames.map((name) => (
            <MenuRow
              key={name}
              kind="radio"
              label={name}
              checked={menu.bot.sections[0] === name}
              onSelect={() => {
                const bot = menu.bot;
                setMenu(null);
                void update(bot, { sections: [name] });
              }}
            />
          ))}
          <MenuRow
            kind="radio"
            label={t("manage.bots.noSection")}
            checked={menu.bot.sections.length === 0}
            onSelect={() => {
              const bot = menu.bot;
              setMenu(null);
              void update(bot, { sections: [] });
            }}
          />
          <MenuRow
            label={t("manage.bots.newSection")}
            onSelect={() => {
              setNaming({ bot: menu.bot, name: "" });
              setMenu(null);
            }}
          />
        </ContextMenuAt>
      )}
      {naming !== null && (
        <SmallDialog
          title={t("manage.bots.newSection")}
          onClose={() => setNaming(null)}
          actions={
            <>
              <Button variant="ghost" size="sm" onClick={() => setNaming(null)}>
                {t("manage.messaging.cancel")}
              </Button>
              <Button
                variant="primary"
                size="sm"
                disabled={naming.name.trim() === ""}
                onClick={() => {
                  const { bot, name } = naming;
                  setNaming(null);
                  void update(bot, { sections: [name.trim()] });
                }}
              >
                {t("manage.save")}
              </Button>
            </>
          }
        >
          <input
            className="desk-input"
            aria-label={t("manage.bots.sectionName")}
            value={naming.name}
            onChange={(event) => setNaming({ ...naming, name: event.target.value })}
          />
        </SmallDialog>
      )}
      {addMenu !== null && (
        <ContextMenuAt
          label={t("manage.bots.add")}
          x={addMenu.x}
          y={addMenu.y}
          onClose={() => setAddMenu(null)}
        >
          <MenuRow
            label={t("manage.bots.newBot")}
            onSelect={() => {
              setAddMenu(null);
              props.onNewBot?.();
            }}
          />
        </ContextMenuAt>
      )}
    </section>
  );
}
