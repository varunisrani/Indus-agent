// The Artifacts page (P24-28): images, files and links the agent produced in the recent sessions
// (`artifacts.ts` finds them), filtered by kind and searched by name or session; each one jumps
// back to the session that made it. A file is downloaded through main (`artifacts.download`):
// main mints the download ticket on the current connection with the artifact's originating profile
// and session and saves the bytes into Downloads — on a remote connection that is the only way to
// fetch it, and the renderer never sees the ticket. W52-l look: a page with centred text tabs and
// counts, a refresh icon, an image card grid and a hairline table for files and links.

import { FileText, ImageIcon, Link2, RefreshCw } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router";
import type { IndusDesktopBridge } from "../../global.d.ts";
import { useT } from "../../i18n/index.tsx";
import { type RestAnswer, type RestLike, rest } from "../../settings/settingsApi.ts";
import { Button, EmptyState, IconButton } from "../../ui/components.tsx";
import { desktopBridge } from "../bridge.ts";
import { QuietSearch, TextTabs } from "../kit.tsx";
import { Loader } from "../Loader.tsx";
import { useApi, useRestValue, when } from "../manage/common.tsx";
import "./artifacts.css";
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

  const all = loaded.state === "ok" ? loaded.data.artifacts : [];
  const counts = {
    all: all.length,
    image: all.filter((a) => a.kind === "image").length,
    file: all.filter((a) => a.kind === "file").length,
    link: all.filter((a) => a.kind === "link").length,
  };
  const q = query.trim().toLowerCase();
  const shown = all.filter(
    (a) =>
      (kind === "all" || a.kind === kind) &&
      (q === "" || a.value.toLowerCase().includes(q) || a.sessionTitle.toLowerCase().includes(q)),
  );
  const images = shown.filter((a) => a.kind === "image");
  const others = shown.filter((a) => a.kind !== "image");
  const nameOf = (a: Artifact) => a.value.split("/").pop() || a.value;
  const action = (artifact: Artifact) =>
    isRemoteValue(artifact.value) ? (
      <button
        type="button"
        className="art-link"
        onClick={() => void bridge?.external.open({ url: artifact.value })}
      >
        {t("manage.artifacts.openLink")}
      </button>
    ) : (
      <button type="button" className="art-link" onClick={() => void download(artifact)}>
        {t("manage.artifacts.download")}
      </button>
    );

  return (
    <section className="art-page" aria-labelledby="artifacts-title">
      <h1 id="artifacts-title" className="sr-only">
        {t("nav.artifacts")}
      </h1>
      <header className="art-head">
        <div className="art-head-left">
          {all.length > 0 && (
            <QuietSearch
              label={t("manage.artifacts.search")}
              placeholder={t("manage.artifacts.search")}
              value={query}
              onChange={setQuery}
            />
          )}
        </div>
        <TextTabs
          label={t("art.kinds")}
          value={kind}
          options={(["all", "image", "file", "link"] as const).map((k) => ({
            value: k,
            label: t(`manage.artifacts.kind.${k}`),
            count: counts[k],
          }))}
          onChange={setKind}
        />
        <div className="art-head-right">
          <IconButton
            label={t("art.refresh")}
            className={loaded.state === "loading" ? "art-spin" : undefined}
            icon={<RefreshCw aria-hidden="true" size={14} />}
            onClick={loaded.reload}
          />
        </div>
      </header>
      {note !== "" && (
        <p className="art-note" role="status">
          {note}
        </p>
      )}
      <div className="art-body">
        {loaded.state === "loading" ? (
          <Loader />
        ) : loaded.state === "error" ? (
          <EmptyState
            look="panel"
            title={t("art.errorTitle")}
            description={
              loaded.code === "not-connected" ? t("manage.notConnected") : loaded.message
            }
            action={
              <Button variant="secondary" size="sm" onClick={loaded.reload}>
                {t("manage.retry")}
              </Button>
            }
          />
        ) : (
          <>
            {loaded.data.skipped > 0 && (
              <p className="art-note">
                {t("manage.artifacts.skipped", { count: loaded.data.skipped })}
              </p>
            )}
            {shown.length === 0 ? (
              <div className="art-empty">
                <EmptyState look="panel" title={t("art.none")} description={t("art.noneBody")} />
              </div>
            ) : (
              <>
                {images.length > 0 && (
                  <>
                    <p className="art-pager">{t("art.images", { count: images.length })}</p>
                    <div className="art-grid">
                      {images.map((artifact) => (
                        <article className="art-card" key={artifact.key} data-kind="image">
                          <div className="art-thumb">
                            {isRemoteValue(artifact.value) ? (
                              <img
                                src={artifact.value}
                                alt=""
                                loading="lazy"
                                referrerPolicy="no-referrer"
                              />
                            ) : (
                              <ImageIcon aria-hidden="true" size={20} />
                            )}
                          </div>
                          <div className="art-card-text">
                            <span className="art-kind">
                              <ImageIcon aria-hidden="true" size={10} />
                              {t("art.image")}
                            </span>
                            <span className="art-name">{nameOf(artifact)}</span>
                            <span className="art-path">{artifact.value}</span>
                            <span className="art-path">
                              {artifact.sessionTitle} · {when(artifact.timestamp)}
                            </span>
                            <span className="art-actions">
                              <button
                                type="button"
                                className="art-link"
                                onClick={() => open(artifact.sessionId)}
                              >
                                {t("manage.artifacts.openChat")}
                              </button>
                              {action(artifact)}
                            </span>
                          </div>
                        </article>
                      ))}
                    </div>
                  </>
                )}
                {others.length > 0 && (
                  <>
                    <p className="art-pager">{t("art.items", { count: others.length })}</p>
                    <table className="art-table">
                      <thead>
                        <tr>
                          <th>{t("art.name")}</th>
                          <th>{t("art.location")}</th>
                          <th>{t("art.session")}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {others.map((artifact) => (
                          <tr key={artifact.key} data-kind={artifact.kind}>
                            <td>
                              <span className="art-file">
                                <span className="art-tile" aria-hidden="true">
                                  {artifact.kind === "link" ? (
                                    <Link2 size={12} />
                                  ) : (
                                    <FileText size={12} />
                                  )}
                                </span>
                                <span className="art-name">{nameOf(artifact)}</span>
                              </span>
                            </td>
                            <td>
                              <span className="art-location">
                                <span className="art-path">{artifact.value}</span>
                                {action(artifact)}
                              </span>
                            </td>
                            <td>
                              <button
                                type="button"
                                className="art-session"
                                onClick={() => open(artifact.sessionId)}
                              >
                                <span>{artifact.sessionTitle}</span>
                                <span className="art-time">{when(artifact.timestamp)}</span>
                              </button>
                              <button
                                type="button"
                                className="art-link"
                                onClick={() => open(artifact.sessionId)}
                              >
                                {t("manage.artifacts.openChat")}
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </>
                )}
              </>
            )}
          </>
        )}
      </div>
    </section>
  );
}
