// The learning map (P24-28): skills and memories as a force-directed graph (`layout.ts`), with a
// playback slider over time, filters all / used / learned, node edit (a skill's on/off switch) and
// delete (a memory), and Share, which copies a code holding the layout only.
//
// Data: `GET /api/learning/graph` (`{nodes, edges}`; memories with timestamps and the skill they
// were learned with) when the server has it; otherwise the skills list alone (`GET /api/skills`,
// linked by category, no times — playback then has nothing to replay).

import { useMemo, useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { type RestAnswer, type RestLike, rest } from "../../settings/settingsApi.ts";
import { copyText } from "../clipboard.ts";
import { LoadState, PageFrame, useApi, useRestValue } from "../manage/common.tsx";
import {
  graphFromSkills,
  type LearningGraph,
  type LearningNode,
  layout,
  type MapFilter,
  readShareCode,
  ShareCodeError,
  shareCode,
  timeRange,
  visible,
} from "./layout.ts";

const BOX = { width: 900, height: 560 };

/** Stable keys for shared points (they carry no ids): kind and position, numbered on a clash. */
function keyed<T extends { kind: string; x: number; y: number }>(
  items: readonly T[],
): Array<{ key: string; node: T }> {
  const seen = new Map<string, number>();
  return items.map((node) => {
    const base = `${node.kind}:${node.x}:${node.y}`;
    const count = (seen.get(base) ?? 0) + 1;
    seen.set(base, count);
    return { key: `${base}:${count}`, node };
  });
}

/** The graph, from the learning read when the server has one, else from the skills list. */
export async function loadGraph(
  api: RestLike | null,
  profile: string | null,
): Promise<RestAnswer<{ graph: LearningGraph; full: boolean }>> {
  const full = await rest<LearningGraph>(api, "GET", "/api/learning/graph", { profile });
  if (full.ok && Array.isArray(full.data.nodes))
    return { ...full, data: { graph: full.data, full: true } };
  const skills = await rest<{
    skills: Array<{ name: string; category: string; enabled: boolean; source?: string }>;
  }>(api, "GET", "/api/skills", { profile });
  if (!skills.ok) return skills;
  return { ...skills, data: { graph: graphFromSkills(skills.data.skills), full: false } };
}

function MapView(props: {
  api: RestLike | null;
  profile: string | null;
  graph: LearningGraph;
  full: boolean;
  reload: () => void;
}) {
  const t = useT();
  const [filter, setFilter] = useState<MapFilter>("all");
  const range = timeRange(props.graph);
  const [until, setUntil] = useState<number | null>(null);
  const [selected, setSelected] = useState<LearningNode | null>(null);
  const [note, setNote] = useState("");
  const shown = useMemo(() => visible(props.graph, filter, until), [props.graph, filter, until]);
  const placed = useMemo(() => layout(props.graph, BOX), [props.graph]);
  const [importText, setImportText] = useState("");

  const toggleSkill = async (node: LearningNode) => {
    const answer = await rest(props.api, "POST", "/api/skills/toggle", {
      profile: props.profile,
      body: { name: node.label, enabled: !node.enabled },
    });
    setNote(answer.ok ? "" : answer.message);
    props.reload();
  };
  const deleteMemory = async (node: LearningNode) => {
    const answer = await rest(
      props.api,
      "DELETE",
      `/api/learning/nodes/${encodeURIComponent(node.id)}`,
      {
        profile: props.profile,
      },
    );
    setNote(answer.ok ? "" : answer.message);
    setSelected(null);
    props.reload();
  };
  const share = async () => {
    const code = shareCode(shown, placed, BOX);
    const copied = await copyText(code);
    setNote(copied ? t("manage.map.copied") : code);
  };
  let imported: ReturnType<typeof readShareCode> | null = null;
  let importError = "";
  if (importText.trim() !== "") {
    try {
      imported = readShareCode(importText);
    } catch (error) {
      importError = error instanceof ShareCodeError ? error.message : t("manage.failed");
    }
  }

  return (
    <div className="manage-body">
      <div className="manage-inline">
        {(["all", "used", "learned"] as const).map((f) => (
          <button
            key={f}
            type="button"
            className="button button-secondary"
            aria-pressed={filter === f}
            onClick={() => setFilter(f)}
          >
            {t(`manage.map.filter.${f}`)}
          </button>
        ))}
        <button type="button" className="button button-secondary" onClick={() => void share()}>
          {t("manage.map.share")}
        </button>
      </div>
      {range !== null && (
        <label className="manage-field">
          {t("manage.map.playback")}
          <input
            type="range"
            min={range.from}
            max={range.to}
            value={until ?? range.to}
            onChange={(e) => setUntil(Number(e.target.value))}
          />
        </label>
      )}
      {!props.full && <p className="manage-muted">{t("manage.map.skillsOnly")}</p>}
      <svg
        className="manage-map"
        viewBox={`0 0 ${BOX.width} ${BOX.height}`}
        role="img"
        aria-label={t("nav.learning-map")}
      >
        {shown.edges.map((edge) => {
          const a = placed.get(edge.from);
          const b = placed.get(edge.to);
          if (a === undefined || b === undefined) return null;
          return (
            <line
              key={`${edge.from}->${edge.to}`}
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              stroke="var(--color-line)"
            />
          );
        })}
        {shown.nodes.map((node) => {
          const at = placed.get(node.id);
          if (at === undefined) return null;
          return (
            // biome-ignore lint/a11y/useSemanticElements: SVG has no <button>; the group takes its role
            <g
              key={node.id}
              data-node={node.id}
              role="button"
              tabIndex={0}
              aria-label={node.label}
              onClick={() => setSelected(node)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") setSelected(node);
              }}
              style={{ cursor: "pointer" }}
            >
              <circle
                cx={at.x}
                cy={at.y}
                r={at.r}
                fill={node.kind === "skill" ? "var(--color-accent)" : "var(--color-ok)"}
                opacity={node.enabled ? 0.9 : 0.35}
              />
              <title>{node.label}</title>
            </g>
          );
        })}
      </svg>
      <p className="manage-muted" data-testid="map-count">
        {t("manage.map.count", { count: shown.nodes.length })}
      </p>
      {selected !== null && (
        <div className="manage-card" role="dialog" aria-label={selected.label}>
          <h3>{selected.label}</h3>
          <p className="manage-muted">{selected.category}</p>
          {selected.kind === "skill" ? (
            <button
              type="button"
              className="button button-secondary"
              onClick={() => void toggleSkill(selected)}
            >
              {selected.enabled ? t("manage.map.disable") : t("manage.map.enable")}
            </button>
          ) : (
            props.full && (
              <button
                type="button"
                className="button button-secondary"
                onClick={() => void deleteMemory(selected)}
              >
                {t("manage.delete")}
              </button>
            )
          )}
          <button
            type="button"
            className="button button-secondary"
            onClick={() => setSelected(null)}
          >
            {t("common.close")}
          </button>
        </div>
      )}
      {note !== "" && (
        <p className="manage-muted manage-code" role="status">
          {note}
        </p>
      )}
      <label className="manage-field">
        {t("manage.map.import")}
        <textarea rows={2} value={importText} onChange={(e) => setImportText(e.target.value)} />
      </label>
      {importError !== "" && <p className="manage-error">{importError}</p>}
      {imported !== null && (
        <svg
          className="manage-map"
          viewBox="0 0 1 1"
          role="img"
          aria-label={t("manage.map.importedMap")}
        >
          {keyed(imported.nodes).map(({ key, node: n }) => (
            <circle
              key={key}
              cx={n.x}
              cy={n.y}
              r={0.008}
              fill={n.kind === "skill" ? "var(--color-accent)" : "var(--color-ok)"}
            />
          ))}
        </svg>
      )}
    </div>
  );
}

export function LearningMap(props: { api?: RestLike | null; profile: string | null }) {
  const t = useT();
  const api = useApi(props.api);
  const graph = useRestValue(() => loadGraph(api, props.profile), [api, props.profile]);
  return (
    <PageFrame title={t("nav.learning-map")} id="learning-map">
      <LoadState value={graph}>
        {(data) =>
          data.graph.nodes.length === 0 ? (
            <p className="manage-muted">{t("manage.map.empty")}</p>
          ) : (
            <MapView
              api={api}
              profile={props.profile}
              graph={data.graph}
              full={data.full}
              reload={graph.reload}
            />
          )
        }
      </LoadState>
    </PageFrame>
  );
}
