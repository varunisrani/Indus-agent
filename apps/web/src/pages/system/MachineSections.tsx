// System page: Host (machine numbers, our version and the update check in the card's footer) and
// the Daemon row (state on the left, START / RESTART / STOP on the right).

import { BRAND_NAME, BRAND_SHORT } from "@indus/ui";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Cpu,
  Download,
  HardDrive,
  LoaderCircle,
  Play,
  Power,
  RotateCw,
  Server,
} from "lucide-react";
import { type ReactNode, useState } from "react";
import { api } from "../../lib/api.ts";
import { formatSize } from "../../lib/format.ts";
import type { HostStats, UpdateInfo } from "../../lib/restTypes.ts";
import { Badge, Button, Card, QueryState } from "../../ui/controls.tsx";
import { ConfirmDialog } from "../../ui/Dialog.tsx";
import { SystemSection, tileLabel, useLoggedAction } from "./frame.tsx";

/** "2.6 GB / 15.7 GB (16.5%)". */
export function usage(used: number, total: number): string {
  const pct = total > 0 ? ` (${((used * 100) / total).toFixed(1)}%)` : "";
  return `${formatSize(used)} / ${formatSize(total)}${pct}`;
}

interface Tile {
  label: string;
  icon?: ReactNode;
  value: ReactNode;
}

/** The version badge: how far behind, "latest", or nothing when we cannot tell. */
export function updateBadge(info: UpdateInfo): string {
  if (info.update_available && info.behind) return `${info.behind} behind`;
  if (info.behind === 0) return "latest";
  return "";
}

function hostTiles(stats: HostStats | undefined, info: UpdateInfo | undefined): Tile[] {
  const tiles: Tile[] = [];
  if (info) {
    const badge = updateBadge(info);
    tiles.push({
      label: BRAND_SHORT,
      value: (
        <span className="flex items-center gap-2">
          v{info.current_version}
          {badge ? <Badge tone={info.update_available ? "warn" : "ok"}>{badge}</Badge> : null}
        </span>
      ),
    });
  }
  if (!stats) return tiles;
  if (stats.cpu_count !== null) {
    const busy = stats.cpu_percent !== undefined ? ` · ${Math.round(stats.cpu_percent)}%` : "";
    tiles.push({
      label: "CPU",
      icon: <Cpu aria-hidden="true" />,
      value: `${stats.cpu_count} cores${busy}`,
    });
  }
  if (stats.memory && stats.memory.available !== null) {
    tiles.push({
      label: "Memory",
      value: usage(stats.memory.total - stats.memory.available, stats.memory.total),
    });
  }
  if (stats.disk) {
    tiles.push({
      label: "Disk",
      icon: <HardDrive aria-hidden="true" />,
      value: usage(stats.disk.used, stats.disk.total),
    });
  }
  if (stats.load) {
    tiles.push({ label: "Load avg", value: stats.load.map((n) => n.toFixed(2)).join(" / ") });
  }
  return tiles;
}

export function HostSection() {
  const client = useQueryClient();
  const [force, setForce] = useState(0);
  const [asking, setAsking] = useState(false);
  const stats = useQuery({ queryKey: ["system", "stats"], queryFn: () => api.system.stats() });
  const info = useQuery({
    queryKey: ["system", "update", force],
    queryFn: () => api.system.update(force > 0),
  });
  const apply = useLoggedAction(
    () => api.system.applyUpdate(),
    () => ({ name: "update", title: "update" }),
    () => void client.invalidateQueries({ queryKey: ["system", "update"] }),
  );
  const data = info.data;
  const tiles = hostTiles(stats.data, data);
  return (
    <SystemSection icon={Server} title="Host">
      <Card className="flex flex-col">
        <QueryState
          loading={stats.isPending}
          error={stats.error}
          what="the host numbers"
          onRetry={() => void stats.refetch()}
        >
          <dl className="m-0 grid grid-cols-1 gap-x-6 gap-y-[0.4375rem] sm:grid-cols-3">
            {tiles.map((tile) => (
              <div key={tile.label} className="flex min-w-0 flex-col gap-0.5">
                <dt className={`flex items-center gap-1 ${tileLabel} [&_svg]:size-3`}>
                  {tile.icon}
                  {tile.label}
                </dt>
                <dd className="m-0 text-[0.875rem] leading-5 text-ink">{tile.value}</dd>
              </div>
            ))}
          </dl>
        </QueryState>
        <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-line pt-4">
          <Button
            tone="ghost"
            size="sm"
            icon={info.isFetching ? <LoaderCircle className="animate-spin" /> : <RotateCw />}
            onClick={() => setForce((n) => n + 1)}
            disabled={info.isFetching}
          >
            Check for updates
          </Button>
          {data?.update_available && data.can_apply ? (
            <Button
              tone="primary"
              size="sm"
              icon={<Download />}
              onClick={() => setAsking(true)}
              disabled={apply.pending}
            >
              Update now
            </Button>
          ) : null}
          {data?.message ? (
            <span className="text-[0.75rem] text-ink-muted">{data.message}</span>
          ) : null}
          {info.error ? (
            <span className="text-[0.75rem] text-danger">
              Could not check for updates: {(info.error as Error).message}
            </span>
          ) : null}
        </div>
      </Card>
      <ConfirmDialog
        open={asking}
        title={`Update ${BRAND_NAME}?`}
        description="This pulls the new commits into the git checkout (fast-forward only). Restart the dashboard and the daemon afterwards to run the new version."
        confirmLabel="Update"
        onConfirm={() => {
          setAsking(false);
          apply.start();
        }}
        onCancel={() => setAsking(false)}
      />
    </SystemSection>
  );
}

type DaemonVerb = "start" | "stop" | "restart";

export function DaemonSection() {
  const client = useQueryClient();
  const [stopping, setStopping] = useState(false);
  const daemon = useQuery({ queryKey: ["system", "daemon"], queryFn: () => api.daemon.get() });
  const action = useLoggedAction(
    (verb: DaemonVerb) => api.daemon[verb](),
    (verb) => ({ name: `daemon-${verb}`, title: `daemon ${verb}` }),
    () => void client.invalidateQueries({ queryKey: ["system", "daemon"] }),
  );
  const data = daemon.data;
  const odd = data && !data.running && data.state !== "stopped";
  return (
    <SystemSection icon={Power} title="Daemon">
      <Card padded={false}>
        <div className="flex flex-wrap items-center gap-3 p-4">
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <QueryState
              loading={daemon.isPending}
              error={daemon.error}
              what="the daemon state"
              onRetry={() => void daemon.refetch()}
            >
              {data ? (
                <Badge tone={data.running ? "ok" : "neutral"}>
                  {data.running ? "running" : odd ? data.state : "stopped"}
                </Badge>
              ) : null}
              {data?.running ? (
                <span className="truncate text-[0.875rem] text-ink-muted">
                  Process {data.pid}
                  {data.code_version ? ` · code ${data.code_version}` : ""}
                  {data.draining ? " · draining (finishing current turns)" : ""}
                </span>
              ) : (
                <span className="text-[0.875rem] text-ink-muted">
                  The daemon runs channels, cron and shared sessions for this profile.
                </span>
              )}
            </QueryState>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button
              size="sm"
              className="uppercase"
              icon={<Play />}
              onClick={() => action.start("start")}
              disabled={action.pending || data?.running === true}
            >
              Start
            </Button>
            <Button
              size="sm"
              className="uppercase"
              icon={<RotateCw />}
              onClick={() => action.start("restart")}
              disabled={action.pending}
            >
              Restart
            </Button>
            <Button
              size="sm"
              tone="ghost"
              className="text-warning uppercase"
              icon={<Power />}
              onClick={() => setStopping(true)}
              disabled={action.pending || data?.running === false}
            >
              Stop
            </Button>
          </div>
        </div>
        {odd ? (
          <p className="m-0 border-t border-line px-4 py-3 text-[0.75rem] text-ink-muted">
            The daemon reports {data.state}.
          </p>
        ) : null}
      </Card>
      <ConfirmDialog
        open={stopping}
        title="Stop the daemon?"
        description="Channels disconnect and cron jobs pause until it starts again. Turns already running finish first."
        confirmLabel="Stop"
        danger
        onConfirm={() => {
          setStopping(false);
          action.start("stop");
        }}
        onCancel={() => setStopping(false)}
      />
    </SystemSection>
  );
}
