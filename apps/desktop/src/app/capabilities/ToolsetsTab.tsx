// The Tools tab (P24-26, W52-k): the plain toolsets as rows with their tool counts, the chosen
// one's tools as chips, and for the terminal toolset the execution backend cards. A toolset's
// switch is read-only here (W52-k-1: there is no toolset write route; the selection lives in
// Settings); the backend card writes `terminal.backend` through `PUT /api/config`.

import { RefreshCw, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { type MessageKey, type Translate, useT } from "../../i18n/index.tsx";
import { patchFor, type RestLike, rest, valueAt } from "../../settings/settingsApi.ts";
import { EmptyState, IconButton } from "../../ui/components.tsx";
import { DetailHead, PanelPill } from "../kit.tsx";
import {
  CapRow,
  DetailColumn,
  ListStrip,
  MasterDetail,
  RowSwitch,
  ToolChip,
} from "../masterDetail.tsx";
import { notify } from "../Toasts.tsx";
import type { ListHandle } from "./SkillsTab.tsx";
import type { ToolsetRow } from "./shared.ts";

/** Our execution backends (the config schema's `terminal.backend` values). */
export const BACKENDS = [
  "local",
  "docker",
  "ssh",
  "singularity",
  "modal",
  "daytona",
  "vercel_sandbox",
] as const;

const BACKEND_NAMES: Readonly<Record<(typeof BACKENDS)[number], string>> = {
  local: "Local",
  docker: "Docker",
  ssh: "SSH",
  singularity: "Singularity",
  modal: "Modal",
  daytona: "Daytona",
  vercel_sandbox: "Vercel Sandbox",
};

/** "1 tool" / "N tools". */
export function toolCount(t: Translate, count: number): string {
  return t(count === 1 ? "toolsets.toolsOne" : "toolsets.tools", { count });
}

export function visibleToolsets(
  rows: readonly ToolsetRow[],
  query: string,
  ascending: boolean,
): ToolsetRow[] {
  const needle = query.trim().toLowerCase();
  return rows
    .filter(
      (row) =>
        needle === "" ||
        `${row.label} ${row.name} ${row.description} ${row.tools.join(" ")}`
          .toLowerCase()
          .includes(needle),
    )
    .sort((a, b) => (ascending ? 1 : -1) * a.label.localeCompare(b.label));
}

export function ToolsetsTab(props: {
  api: RestLike | null;
  profile: string;
  list: ListHandle<ToolsetRow>;
  query: string;
}) {
  const t = useT();
  const [ascending, setAscending] = useState(true);
  const [chosen, setChosen] = useState<string | null>(null);
  const rows = useMemo(
    () => visibleToolsets(props.list.rows ?? [], props.query, ascending),
    [props.list.rows, props.query, ascending],
  );
  const selected = rows.find((row) => row.name === chosen) ?? rows[0] ?? null;

  if (props.list.rows !== null && rows.length === 0) {
    return (
      <div className="md-tab-body" data-testid="tab-toolsets">
        <EmptyState
          look="panel"
          fill
          icon={<Search aria-hidden="true" />}
          title={t("toolsets.none")}
          description={
            props.query.trim() === ""
              ? t("capabilities.noneYet")
              : t("capabilities.nothingMatches", { query: props.query.trim() })
          }
        />
      </div>
    );
  }

  const list = (
    <>
      <ListStrip
        sortLabel={t(ascending ? "toolsets.az" : "toolsets.za")}
        descending={ascending}
        onSort={() => setAscending(!ascending)}
      />
      <ul>
        {rows.map((set) => (
          <CapRow
            key={set.name}
            title={set.label}
            subtitle={set.description !== "" ? set.description : set.tools.join(", ")}
            meta={toolCount(t, set.tools.length)}
            selected={selected?.name === set.name}
            dim={!set.enabled}
            onSelect={() => setChosen(set.name)}
            control={
              <RowSwitch
                label={t("skills.enabled", { name: set.label })}
                checked={set.enabled}
                disabled
                title={t("toolsets.changeInSettings")}
                onChange={() => undefined}
              />
            }
          />
        ))}
      </ul>
    </>
  );

  const detail =
    selected === null ? null : (
      <DetailColumn footer={<p className="md-caption-end">{t("capabilities.changesApply")}</p>}>
        <DetailHead level={2} title={selected.label} description={selected.description} />
        <ul className="md-tool-chips" aria-label={toolCount(t, selected.tools.length)}>
          {selected.tools.map((tool) => (
            <ToolChip key={tool}>{tool}</ToolChip>
          ))}
        </ul>
        {selected.name === "terminal" && <BackendPanel api={props.api} profile={props.profile} />}
      </DetailColumn>
    );

  return (
    <div className="md-tab-body" data-testid="tab-toolsets">
      <MasterDetail
        id="capabilities.tools"
        list={list}
        detail={detail}
        listLabel={t("capabilities.tab.toolsets")}
      />
    </div>
  );
}

/** "Execution backend": one card per backend; the configured one is selected and "In use". */
export function BackendPanel(props: { api: RestLike | null; profile: string }) {
  const t = useT();
  const [current, setCurrent] = useState<string | null>(null);
  const [serial, setSerial] = useState(0);
  const [busy, setBusy] = useState(false);

  // biome-ignore lint/correctness/useExhaustiveDependencies: serial asks for a reload
  useEffect(() => {
    let live = true;
    void rest<unknown>(props.api, "GET", "/api/config", { profile: props.profile }).then(
      (answer) => {
        if (!live || !answer.ok) return;
        const tree = (answer.data as { config?: unknown }).config ?? answer.data;
        const value = valueAt(tree, "terminal.backend");
        setCurrent(typeof value === "string" && value !== "" ? value : "local");
      },
    );
    return () => {
      live = false;
    };
  }, [props.api, props.profile, serial]);

  const choose = async (backend: string) => {
    if (busy || backend === current) return;
    const before = current;
    setBusy(true);
    setCurrent(backend);
    const answer = await rest(props.api, "PUT", "/api/config", {
      body: { config: patchFor("terminal.backend", backend) },
      profile: props.profile,
    });
    setBusy(false);
    if (!answer.ok) {
      setCurrent(before);
      notify({
        tone: "error",
        title: t("toolsets.backend.saveFailed", { message: answer.message }),
      });
    }
  };

  return (
    <section className="cap-backend" aria-label={t("toolsets.backend")}>
      <div className="cap-backend-head">
        <span>{t("toolsets.backend")}</span>
        <IconButton
          label={t("toolsets.backend.refresh")}
          icon={<RefreshCw aria-hidden="true" size={13} />}
          size="sm"
          onClick={() => setSerial((n) => n + 1)}
        />
      </div>
      {BACKENDS.map((backend) => {
        const inUse = backend === current;
        return (
          <button
            key={backend}
            type="button"
            className="cap-backend-card"
            aria-pressed={inUse}
            disabled={busy}
            onClick={() => void choose(backend)}
          >
            <span className="cap-backend-name">
              {BACKEND_NAMES[backend]}
              {inUse && <PanelPill tone="accent">{`✓ ${t("toolsets.backend.inUse")}`}</PanelPill>}
            </span>
            <span className="cap-backend-desc">
              {t(`toolsets.backend.${backend}` as MessageKey)}
            </span>
          </button>
        );
      })}
    </section>
  );
}
