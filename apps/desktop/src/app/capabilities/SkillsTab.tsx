// The Skills tab (P24-26, W52-k): the profile's skills as switch rows (usage count and "learned"
// origin joined from the learning graph), the chosen skill's detail, and the Skills Hub section
// under both (our own hub search; installs are confirmed first, deep links too).

import { Search } from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { type RestLike, rest } from "../../settings/settingsApi.ts";
import { Button, EmptyState, MenuRow, SmallDialog } from "../../ui/components.tsx";
import { DetailHead, MetaGrid, PanelPill, QuietSearch, TextAction } from "../kit.tsx";
import {
  BottomSection,
  CapRow,
  DetailColumn,
  ListStrip,
  MasterDetail,
  RowSwitch,
} from "../masterDetail.tsx";
import { notify } from "../Toasts.tsx";
import type { HubResult, SkillRow } from "./shared.ts";

export interface SkillUsage {
  readonly useCount: number;
  readonly createdBy: "agent" | "user" | null;
}

export interface ListHandle<T> {
  rows: T[] | null;
  reload: () => void;
  setRows: (update: (rows: T[]) => T[]) => void;
}

/** "productivity-tools" → "Productivity Tools"; empty → the fallback. */
export function titleCase(value: string, fallback: string): string {
  const words = value.trim().replace(/[-_]+/g, " ").split(/\s+/).filter(Boolean);
  if (words.length === 0) return fallback;
  return words.map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(" ");
}

/** Rows matching `query` (name, description, category), sorted by use (ties by name). */
export function visibleSkills(
  rows: readonly SkillRow[],
  usage: ReadonlyMap<string, SkillUsage>,
  query: string,
  mostUsedFirst: boolean,
): SkillRow[] {
  const needle = query.trim().toLowerCase();
  const uses = (row: SkillRow) => usage.get(row.name)?.useCount ?? 0;
  return rows
    .filter(
      (row) =>
        needle === "" ||
        `${row.name} ${row.description} ${row.category}`.toLowerCase().includes(needle),
    )
    .sort((a, b) => {
      const diff = mostUsedFirst ? uses(b) - uses(a) : uses(a) - uses(b);
      return diff !== 0 ? diff : a.name.localeCompare(b.name);
    });
}

/** Set every row's switch, one call at a time; answers how many changed. */
export async function setMany(
  names: readonly string[],
  call: (name: string) => Promise<boolean>,
): Promise<number> {
  let done = 0;
  for (const name of names) if (await call(name)) done += 1;
  return done;
}

export function SkillsTab(props: {
  api: RestLike | null;
  profile: string;
  list: ListHandle<SkillRow>;
  query: string;
  /** A hub identifier from an install deep link: confirmed before anything is called. */
  pendingInstall?: string | null;
  onPendingDone?: () => void;
}) {
  const t = useT();
  const { api, profile, list } = props;
  const [usage, setUsage] = useState<Map<string, SkillUsage>>(new Map());
  const [mostUsed, setMostUsed] = useState(true);
  const [chosen, setChosen] = useState<string | null>(null);
  const [archiving, setArchiving] = useState<string | null>(null);

  // usage and origin from the learning graph (absent on older daemons: everything counts 0)
  useEffect(() => {
    let live = true;
    void rest<{ nodes?: Array<{ id: string } & Partial<SkillUsage>> }>(
      api,
      "GET",
      "/api/learning/graph",
      { profile },
    ).then((answer) => {
      if (!live || !answer.ok) return;
      const next = new Map<string, SkillUsage>();
      for (const node of answer.data.nodes ?? []) {
        if (typeof node.id !== "string" || !node.id.startsWith("skill:")) continue;
        next.set(node.id.slice("skill:".length), {
          useCount: typeof node.useCount === "number" ? node.useCount : 0,
          createdBy:
            node.createdBy === "agent" || node.createdBy === "user" ? node.createdBy : null,
        });
      }
      setUsage(next);
    });
    return () => {
      live = false;
    };
  }, [api, profile]);

  const rows = useMemo(
    () => visibleSkills(list.rows ?? [], usage, props.query, mostUsed),
    [list.rows, usage, props.query, mostUsed],
  );
  const selected = rows.find((row) => row.name === chosen) ?? rows[0] ?? null;

  const write = async (name: string, enabled: boolean): Promise<boolean> => {
    list.setRows((all) => all.map((row) => (row.name === name ? { ...row, enabled } : row)));
    const answer = await rest(api, "POST", "/api/skills/toggle", {
      body: { name, enabled },
      profile,
    });
    if (!answer.ok) {
      list.setRows((all) =>
        all.map((row) => (row.name === name ? { ...row, enabled: !enabled } : row)),
      );
      notify({
        tone: "error",
        title: t("capabilities.toggleFailed", { name }),
        message: answer.message,
      });
    }
    return answer.ok;
  };

  const bulk = async (names: string[], enabled: boolean) => {
    const count = await setMany(names, (name) => write(name, enabled));
    notify({ tone: "success", title: t("capabilities.updated", { count }) });
    list.reload();
  };

  const all = list.rows ?? [];
  const allOn = all.length > 0 && all.every((row) => row.enabled);
  const uses = (name: string) => usage.get(name)?.useCount ?? 0;

  const archive = async () => {
    if (archiving === null) return;
    const name = archiving;
    setArchiving(null);
    const answer = await rest(
      api,
      "DELETE",
      `/api/learning/nodes/${encodeURIComponent(`skill:${name}`)}`,
      { profile },
    );
    notify(
      answer.ok
        ? { tone: "success", title: t("skills.archived", { name }) }
        : { tone: "error", title: t("skills.archive"), message: answer.message },
    );
    list.reload();
  };

  const uninstall = async (name: string) => {
    const answer = await rest(api, "POST", "/api/skills/hub/uninstall", {
      body: { name },
      profile,
    });
    if (!answer.ok)
      notify({ tone: "error", title: t("skills.uninstall"), message: answer.message });
    list.reload();
  };

  const listColumn = (
    <>
      <ListStrip
        sortLabel={t(mostUsed ? "skills.mostUsed" : "skills.leastUsed")}
        descending={mostUsed}
        onSort={() => setMostUsed(!mostUsed)}
        menuLabel={t("capabilities.listMenu")}
        menu={(close) => (
          <>
            <MenuRow
              kind="switch"
              label={t("capabilities.all")}
              checked={allOn}
              onSelect={() => {
                close();
                void bulk(
                  all.filter((row) => row.enabled === allOn).map((row) => row.name),
                  !allOn,
                );
              }}
            />
            <MenuRow
              label={t("skills.disableUnused")}
              onSelect={() => {
                close();
                void bulk(
                  all.filter((row) => row.enabled && uses(row.name) === 0).map((row) => row.name),
                  false,
                );
              }}
            />
          </>
        )}
      />
      <ul>
        {rows.map((skill) => {
          const used = uses(skill.name);
          const learned = usage.get(skill.name)?.createdBy === "agent";
          return (
            <CapRow
              key={skill.name}
              title={skill.name}
              subtitle={titleCase(skill.category, t("skills.general"))}
              badge={
                learned ? (
                  <span className="md-row-badge" data-tone="good">
                    {t("skills.badge.learned")}
                  </span>
                ) : skill.source === "hub" ? (
                  <span className="md-row-badge">{t("skills.badge.hub")}</span>
                ) : undefined
              }
              {...(used > 0 ? { meta: t("skills.uses", { count: used }) } : {})}
              selected={selected?.name === skill.name}
              dim={!skill.enabled}
              onSelect={() => setChosen(skill.name)}
              control={
                <RowSwitch
                  label={t("skills.enabled", { name: skill.name })}
                  checked={skill.enabled}
                  onChange={(on) => void write(skill.name, on)}
                />
              }
            />
          );
        })}
      </ul>
    </>
  );

  const detail =
    selected === null ? null : (
      <DetailColumn footer={<p className="md-caption-end">{t("capabilities.changesApply")}</p>}>
        <DetailHead
          level={2}
          title={selected.name}
          pills={
            <>
              <PanelPill tone="category">
                {titleCase(selected.category, t("skills.general"))}
              </PanelPill>
              {usage.get(selected.name)?.createdBy === "agent" ? (
                <PanelPill tone="good">{t("skills.badge.learned")}</PanelPill>
              ) : selected.source === "hub" ? (
                <PanelPill>{t("skills.badge.hub")}</PanelPill>
              ) : null}
            </>
          }
          description={selected.description}
          actions={
            <>
              {selected.source === "hub" && (
                <TextAction onClick={() => void uninstall(selected.name)}>
                  {t("skills.uninstall")}
                </TextAction>
              )}
              {usage.get(selected.name)?.createdBy === "agent" && (
                <TextAction tone="danger" onClick={() => setArchiving(selected.name)}>
                  {t("skills.archive")}
                </TextAction>
              )}
            </>
          }
        />
        <MetaGrid
          framed
          rows={[
            [t("skills.meta.name"), selected.name],
            [t("skills.meta.category"), titleCase(selected.category, t("skills.general"))],
            [t("skills.meta.source"), selected.source],
            ...(usage.get(selected.name)?.createdBy
              ? ([
                  [
                    t("skills.meta.createdBy"),
                    t(
                      usage.get(selected.name)?.createdBy === "agent"
                        ? "skills.createdBy.agent"
                        : "skills.createdBy.user",
                    ),
                  ],
                ] as const)
              : []),
            [t("skills.meta.uses"), String(uses(selected.name))],
          ]}
        />
      </DetailColumn>
    );

  return (
    <div className="md-tab-body" data-testid="tab-skills">
      {list.rows !== null && rows.length === 0 ? (
        <EmptyState
          look="panel"
          fill
          icon={<Search aria-hidden="true" />}
          title={t("skills.none")}
          description={
            props.query.trim() === ""
              ? t("capabilities.noneYet")
              : t("capabilities.nothingMatches", { query: props.query.trim() })
          }
        />
      ) : (
        <MasterDetail
          id="capabilities.skills"
          list={listColumn}
          detail={detail}
          listLabel={t("capabilities.tab.skills")}
        />
      )}
      <HubSection
        api={api}
        profile={profile}
        installed={() => list.reload()}
        {...(props.pendingInstall === undefined ? {} : { pendingInstall: props.pendingInstall })}
        {...(props.onPendingDone === undefined ? {} : { onPendingDone: props.onPendingDone })}
      />
      {archiving !== null && (
        <SmallDialog
          role="alertdialog"
          title={t("skills.archive.title")}
          description={t("skills.archive.body", { name: archiving })}
          onClose={() => setArchiving(null)}
          actions={
            <>
              <Button variant="ghost" size="sm" onClick={() => setArchiving(null)}>
                {t("capabilities.cancel")}
              </Button>
              <Button variant="danger" size="sm" onClick={() => void archive()}>
                {t("skills.archive")}
              </Button>
            </>
          }
        />
      )}
    </div>
  );
}

/** The Skills Hub section: our hub search inside the framed box, "Update installed". */
export function HubSection(props: {
  api: RestLike | null;
  profile: string;
  installed: () => void;
  pendingInstall?: string | null;
  onPendingDone?: () => void;
}) {
  const t = useT();
  const { api, profile } = props;
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<HubResult[] | null>(null);
  const [confirming, setConfirming] = useState<string | null>(props.pendingInstall ?? null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ text: string; bad: boolean } | null>(null);

  useEffect(() => {
    if (props.pendingInstall) setConfirming(props.pendingInstall);
  }, [props.pendingInstall]);

  const search = async (event: FormEvent) => {
    event.preventDefault();
    if (query.trim() === "") return;
    const answer = await rest<{ results?: HubResult[] }>(
      api,
      "GET",
      `/api/skills/hub/search?q=${encodeURIComponent(query.trim())}`,
      { profile },
    );
    if (answer.ok) setResults(answer.data.results ?? []);
    else setStatus({ text: answer.message, bad: true });
  };

  const install = async () => {
    if (confirming === null || busy) return;
    setBusy(true);
    const identifier = confirming;
    const answer = await rest<{ name?: string }>(api, "POST", "/api/skills/hub/install", {
      body: { identifier },
      profile,
      timeoutMs: 120_000,
    });
    setBusy(false);
    setConfirming(null);
    props.onPendingDone?.();
    const text = answer.ok
      ? t("skills.done", { name: answer.data.name ?? identifier })
      : answer.message;
    setStatus({ text, bad: !answer.ok });
    notify(
      answer.ok
        ? { tone: "success", title: text }
        : { tone: "error", title: t("skills.install"), message: text },
    );
    if (answer.ok) {
      setResults((current) =>
        current === null
          ? current
          : current.map((hit) =>
              hit.identifier === identifier ? { ...hit, installed: true } : hit,
            ),
      );
    }
    props.installed();
  };

  const update = async () => {
    const answer = await rest<{ message?: string }>(api, "POST", "/api/skills/hub/update", {
      body: {},
      profile,
      timeoutMs: 300_000,
    });
    notify(
      answer.ok
        ? { tone: "success", title: answer.data.message ?? t("skills.hub.updated") }
        : { tone: "error", title: t("skills.hub.update"), message: answer.message },
    );
    if (answer.ok) props.installed();
  };

  return (
    <>
      <BottomSection
        id="capabilities.skillsHub"
        label={t("skills.hub")}
        hideLabel={t("skills.hub.hide")}
        showLabel={t("skills.hub.show")}
        hint={t("skills.hub.hint")}
        actions={<TextAction onClick={() => void update()}>{t("skills.hub.update")}</TextAction>}
      >
        <form className="cap-hub-search" onSubmit={search}>
          <QuietSearch fill label={t("skills.search")} value={query} onChange={setQuery} />
          <Button type="submit" variant="outline" size="sm" disabled={query.trim() === ""}>
            {t("skills.searchButton")}
          </Button>
        </form>
        {status?.bad === true && (
          <p className="md-status cap-hub-status" data-tone="bad">
            {status.text}
          </p>
        )}
        {results === null ? (
          <p className="cap-hub-empty">{t("skills.hub.empty")}</p>
        ) : (
          <ul className="cap-hub-results">
            {results.map((hit) => (
              <li key={hit.identifier} className="cap-hub-row">
                <span className="cap-hub-text">
                  <span className="cap-hub-name">{hit.name}</span>
                  <span className="cap-hub-desc">{hit.description}</span>
                </span>
                <span className="cap-hub-source">{hit.source}</span>
                {hit.installed ? (
                  <span className="cap-hub-installed">{t("skills.installed")}</span>
                ) : (
                  <Button variant="outline" size="sm" onClick={() => setConfirming(hit.identifier)}>
                    {t("skills.install")}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </BottomSection>
      <p className="sr-only" role="status">
        {status?.text ?? ""}
      </p>
      {confirming !== null && (
        <SmallDialog
          title={t("skills.confirm.title")}
          description={t("skills.confirm.body", { identifier: confirming, profile })}
          onClose={() => {
            setConfirming(null);
            props.onPendingDone?.();
          }}
          actions={
            <>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setConfirming(null);
                  props.onPendingDone?.();
                }}
              >
                {t("capabilities.cancel")}
              </Button>
              <Button variant="primary" size="sm" disabled={busy} onClick={() => void install()}>
                {t("skills.confirm.install")}
              </Button>
            </>
          }
        />
      )}
    </>
  );
}
