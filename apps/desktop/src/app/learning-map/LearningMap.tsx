// The learning map (P24-28; W52-l look: an overlay card whose graph fills the panel): skills and
// memories as a force-directed graph (`layout.ts`; W52-l-6: no radial time rings), with a floating
// playback slider over time, text-tab filters all / used / learned, a node popover to switch a
// skill on/off or delete a memory, and Share / Load code, which copy and read a code holding the
// layout only.
//
// Data: `GET /api/learning/graph` (`{nodes, edges}`; memories with timestamps and the skill they
// were learned with) when the server has it; otherwise the skills list alone (`GET /api/skills`,
// linked by category, no times — playback then has nothing to replay).

import { AlertTriangle, FileInput, Lightbulb, Share2 } from "lucide-react";
import { type ReactNode, useMemo, useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { type RestAnswer, type RestLike, rest } from "../../settings/settingsApi.ts";
import { Button, EmptyState, IconButton, SmallDialog } from "../../ui/components.tsx";
import { copyText } from "../clipboard.ts";
import { InlineError, TextAction, TextTabs } from "../kit.tsx";
import { Loader } from "../Loader.tsx";
import { useApi, useRestValue } from "../manage/common.tsx";
import { OverlayCard } from "../overlay/OverlayFrame.tsx";
import "./learningMap.css";
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
  const [loading, setLoading] = useState(false);
  const [importText, setImportText] = useState("");
  const [imported, setImported] = useState<ReturnType<typeof readShareCode> | null>(null);
  const [importError, setImportError] = useState("");

  const toggleSkill = async (node: LearningNode) => {
    const answer = await rest(props.api, "POST", "/api/skills/toggle", {
      profile: props.profile,
      body: { name: node.label, enabled: !node.enabled },
    });
    setNote(answer.ok ? "" : answer.message);
    setSelected(null);
    props.reload();
  };
  const deleteMemory = async (node: LearningNode) => {
    const answer = await rest(
      props.api,
      "DELETE",
      `/api/learning/nodes/${encodeURIComponent(node.id)}`,
      { profile: props.profile },
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
  const showCode = () => {
    try {
      setImported(readShareCode(importText));
      setImportError("");
      setLoading(false);
      setSelected(null);
    } catch (error) {
      setImportError(error instanceof ShareCodeError ? error.message : t("manage.failed"));
    }
  };
  const at = selected === null ? undefined : placed.get(selected.id);

  return (
    <div className="map-stage">
      <div className="map-top-left">
        {imported === null ? (
          <TextTabs
            label={t("map.filter")}
            value={filter}
            options={(["all", "used", "learned"] as const).map((f) => ({
              value: f,
              label: t(`manage.map.filter.${f}`),
            }))}
            onChange={setFilter}
          />
        ) : (
          <TextAction strong onClick={() => setImported(null)}>
            {t("map.back")}
          </TextAction>
        )}
      </div>
      {range !== null && imported === null && (
        <label className="map-timeline">
          <span className="sr-only">{t("manage.map.playback")}</span>
          <input
            type="range"
            min={range.from}
            max={range.to}
            value={until ?? range.to}
            aria-label={t("manage.map.playback")}
            onChange={(e) => setUntil(Number(e.target.value))}
          />
        </label>
      )}
      {imported === null ? (
        <svg
          className="map-svg"
          viewBox={`0 0 ${BOX.width} ${BOX.height}`}
          preserveAspectRatio="xMidYMid meet"
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
                className="map-edge"
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
              />
            );
          })}
          {shown.nodes.map((node) => {
            const spot = placed.get(node.id);
            if (spot === undefined) return null;
            return (
              // biome-ignore lint/a11y/useSemanticElements: SVG has no <button>; the group takes its role
              <g
                key={node.id}
                data-node={node.id}
                className="map-node"
                data-kind={node.kind}
                data-off={node.enabled ? undefined : "true"}
                role="button"
                tabIndex={0}
                aria-label={node.label}
                onClick={() => setSelected(node)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") setSelected(node);
                }}
              >
                {node.kind === "skill" ? (
                  <circle cx={spot.x} cy={spot.y} r={spot.r} />
                ) : (
                  <rect
                    x={spot.x - spot.r * 0.8}
                    y={spot.y - spot.r * 0.8}
                    width={spot.r * 1.6}
                    height={spot.r * 1.6}
                    transform={`rotate(45 ${spot.x} ${spot.y})`}
                  />
                )}
                <title>{node.label}</title>
              </g>
            );
          })}
        </svg>
      ) : (
        <svg
          className="map-svg"
          viewBox="0 0 1 1"
          preserveAspectRatio="xMidYMid meet"
          role="img"
          aria-label={t("manage.map.importedMap")}
        >
          {keyed(imported.nodes).map(({ key, node: n }) => (
            <circle
              key={key}
              className="map-node-dot"
              data-kind={n.kind}
              cx={n.x}
              cy={n.y}
              r={0.008}
            />
          ))}
        </svg>
      )}
      {selected !== null && at !== undefined && (
        <div
          className="map-popover"
          role="dialog"
          aria-label={selected.label}
          style={{
            left: `${(at.x / BOX.width) * 100}%`,
            top: `${(at.y / BOX.height) * 100}%`,
          }}
        >
          <p className="map-popover-title">{selected.label}</p>
          <p className="map-popover-meta">{selected.category}</p>
          <div className="map-popover-actions">
            {selected.kind === "skill" ? (
              <TextAction strong onClick={() => void toggleSkill(selected)}>
                {selected.enabled ? t("manage.map.disable") : t("manage.map.enable")}
              </TextAction>
            ) : (
              props.full && (
                <TextAction strong tone="danger" onClick={() => void deleteMemory(selected)}>
                  {t("manage.delete")}
                </TextAction>
              )
            )}
            <TextAction onClick={() => setSelected(null)}>{t("common.close")}</TextAction>
          </div>
        </div>
      )}
      <div className="map-legend">
        <span>
          <span className="map-key" data-kind="skill" aria-hidden="true" />
          {t("map.skill")}
        </span>
        <span>
          <span className="map-key" data-kind="memory" aria-hidden="true" />
          {t("map.memory")}
        </span>
        <span className="map-legend-note" data-testid="map-count">
          {t("manage.map.count", { count: shown.nodes.length })}
        </span>
        {!props.full && <span className="map-legend-note">{t("manage.map.skillsOnly")}</span>}
        {note !== "" && (
          <span className="map-legend-note map-code" role="status">
            {note}
          </span>
        )}
      </div>
      <div className="map-share">
        <IconButton
          label={t("manage.map.share")}
          icon={<Share2 aria-hidden="true" size={14} />}
          onClick={() => void share()}
        />
        <IconButton
          label={t("manage.map.import")}
          icon={<FileInput aria-hidden="true" size={14} />}
          onClick={() => setLoading(true)}
        />
      </div>
      {loading && (
        <SmallDialog
          title={t("manage.map.import")}
          closeLabel={t("common.close")}
          onClose={() => setLoading(false)}
        >
          <textarea
            className="map-code-input"
            rows={5}
            aria-label={t("map.code")}
            value={importText}
            onChange={(e) => setImportText(e.target.value)}
          />
          {importError !== "" && <InlineError>{importError}</InlineError>}
          <Button
            variant="primary"
            className="map-code-button"
            disabled={importText.trim() === ""}
            onClick={showCode}
          >
            {t("map.showCode")}
          </Button>
        </SmallDialog>
      )}
    </div>
  );
}

export function LearningMap(props: {
  api?: RestLike | null;
  profile: string | null;
  onClose?: () => void;
}) {
  const t = useT();
  const api = useApi(props.api);
  const graph = useRestValue(() => loadGraph(api, props.profile), [api, props.profile]);
  let body: ReactNode;
  if (graph.state === "loading") body = <Loader />;
  else if (graph.state === "error") {
    body = (
      <EmptyState
        look="panel"
        icon={<AlertTriangle aria-hidden="true" />}
        title={t("map.errorTitle")}
        description={graph.code === "not-connected" ? t("manage.notConnected") : graph.message}
        action={
          <Button variant="secondary" size="sm" onClick={graph.reload}>
            {t("manage.retry")}
          </Button>
        }
      />
    );
  } else if (graph.data.graph.nodes.length === 0) {
    body = (
      <EmptyState
        look="panel"
        icon={<Lightbulb aria-hidden="true" />}
        title={t("manage.map.empty")}
        description={t("map.emptyBody")}
      />
    );
  } else {
    body = (
      <MapView
        api={api}
        profile={props.profile}
        graph={graph.data.graph}
        full={graph.data.full}
        reload={graph.reload}
      />
    );
  }
  return (
    <OverlayCard
      id="learning-map"
      title={t("nav.learning-map")}
      closeLabel={t("common.close")}
      onClose={props.onClose ?? (() => {})}
    >
      <div className="map-frame">{body}</div>
    </OverlayCard>
  );
}
