// The Artifacts page (P24-28): images, files and links the agent produced in the recent sessions
// (`artifacts.ts` finds them), filtered by kind and searched by name or session; each one jumps
// back to the session that made it. A file is downloaded through main (`artifacts.download`):
// main mints the download ticket on the current connection with the artifact's originating profile
// and session and saves the bytes into Downloads — on a remote connection that is the only way to
// fetch it, and the renderer never sees the ticket.

import { useState } from "react";
import { useNavigate } from "react-router";
import type { IndusDesktopBridge } from "../../global.d.ts";
import { useT } from "../../i18n/index.tsx";
import { type RestAnswer, type RestLike, rest } from "../../settings/settingsApi.ts";
import { desktopBridge } from "../bridge.ts";
import { LoadState, PageFrame, useApi, useRestValue, when } from "../manage/common.tsx";
import { sessionPath } from "../routes.ts";
import {
  type Artifact,
  type ArtifactKind,
  extractArtifacts,
  isRemoteValue,
  type MessageLike,
} from "./artifacts.ts";

export const RECENT_SESSIONS = 30;
export const MESSAGES_PER_SESSION = 200;

interface SessionRow {
  id: string;
  title: string | null;
  preview?: string | null;
}

/** Index the recent sessions of `profile`; sessions whose messages fail are counted. */
export async function loadArtifacts(
  api: RestLike | null,
  profile: string | null,
): Promise<RestAnswer<{ artifacts: Artifact[]; skipped: number }>> {
  const list = await rest<{ sessions: SessionRow[] }>(
    api,
    "GET",
    `/api/sessions?limit=${RECENT_SESSIONS}`,
    { profile },
  );
  if (!list.ok) return list;
  let skipped = 0;
  const all: Artifact[] = [];
  const pages = await Promise.all(
    list.data.sessions.map((session) =>
      rest<{ messages: MessageLike[] }>(
        api,
        "GET",
        `/api/sessions/${encodeURIComponent(session.id)}/messages?limit=${MESSAGES_PER_SESSION}&order=latest`,
        { profile },
      ),
    ),
  );
  pages.forEach((page, i) => {
    const session = list.data.sessions[i] as SessionRow;
    if (!page.ok) {
      skipped += 1;
      return;
    }
    all.push(
      ...extractArtifacts(page.data.messages, {
        id: session.id,
        title: session.title ?? session.preview ?? session.id,
        profile,
      }),
    );
  });
  all.sort((a, b) => b.timestamp - a.timestamp);
  return { ok: true, status: 200, data: { artifacts: all, skipped } };
}

export type ArtifactsBridge = Pick<IndusDesktopBridge, "artifacts" | "external">;

export function ArtifactsPage(props: {
  api?: RestLike | null;
  profile: string | null;
  bridge?: ArtifactsBridge | null;
  /** Jump back to a session (default: the router). */
  onOpenSession?: (sessionId: string) => void;
}) {
  const t = useT();
  const api = useApi(props.api);
  const bridge = props.bridge === undefined ? desktopBridge() : props.bridge;
  const navigate = useNavigate();
  const open = props.onOpenSession ?? ((id: string) => navigate(sessionPath(id)));
  const [kind, setKind] = useState<"all" | ArtifactKind>("all");
  const [query, setQuery] = useState("");
  const [note, setNote] = useState("");
  const loaded = useRestValue(() => loadArtifacts(api, props.profile), [api, props.profile]);

  const download = async (artifact: Artifact) => {
    if (bridge === null) return;
    try {
      const done = await bridge.artifacts.download({
        path: artifact.value,
        profile: artifact.profile,
        sessionId: artifact.sessionId,
      });
      setNote(t("manage.artifacts.saved", { path: done.savedTo }));
    } catch (error) {
      setNote(error instanceof Error ? error.message : t("manage.failed"));
    }
  };

  return (
    <PageFrame title={t("nav.artifacts")} id="artifacts">
      <div className="manage-inline">
        {(["all", "image", "file", "link"] as const).map((k) => (
          <button
            key={k}
            type="button"
            className="button button-secondary"
            aria-pressed={kind === k}
            onClick={() => setKind(k)}
          >
            {t(`manage.artifacts.kind.${k}`)}
          </button>
        ))}
        <input
          aria-label={t("manage.artifacts.search")}
          placeholder={t("manage.artifacts.search")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {note !== "" && (
        <p className="manage-muted" role="status">
          {note}
        </p>
      )}
      <LoadState value={loaded}>
        {(data) => {
          const q = query.trim().toLowerCase();
          const shown = data.artifacts.filter(
            (a) =>
              (kind === "all" || a.kind === kind) &&
              (q === "" ||
                a.value.toLowerCase().includes(q) ||
                a.sessionTitle.toLowerCase().includes(q)),
          );
          return (
            <>
              {data.skipped > 0 && (
                <p className="manage-muted">
                  {t("manage.artifacts.skipped", { count: data.skipped })}
                </p>
              )}
              {shown.length === 0 && <p className="manage-muted">{t("manage.artifacts.none")}</p>}
              <div className="manage-gallery">
                {shown.map((artifact) => (
                  <div className="manage-card" key={artifact.key} data-kind={artifact.kind}>
                    {artifact.kind === "image" && isRemoteValue(artifact.value) && (
                      <img
                        className="manage-thumb"
                        src={artifact.value}
                        alt=""
                        loading="lazy"
                        referrerPolicy="no-referrer"
                      />
                    )}
                    <span className="manage-code">
                      {artifact.value.split("/").pop() || artifact.value}
                    </span>
                    <span className="manage-muted">
                      {artifact.sessionTitle} · {when(artifact.timestamp)}
                    </span>
                    <div className="manage-inline">
                      <button
                        type="button"
                        className="button button-secondary"
                        onClick={() => open(artifact.sessionId)}
                      >
                        {t("manage.artifacts.openChat")}
                      </button>
                      {isRemoteValue(artifact.value) ? (
                        <button
                          type="button"
                          className="button button-secondary"
                          onClick={() => void bridge?.external.open({ url: artifact.value })}
                        >
                          {t("manage.artifacts.openLink")}
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="button button-secondary"
                          onClick={() => void download(artifact)}
                        >
                          {t("manage.artifacts.download")}
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </>
          );
        }}
      </LoadState>
    </PageFrame>
  );
}
