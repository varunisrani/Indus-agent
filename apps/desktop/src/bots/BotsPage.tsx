// The Bots surface (P24-31): every profile as a bot — its avatar, its routines (that profile's cron
// jobs), the sections the person sorted it into and a hidden flag — plus the group rooms. A click
// opens the bot's forever-chat through `CanonicalChats` (identity from the daemon only). The
// surface is a bundled desktop contribution: `registerBotsSurface` adds its route and nav entry to
// the contribution registry (P24-32) and its switch unregisters both live.

import { type ReactNode, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { Loader } from "../app/Loader.tsx";
import { sessionPath } from "../app/routes.ts";
import { type ContributionRegistry, contributions } from "../contrib/registry.ts";
import { useT } from "../i18n/index.tsx";
import "../app/manage/manage.css";
import { type Avatar, AvatarView, readAvatarFile } from "./avatar.tsx";
import { CanonicalChats, type CanonicalRow, type RpcLike } from "./canonicalChat.ts";
import { GroupRoom } from "./GroupRoom.tsx";

export interface BotRow {
  profile: string;
  avatar: Avatar;
  hidden: boolean;
  sections: string[];
  routines: Array<{ id: string; name: string; schedule: string; enabled: boolean }>;
  canonical: CanonicalRow | null;
}

interface RoomRow {
  room_id: string;
  name: string;
  members: Array<Record<string, unknown>>;
}

export function BotsPage(props: {
  rpc: RpcLike;
  chats?: CanonicalChats;
  onOpenChat?: (sessionId: string) => void;
}) {
  const t = useT();
  const navigate = useNavigate();
  const chats = useMemo(
    () => props.chats ?? new CanonicalChats(props.rpc),
    [props.chats, props.rpc],
  );
  const openChat = props.onOpenChat ?? ((id: string) => navigate(sessionPath(id)));
  const [bots, setBots] = useState<BotRow[] | null>(null);
  const [rooms, setRooms] = useState<RoomRow[]>([]);
  const [room, setRoom] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [error, setError] = useState("");
  const [serial, setSerial] = useState(0);
  const { rpc } = props;

  // biome-ignore lint/correctness/useExhaustiveDependencies: `serial` asks for a reload after a change
  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const answer = (await rpc.request("bots.list", {})) as { bots: BotRow[] };
        if (!live) return;
        for (const bot of answer.bots) chats.remember(bot.profile, bot.canonical);
        setBots(answer.bots);
      } catch (failure) {
        if (live) setError(failure instanceof Error ? failure.message : t("manage.failed"));
      }
      try {
        const listed = (await rpc.request("groups.list", {})) as { rooms: RoomRow[] };
        if (live) setRooms(listed.rooms);
      } catch {
        if (live) setRooms([]); // no room support on this daemon
      }
    })();
    return () => {
      live = false;
    };
  }, [rpc, chats, t, serial]);

  const open = async (bot: BotRow) => {
    try {
      openChat(await chats.open(bot.profile));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("manage.failed"));
    }
  };

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

  const visibleBots = (bots ?? []).filter((bot) => showHidden || !bot.hidden);
  const sections = new Map<string, BotRow[]>();
  for (const bot of visibleBots) {
    const key = bot.sections[0] ?? "";
    sections.set(key, [...(sections.get(key) ?? []), bot]);
  }

  return (
    <section className="manage-page" aria-labelledby="bots-title">
      <header className="manage-header">
        <h1 id="bots-title">{t("nav.bots")}</h1>
        <label className="manage-inline">
          <input
            type="checkbox"
            checked={showHidden}
            onChange={(e) => setShowHidden(e.target.checked)}
          />
          {t("manage.bots.showHidden")}
        </label>
      </header>
      {error !== "" && (
        <p className="manage-error" role="alert">
          {error}
        </p>
      )}
      {bots === null && error === "" && <Loader />}
      {[...sections.entries()].map(([section, rows]) => (
        <div className="manage-body" key={section || "-"}>
          {section !== "" && <h2>{section}</h2>}
          <div className="manage-grid">
            {rows.map((bot) => (
              <article className="manage-card" key={bot.profile} data-bot={bot.profile}>
                <button type="button" className="bots-row" onClick={() => void open(bot)}>
                  <AvatarView avatar={bot.avatar} fallbackSeed={bot.profile} label={bot.profile} />
                  <span>
                    <strong>{bot.profile}</strong>
                    <span className="manage-muted">
                      {" "}
                      {t("manage.bots.routines", { count: bot.routines.length })}
                    </span>
                  </span>
                </button>
                <div className="manage-inline">
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={() =>
                      void update(bot, {
                        avatar: { kind: "face", seed: `${bot.profile}-${Date.now()}` },
                      })
                    }
                  >
                    {t("manage.bots.newFace")}
                  </button>
                  <label className="button button-secondary">
                    {t("manage.bots.upload")}
                    <input
                      type="file"
                      accept="image/png,image/jpeg,image/webp"
                      hidden
                      onChange={(e) => {
                        const file = e.target.files?.[0];
                        if (file === undefined) return;
                        void readAvatarFile(file).then(
                          (data) => update(bot, { avatar: { kind: "upload", data } }),
                          (failure: Error) => setError(failure.message),
                        );
                      }}
                    />
                  </label>
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={() => void update(bot, { hidden: !bot.hidden })}
                  >
                    {bot.hidden ? t("manage.bots.unhide") : t("manage.bots.hide")}
                  </button>
                  <input
                    aria-label={t("manage.bots.section", { name: bot.profile })}
                    defaultValue={bot.sections.join(", ")}
                    onBlur={(e) => {
                      const next = e.target.value
                        .split(",")
                        .map((s) => s.trim())
                        .filter((s) => s !== "");
                      if (next.join(",") !== bot.sections.join(","))
                        void update(bot, { sections: next });
                    }}
                  />
                </div>
                {bot.routines.length > 0 && (
                  <ul className="manage-muted">
                    {bot.routines.map((r) => (
                      <li key={r.id}>
                        {r.name || r.id} · {r.schedule}
                        {r.enabled ? "" : ` (${t("manage.bots.paused")})`}
                      </li>
                    ))}
                  </ul>
                )}
              </article>
            ))}
          </div>
        </div>
      ))}
      {rooms.length > 0 && (
        <div className="manage-body">
          <h2>{t("manage.bots.rooms")}</h2>
          <div className="manage-inline">
            {rooms.map((r) => (
              <button
                key={r.room_id}
                type="button"
                className="button button-secondary"
                aria-pressed={room === r.room_id}
                onClick={() => setRoom(room === r.room_id ? null : r.room_id)}
              >
                {r.name}
              </button>
            ))}
          </div>
          {room !== null && (
            <GroupRoom
              rpc={rpc}
              roomId={room}
              onDisbanded={() => {
                setRoom(null);
                setSerial((n) => n + 1);
              }}
            />
          )}
        </div>
      )}
    </section>
  );
}

// -- the bundled contribution ----------------------------------------------------------------------

/** What the Bots surface adds to the shell (P24-32's contribution registry takes this shape). */
export interface BundledContribution {
  id: string;
  routes: Array<{ path: string; render: () => ReactNode }>;
  nav: Array<{ id: string; label: string; path: string }>;
}

/** The registry the surface registers with: `register` answers the function that unregisters. */
export interface ContributionHost {
  register(contribution: BundledContribution): () => void;
}

export const BOTS_CONTRIBUTION_ID = "indus.bots";

export function botsContribution(render: () => ReactNode, label: string): BundledContribution {
  return {
    id: BOTS_CONTRIBUTION_ID,
    routes: [{ path: "/bots", render }],
    nav: [{ id: "bots", label, path: "/bots" }],
  };
}

/** The surface's on/off switch: on registers it, off unregisters it at once (no reload). */
export class BotsSurface {
  private readonly host: ContributionHost;
  private readonly contribution: BundledContribution;
  private unregister: (() => void) | null = null;

  constructor(host: ContributionHost, contribution: BundledContribution) {
    this.host = host;
    this.contribution = contribution;
  }

  enabled(): boolean {
    return this.unregister !== null;
  }

  setEnabled(on: boolean): void {
    if (on && this.unregister === null) this.unregister = this.host.register(this.contribution);
    if (!on && this.unregister !== null) {
      this.unregister();
      this.unregister = null;
    }
  }
}

/** `ContributionHost` over the app's contribution registry (P24-32): a bundled surface becomes one
 * `route` and one `sidebar-nav` contribution per entry, source `app`, registered all at once and
 * dropped together. */
export function registryHost(registry: ContributionRegistry = contributions): ContributionHost {
  return {
    register: (contribution) =>
      registry.registerMany([
        ...contribution.routes.map((route) => ({
          id: `${contribution.id}.route${route.path.replaceAll("/", ".")}`,
          area: "route" as const,
          source: "app",
          path: route.path,
          render: route.render,
        })),
        ...contribution.nav.map((item) => ({
          id: `${contribution.id}.nav.${item.id}`,
          area: "sidebar-nav" as const,
          source: "app",
          label: item.label,
          path: item.path,
          navId: item.id,
        })),
      ]),
  };
}
