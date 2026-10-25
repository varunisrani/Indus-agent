// Session import (P24-28; W52-l look): a full-window overlay that lists the Claude Code and Codex
// conversations found on the server's machine, previews one, and copies it into a new Indus
// session. The source files are only read. Data the backend does not have (a per-row message
// count before the preview, "already imported", paging, the server's host name) is left out.

import { ArrowRight, CircleUser, MessagesSquare, Monitor, RefreshCw } from "lucide-react";
import { useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { type RestLike, rest } from "../../settings/settingsApi.ts";
import { Button, EmptyState, IconButton } from "../../ui/components.tsx";
import { InlineError, QuietSearch } from "../kit.tsx";
import { Loader } from "../Loader.tsx";
import { useApi, useRestValue } from "../manage/common.tsx";
import { OverlayCard, Segmented } from "../overlay/OverlayFrame.tsx";
import "./sessionImport.css";

export interface Candidate {
  source: string;
  path: string;
  title: string;
  cwd: string | null;
  modified: number;
}

interface Preview {
  title: string;
  messages: Array<{ role: string; content: string; tool?: string }>;
  skipped: number;
}

type SourceFilter = "all" | "claude-code" | "codex";
const PREVIEW_LIMIT = 60;

function sourceLabel(t: ReturnType<typeof useT>, source: string): string {
  return source === "codex"
    ? t("manage.import.codex")
    : source === "claude-code"
      ? t("manage.import.claude")
      : source;
}

function dayOf(modified: number): string {
  const ms = modified > 1e12 ? modified : modified * 1000;
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(ms);
}

export function SessionImport(props: {
  api?: RestLike | null;
  profile: string | null;
  onClose?: () => void;
  /** Open the new session (and close the overlay). */
  onImported?: (sessionId: string) => void;
  /** The server's host name for a remote connection; absent = this computer. */
  host?: string | null;
}) {
  const t = useT();
  const api = useApi(props.api);
  const [source, setSource] = useState<SourceFilter>("all");
  const [query, setQuery] = useState("");
  const candidates = useRestValue<{ candidates: Candidate[] }>(
    () =>
      rest(
        api,
        "GET",
        `/api/session-import/candidates${source === "all" ? "" : `?source=${source}`}`,
        { profile: props.profile },
      ),
    [api, props.profile, source],
  );
  const [picked, setPicked] = useState<Candidate | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [importing, setImporting] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  const show = async (row: Candidate) => {
    setPicked(row);
    setPreview(null);
    setPreviewError("");
    setError("");
    setNote("");
    const answer = await rest<Preview>(api, "POST", "/api/session-import/preview", {
      profile: props.profile,
      body: { source: row.source, path: row.path },
    });
    if (answer.ok) setPreview(answer.data);
    else setPreviewError(answer.message);
  };
  const doImport = async () => {
    if (picked === null) return;
    setImporting(true);
    setError("");
    const answer = await rest<{ session_id: string; message_count: number }>(
      api,
      "POST",
      "/api/session-import/import",
      { profile: props.profile, body: { source: picked.source, path: picked.path } },
    );
    setImporting(false);
    if (!answer.ok) {
      setError(answer.message);
      return;
    }
    setNote(t("manage.import.done", { count: answer.data.message_count }));
    props.onImported?.(answer.data.session_id);
  };
  const refresh = () => {
    candidates.reload();
    if (picked !== null) void show(picked);
  };

  const rows = candidates.state === "ok" ? candidates.data.candidates : [];
  const needle = query.trim().toLowerCase();
  const shown =
    needle === ""
      ? rows
      : rows.filter(
          (r) =>
            r.title.toLowerCase().includes(needle) || (r.cwd ?? "").toLowerCase().includes(needle),
        );
  const groups: Array<{ day: string; rows: Candidate[] }> = [];
  for (const row of [...shown].sort((a, b) => b.modified - a.modified)) {
    const day = dayOf(row.modified);
    const last = groups[groups.length - 1];
    if (last !== undefined && last.day === day) last.rows.push(row);
    else groups.push({ day, rows: [row] });
  }

  return (
    <OverlayCard
      id="session-import"
      title={t("imp.title")}
      closeLabel={t("common.close")}
      onClose={props.onClose ?? (() => {})}
      surface="elevated"
      className="imp-card"
      titlebarActions={
        <IconButton
          label={t("imp.refresh")}
          icon={<RefreshCw aria-hidden="true" size={14} />}
          onClick={refresh}
        />
      }
    >
      <div className="imp-frame" data-testid="session-import">
        <header className="imp-head">
          <h1 className="imp-title">{t("imp.title")}</h1>
          <p className="imp-subtitle">{t("imp.subtitle")}</p>
          <p className="imp-meta">
            <span>
              <Monitor aria-hidden="true" size={14} />
              {props.host ? t("imp.readingFrom", { host: props.host }) : t("imp.readingLocal")}
            </span>
            <span>
              <CircleUser aria-hidden="true" size={14} />
              {t("imp.into", { profile: props.profile ?? t("manage.profiles.default") })}
            </span>
          </p>
        </header>
        <div className="imp-body">
          <aside className="imp-list-col">
            <div className="imp-list-tools">
              <Segmented
                label={t("imp.source")}
                value={source}
                options={[
                  { value: "all", label: t("manage.import.all") },
                  { value: "claude-code", label: t("manage.import.claude") },
                  { value: "codex", label: t("manage.import.codex") },
                ]}
                onChange={(next) => {
                  setSource(next);
                  setPicked(null);
                  setPreview(null);
                }}
              />
              {rows.length > 0 && (
                <QuietSearch
                  label={t("imp.search")}
                  placeholder={t("imp.searchPlaceholder")}
                  value={query}
                  onChange={setQuery}
                />
              )}
            </div>
            <div className="imp-list">
              {candidates.state === "loading" ? (
                <div className="imp-center">
                  <Loader />
                  <span className="imp-caption">{t("imp.finding")}</span>
                </div>
              ) : candidates.state === "error" ? (
                <div className="imp-center">
                  <InlineError>
                    {candidates.code === "not-connected"
                      ? t("manage.notConnected")
                      : candidates.message}
                  </InlineError>
                  <Button variant="secondary" size="sm" onClick={candidates.reload}>
                    {t("manage.retry")}
                  </Button>
                </div>
              ) : shown.length === 0 ? (
                <div className="imp-center">
                  <p className="imp-empty-title">{t("imp.none")}</p>
                  <p className="imp-caption">{t("manage.import.none")}</p>
                </div>
              ) : (
                groups.map((group) => (
                  <section key={group.day} className="imp-group">
                    <h3 className="imp-day">{group.day}</h3>
                    <ul>
                      {group.rows.map((row) => (
                        <li key={row.path}>
                          <button
                            type="button"
                            className="imp-row"
                            aria-current={picked?.path === row.path ? "true" : undefined}
                            onClick={() => void show(row)}
                          >
                            <span className="imp-row-source">
                              <MessagesSquare aria-hidden="true" size={12} />
                              {sourceLabel(t, row.source)}
                            </span>
                            <span className="imp-row-title">{row.title}</span>
                            {row.cwd !== null && row.cwd !== "" && (
                              <span className="imp-row-cwd">{row.cwd}</span>
                            )}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </section>
                ))
              )}
            </div>
          </aside>
          <section className="imp-detail">
            {picked === null ? (
              <div className="imp-center imp-fill">
                <EmptyState
                  look="panel"
                  icon={<MessagesSquare aria-hidden="true" />}
                  title={t("imp.pickTitle")}
                  description={t("imp.pickBody")}
                />
              </div>
            ) : (
              <>
                <header className="imp-detail-head">
                  <h2>{preview?.title ?? picked.title}</h2>
                  <p className="imp-caption">
                    {preview === null
                      ? sourceLabel(t, picked.source)
                      : t("imp.caption", {
                          source: sourceLabel(t, picked.source),
                          count: preview.messages.length,
                        })}
                  </p>
                </header>
                <div className="imp-messages" data-testid="import-preview">
                  {previewError !== "" ? (
                    <InlineError>{previewError}</InlineError>
                  ) : preview === null ? (
                    <Loader />
                  ) : (
                    <>
                      {preview.messages.slice(0, PREVIEW_LIMIT).map((m, i) => (
                        // biome-ignore lint/suspicious/noArrayIndexKey: a fixed preview list
                        <article key={i} className="imp-message">
                          <p className="imp-speaker">
                            {m.role === "user" ? t("imp.you") : sourceLabel(t, picked.source)}
                            {m.tool !== undefined && (
                              <span className="imp-tool">{` · ${m.tool}`}</span>
                            )}
                          </p>
                          <div className="imp-text">{m.content}</div>
                        </article>
                      ))}
                      {preview.messages.length > PREVIEW_LIMIT && (
                        <p className="imp-caption">
                          {t("imp.shortened", { count: PREVIEW_LIMIT })}
                        </p>
                      )}
                    </>
                  )}
                </div>
                <footer className="imp-footer">
                  <p className="imp-caption">{t("imp.notice")}</p>
                  <div className="imp-footer-action">
                    <Button
                      variant="primary"
                      size="sm"
                      disabled={importing || preview === null}
                      onClick={() => void doImport()}
                    >
                      {importing ? t("imp.importing") : t("imp.continue")}
                      <ArrowRight aria-hidden="true" size={14} />
                    </Button>
                  </div>
                </footer>
                {error !== "" && <InlineError>{error}</InlineError>}
                {note !== "" && (
                  <p className="imp-caption" role="status">
                    {note}
                  </p>
                )}
              </>
            )}
          </section>
        </div>
      </div>
    </OverlayCard>
  );
}
