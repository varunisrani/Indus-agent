// Profiles (P24-27): create, rename, delete, export and import an archive, and edit the persona
// file. Rename and delete go through the window's profile switcher (P20-26) exactly like
// Settings › Profiles: main leaves that profile and tears down its connection before the backend
// change, then every profile-keyed store moves or goes (R08.7). Delete asks the person to type the
// profile's name. Export writes a zip without history, runtime state or any key (the server strips
// them; the page says so); import takes an archive path on the server's machine. A profile with no
// persona file is told so, and "Create persona" writes one. W52-l look: an overlay panel (list +
// detail with the persona editor) and small dialogs for new, rename, delete and import.

import { FolderInput, FolderOutput, House, Save, Users } from "lucide-react";
import { type ReactNode, useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { profileColor } from "../../profiles/ProfileRail.tsx";
import { useSwitcher } from "../../profiles/switcherReact.tsx";
import { type RestLike, rest } from "../../settings/settingsApi.ts";
import { Button, SmallDialog } from "../../ui/components.tsx";
import {
  DetailHead,
  Field,
  InlineError,
  MetaGrid,
  PanelPill,
  PanelRow,
  TextAction,
} from "../kit.tsx";
import { Loader } from "../Loader.tsx";
import { useApi, useRestValue } from "../manage/common.tsx";
import { ListDetail, OverlayCard } from "../overlay/OverlayFrame.tsx";
import "./profiles.css";

interface ProfileRow {
  name: string;
  is_default: boolean;
  description?: string | null;
  model?: string | null;
  provider?: string | null;
  has_persona?: boolean;
  /** Skills the profile has turned on; null when its settings cannot be read (W55-F2-2). */
  skill_count?: number | null;
}

/** What rename/delete need from the switcher (the real one, or a test fake). */
export interface ProfileChanges {
  renameProfile(
    from: string,
    to: string,
    run: () => Promise<void>,
  ): Promise<{ ok: true } | { ok: false; error: string }>;
  deleteProfile(
    name: string,
    run: () => Promise<void>,
  ): Promise<{ ok: true } | { ok: false; error: string }>;
}

function PersonaEditor(props: { api: RestLike | null; name: string }) {
  const t = useT();
  const persona = useRestValue<{ content: string; exists: boolean }>(
    () => rest(props.api, "GET", `/api/profiles/${encodeURIComponent(props.name)}/persona`),
    [props.api, props.name],
  );
  const [draft, setDraft] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const save = async (content: string) => {
    const answer = await rest(
      props.api,
      "PUT",
      `/api/profiles/${encodeURIComponent(props.name)}/persona`,
      { body: { content } },
    );
    if (answer.ok) {
      setNote(t("manage.saved"));
      setError("");
      setDraft(null);
      persona.reload();
    } else setError(answer.message);
  };
  if (persona.state === "loading") return <Loader />;
  if (persona.state === "error") return <InlineError>{persona.message}</InlineError>;
  const data = persona.data;
  const dirty = draft !== null && draft !== data.content;
  return (
    <section className="prof-persona">
      <div className="prof-persona-head">
        <div>
          <h4 className="prof-persona-label">{t("prof.personaFile")}</h4>
          <p className="prof-persona-desc">
            {data.exists ? t("prof.personaBody") : t("manage.profiles.noPersona")}
          </p>
        </div>
        {dirty && <span className="prof-unsaved">{t("prof.unsaved")}</span>}
      </div>
      {!data.exists && draft === null ? (
        <div>
          <TextAction strong onClick={() => void save("")}>
            {t("manage.profiles.createPersona")}
          </TextAction>
        </div>
      ) : (
        <>
          <textarea
            className="prof-editor"
            aria-label={t("manage.profiles.persona")}
            spellCheck={false}
            value={draft ?? data.content}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="prof-save">
            {note !== "" && !dirty && <span className="prof-saved">{note}</span>}
            <Button
              variant="primary"
              size="sm"
              icon={<Save aria-hidden="true" size={13} />}
              disabled={!dirty}
              onClick={() => void save(draft ?? data.content)}
            >
              {t("prof.savePersona")}
            </Button>
          </div>
        </>
      )}
      {error !== "" && <InlineError>{error}</InlineError>}
    </section>
  );
}

type ProfileDialog =
  | { kind: "new" }
  | { kind: "rename"; name: string }
  | { kind: "delete"; name: string }
  | { kind: "import" };

export function ProfilesOverlay(props: {
  api?: RestLike | null;
  onClose: () => void;
  /** Defaults to the window's switcher. */
  changes?: ProfileChanges | null;
}) {
  const t = useT();
  const api = useApi(props.api);
  const switcher = useSwitcher();
  const changes: ProfileChanges | null = props.changes === undefined ? switcher : props.changes;
  const list = useRestValue<{ profiles: ProfileRow[]; current?: string | null }>(
    () => rest(api, "GET", "/api/profiles"),
    [api],
  );
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const [dialog, setDialog] = useState<ProfileDialog | null>(null);
  const [field, setField] = useState("");
  const [cloneFrom, setCloneFrom] = useState("");
  const [dialogError, setDialogError] = useState("");
  const [note, setNote] = useState("");

  const open = (next: ProfileDialog) => {
    setDialog(next);
    setField(next.kind === "rename" ? next.name : "");
    setCloneFrom("");
    setDialogError("");
  };
  const runChange = async (call: () => ReturnType<typeof rest>) => {
    const answer = await call();
    if (!answer.ok) throw new Error(answer.message);
  };

  const create = async () => {
    const body = { name: field.trim(), ...(cloneFrom === "" ? {} : { clone_from: cloneFrom }) };
    const answer = await rest(api, "POST", "/api/profiles", { body });
    if (!answer.ok) {
      setDialogError(answer.message || t("manage.failed"));
      return;
    }
    setDialog(null);
    setNote(t("manage.profiles.created", { name: body.name }));
    setPicked(body.name);
    list.reload();
  };

  const rename = async (from: string) => {
    const to = field.trim();
    if (to === "" || to === from || changes === null) return;
    const result = await changes.renameProfile(from, to, () =>
      runChange(() =>
        rest(api, "PATCH", `/api/profiles/${encodeURIComponent(from)}`, { body: { new_name: to } }),
      ),
    );
    if (!result.ok) {
      setDialogError(result.error || t("manage.failed"));
      return;
    }
    setDialog(null);
    setNote(t("manage.profiles.renamed", { name: to }));
    setPicked(to);
    list.reload();
  };

  const remove = async (name: string) => {
    if (field !== name || changes === null) return;
    const result = await changes.deleteProfile(name, () =>
      runChange(() => rest(api, "DELETE", `/api/profiles/${encodeURIComponent(name)}`)),
    );
    if (!result.ok) {
      setDialogError(result.error || t("manage.failed"));
      return;
    }
    setDialog(null);
    setNote(t("manage.profiles.deleted", { name }));
    setPicked(null);
    list.reload();
  };

  const exportProfile = async (name: string) => {
    const answer = await rest<{ archive: string }>(
      api,
      "POST",
      `/api/profiles/${encodeURIComponent(name)}/export`,
    );
    setNote(
      answer.ok
        ? t("manage.profiles.exported", { path: answer.data.archive })
        : answer.message || t("manage.failed"),
    );
  };

  const importArchive = async () => {
    const answer = await rest<{ name?: string }>(api, "POST", "/api/profiles/import", {
      body: { archive: field.trim() },
    });
    if (!answer.ok) {
      setDialogError(answer.message || t("manage.failed"));
      return;
    }
    setDialog(null);
    setNote(t("manage.profiles.imported"));
    list.reload();
  };

  let body: ReactNode;
  const rows = list.state === "ok" ? list.data.profiles : [];
  if (list.state === "loading") body = <Loader />;
  else if (list.state === "error") {
    body = (
      <div className="prof-pad">
        <InlineError>
          {list.code === "not-connected" ? t("manage.notConnected") : list.message}
        </InlineError>
        <Button variant="secondary" size="sm" onClick={list.reload}>
          {t("manage.retry")}
        </Button>
      </div>
    );
  } else {
    const needle = query.trim().toLowerCase();
    const shown = rows.filter((r) => needle === "" || r.name.toLowerCase().includes(needle));
    // W54-D2-7: nothing picked yet opens the profile this window runs on (else the default), so
    // its model and persona show at once instead of a "pick one" placeholder.
    const fallback =
      rows.find((r) => r.name === list.data.current) ?? rows.find((r) => r.is_default) ?? rows[0];
    const row = rows.find((r) => r.name === picked) ?? (picked === null ? fallback : null) ?? null;
    body = (
      <ListDetail
        title={t("nav.profiles")}
        subtitle={t(rows.length === 1 ? "prof.countOne" : "prof.count", { count: rows.length })}
        actions={
          <Button
            variant="ghost"
            size="sm"
            icon={<FolderInput aria-hidden="true" size={13} />}
            onClick={() => open({ kind: "import" })}
          >
            {t("manage.profiles.importButton")}
          </Button>
        }
        banner={
          note === "" ? undefined : (
            <p className="prof-note" role="status">
              {note}
            </p>
          )
        }
        whole={rows.length === 0}
        search={{
          label: t("prof.search"),
          placeholder: t("prof.searchPlaceholder"),
          value: query,
          onChange: setQuery,
        }}
        add={{ label: t("manage.profiles.new"), onAdd: () => open({ kind: "new" }) }}
        listLabel={t("nav.profiles")}
        list={shown.map((r) => (
          <PanelRow
            key={r.name}
            title={r.name}
            icon={
              r.is_default ? (
                <House aria-hidden="true" />
              ) : (
                <span
                  className="prof-glyph"
                  style={{ background: profileColor(r.name) }}
                  aria-hidden="true"
                >
                  {r.name.slice(0, 1).toUpperCase()}
                </span>
              )
            }
            selected={r.name === row?.name}
            onSelect={() => setPicked(r.name)}
            data={{ "data-profile": r.name }}
            menu={{
              label: t("prof.rowMenu", { name: r.name }),
              items: [
                ...(r.is_default
                  ? []
                  : [
                      {
                        id: "rename",
                        label: t("manage.profiles.rename"),
                        run: () => open({ kind: "rename", name: r.name }),
                      },
                    ]),
                {
                  id: "export",
                  label: t("manage.profiles.export"),
                  run: () => void exportProfile(r.name),
                },
                ...(r.is_default
                  ? []
                  : [
                      {
                        id: "delete",
                        label: t("manage.delete"),
                        tone: "danger" as const,
                        run: () => open({ kind: "delete", name: r.name }),
                      },
                    ]),
              ],
            }}
          />
        ))}
        {...(row === null
          ? {}
          : {
              detail: (
                <div className="prof-detail" data-detail={row.name}>
                  <DetailHead
                    title={row.name}
                    pills={
                      row.is_default ? (
                        <PanelPill tone="info">{t("prof.defaultPill")}</PanelPill>
                      ) : undefined
                    }
                    below={
                      row.description ? (
                        <p className="prof-description">{row.description}</p>
                      ) : undefined
                    }
                    actions={
                      <>
                        <Button
                          variant="ghost"
                          size="sm"
                          icon={<FolderOutput aria-hidden="true" size={13} />}
                          onClick={() => void exportProfile(row.name)}
                        >
                          {t("manage.profiles.export")}
                        </Button>
                        {!row.is_default && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => open({ kind: "delete", name: row.name })}
                          >
                            {t("manage.delete")}
                          </Button>
                        )}
                      </>
                    }
                  />
                  <MetaGrid
                    rows={[
                      [
                        t("prof.model"),
                        row.model ? (
                          <span key="model" className="prof-model">
                            {row.model}
                            {row.provider ? (
                              <span className="prof-provider"> · {row.provider}</span>
                            ) : null}
                          </span>
                        ) : (
                          "—"
                        ),
                      ],
                      [
                        t("prof.skills"),
                        typeof row.skill_count === "number" ? String(row.skill_count) : "—",
                      ],
                    ]}
                  />
                  <PersonaEditor api={api} name={row.name} />
                </div>
              ),
            })}
        empty={
          rows.length === 0
            ? {
                icon: <Users aria-hidden="true" />,
                title: t("prof.none"),
                action: (
                  <Button variant="primary" size="sm" onClick={() => open({ kind: "new" })}>
                    {t("manage.profiles.new")}
                  </Button>
                ),
              }
            : { icon: <Users aria-hidden="true" />, title: t("prof.pick") }
        }
      />
    );
  }

  const cancel = (
    <Button variant="outline" onClick={() => setDialog(null)}>
      {t("manage.cancel")}
    </Button>
  );
  return (
    <OverlayCard
      id="profiles"
      title={t("nav.profiles")}
      closeLabel={t("common.close")}
      onClose={props.onClose}
    >
      {body}
      {dialog?.kind === "new" && (
        <SmallDialog
          title={t("manage.profiles.new")}
          closeLabel={t("common.close")}
          onClose={() => setDialog(null)}
          actions={
            <>
              {cancel}
              <Button
                variant="primary"
                disabled={field.trim() === ""}
                onClick={() => void create()}
              >
                {t("manage.profiles.create")}
              </Button>
            </>
          }
        >
          <Field label={t("manage.profiles.name")} htmlFor="prof-new-name">
            <input id="prof-new-name" value={field} onChange={(e) => setField(e.target.value)} />
          </Field>
          <Field label={t("manage.profiles.cloneFrom")} htmlFor="prof-clone">
            <select
              id="prof-clone"
              value={cloneFrom}
              onChange={(e) => setCloneFrom(e.target.value)}
            >
              <option value="">{t("manage.profiles.fresh")}</option>
              {rows.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>
          {dialogError !== "" && <InlineError>{dialogError}</InlineError>}
        </SmallDialog>
      )}
      {dialog?.kind === "rename" && (
        <SmallDialog
          title={t("prof.renameTitle")}
          closeLabel={t("common.close")}
          onClose={() => setDialog(null)}
          actions={
            <>
              {cancel}
              <Button
                variant="primary"
                disabled={field.trim() === "" || field.trim() === dialog.name}
                onClick={() => void rename(dialog.name)}
              >
                {t("manage.profiles.rename")}
              </Button>
            </>
          }
        >
          <Field label={t("manage.profiles.renameTo", { name: dialog.name })} htmlFor="prof-rename">
            <input id="prof-rename" value={field} onChange={(e) => setField(e.target.value)} />
          </Field>
          {dialogError !== "" && <InlineError>{dialogError}</InlineError>}
        </SmallDialog>
      )}
      {dialog?.kind === "delete" && (
        <SmallDialog
          role="alertdialog"
          title={t("manage.profiles.confirmDelete", { name: dialog.name })}
          closeLabel={t("common.close")}
          onClose={() => setDialog(null)}
          actions={
            <>
              {cancel}
              <Button
                variant="danger"
                disabled={field !== dialog.name}
                onClick={() => void remove(dialog.name)}
              >
                {t("manage.profiles.deleteForGood")}
              </Button>
            </>
          }
        >
          <Field
            label={t("manage.profiles.typeToDelete", { name: dialog.name })}
            htmlFor="prof-delete"
          >
            <input id="prof-delete" value={field} onChange={(e) => setField(e.target.value)} />
          </Field>
          {dialogError !== "" && <InlineError>{dialogError}</InlineError>}
        </SmallDialog>
      )}
      {dialog?.kind === "import" && (
        <SmallDialog
          title={t("manage.profiles.import")}
          description={t("manage.profiles.exportNote")}
          closeLabel={t("common.close")}
          onClose={() => setDialog(null)}
          actions={
            <>
              {cancel}
              <Button
                variant="primary"
                disabled={field.trim() === ""}
                onClick={() => void importArchive()}
              >
                {t("manage.profiles.importButton")}
              </Button>
            </>
          }
        >
          <Field label={t("manage.profiles.archive")} htmlFor="prof-archive">
            <input id="prof-archive" value={field} onChange={(e) => setField(e.target.value)} />
          </Field>
          {dialogError !== "" && <InlineError>{dialogError}</InlineError>}
        </SmallDialog>
      )}
    </OverlayCard>
  );
}
