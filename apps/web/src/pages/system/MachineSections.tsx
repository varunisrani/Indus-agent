// System page: host numbers, the update check, and the daemon's start / stop / restart.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../lib/api.ts";
import { formatBytes } from "../../lib/format.ts";
import type { HostStats, UpdateInfo } from "../../lib/restTypes.ts";
import { ActionLog, useActionStarter } from "../../ui/ActionLog.tsx";
import { Badge, Button, Card, Notice, QueryState } from "../../ui/controls.tsx";
import { ConfirmDialog } from "../../ui/Dialog.tsx";

function percent(part: number | null, whole: number): string {
  if (part === null || whole <= 0) return "";
  return ` (${Math.round((part * 100) / whole)} %)`;
}

function StatsList({ stats }: { stats: HostStats }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
      <dt className="text-muted">CPUs</dt>
      <dd>
        {stats.cpu_count ?? "unknown"}
        {stats.cpu_percent !== undefined ? ` · ${Math.round(stats.cpu_percent)} % busy` : ""}
      </dd>
      <dt className="text-muted">Load</dt>
      <dd>{stats.load ? stats.load.join(" / ") : "unknown"}</dd>
      <dt className="text-muted">Memory free</dt>
      <dd>
        {stats.memory
          ? `${formatBytes(stats.memory.available)} of ${formatBytes(stats.memory.total)}${percent(
              stats.memory.available,
              stats.memory.total,
            )}`
          : "unknown"}
      </dd>
      <dt className="text-muted">Disk free</dt>
      <dd>
        {stats.disk
          ? `${formatBytes(stats.disk.free)} of ${formatBytes(stats.disk.total)}${percent(
              stats.disk.free,
              stats.disk.total,
            )}`
          : "unknown"}
      </dd>
    </dl>
  );
}

export function HostSection() {
  const stats = useQuery({ queryKey: ["system", "stats"], queryFn: () => api.system.stats() });
  return (
    <Card className="flex flex-col gap-2">
      <h2 className="text-sm font-semibold">This machine</h2>
      <QueryState loading={stats.isPending} error={stats.error}>
        {stats.data ? <StatsList stats={stats.data} /> : null}
      </QueryState>
    </Card>
  );
}

/** The badge text for an update answer. */
export function updateBadge(info: UpdateInfo): string {
  if (info.update_available && info.behind) {
    return `${info.behind} commit${info.behind === 1 ? "" : "s"} behind`;
  }
  if (info.behind === 0) return "Up to date";
  return info.install_method === "git" ? "Not checked" : info.install_method;
}

export function UpdateSection() {
  const client = useQueryClient();
  const [force, setForce] = useState(0);
  const [asking, setAsking] = useState(false);
  const info = useQuery({
    queryKey: ["system", "update", force],
    queryFn: () => api.system.update(force > 0),
  });
  const apply = useActionStarter(() => api.system.applyUpdate());
  const data = info.data;
  return (
    <Card className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="flex-1 text-sm font-semibold">Updates</h2>
        {data ? (
          <Badge tone={data.update_available ? "warn" : "neutral"}>{updateBadge(data)}</Badge>
        ) : null}
      </div>
      <QueryState loading={info.isPending} error={info.error}>
        {data ? (
          <>
            <p className="text-sm">
              Version {data.current_version} · installed as {data.install_method}
            </p>
            {data.message ? <p className="text-xs text-muted">{data.message}</p> : null}
            <div className="flex gap-2">
              <Button onClick={() => setForce((n) => n + 1)} disabled={info.isFetching}>
                Check again
              </Button>
              {data.can_apply && data.update_available ? (
                <Button tone="primary" onClick={() => setAsking(true)} disabled={apply.pending}>
                  Update
                </Button>
              ) : null}
            </div>
          </>
        ) : null}
      </QueryState>
      {apply.run > 0 ? (
        <ActionLog
          name="update"
          run={apply.run}
          title="Update"
          onFinished={() => void client.invalidateQueries({ queryKey: ["system", "update"] })}
        />
      ) : null}
      <ConfirmDialog
        open={asking}
        title="Update Indus Agent?"
        description="This pulls the new commits into the git checkout (fast-forward only). Restart the dashboard and the daemon afterwards to run the new version."
        confirmLabel="Update"
        onConfirm={() => {
          setAsking(false);
          apply.start();
        }}
        onCancel={() => setAsking(false)}
      />
    </Card>
  );
}

type DaemonVerb = "start" | "stop" | "restart";

export function DaemonSection() {
  const client = useQueryClient();
  const [verb, setVerb] = useState<DaemonVerb>("start");
  const [stopping, setStopping] = useState(false);
  const daemon = useQuery({ queryKey: ["system", "daemon"], queryFn: () => api.daemon.get() });
  const action = useActionStarter((next: DaemonVerb) => api.daemon[next]());
  const run = (next: DaemonVerb) => {
    setVerb(next);
    action.start(next);
  };
  const data = daemon.data;
  return (
    <Card className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="flex-1 text-sm font-semibold">Daemon</h2>
        {data ? (
          <Badge tone={data.running ? "ok" : "neutral"}>
            {data.running ? data.state : "stopped"}
          </Badge>
        ) : null}
      </div>
      <QueryState loading={daemon.isPending} error={daemon.error}>
        {data?.running ? (
          <p className="text-xs text-muted">
            Process {data.pid}
            {data.code_version ? ` · code ${data.code_version}` : ""}
            {data.draining ? " · draining (finishing current turns)" : ""}
          </p>
        ) : (
          <p className="text-xs text-muted">
            The daemon runs channels, cron and shared sessions for this profile.
          </p>
        )}
      </QueryState>
      <div className="flex gap-2">
        <Button onClick={() => run("start")} disabled={action.pending || data?.running === true}>
          Start
        </Button>
        <Button
          tone="danger"
          onClick={() => setStopping(true)}
          disabled={action.pending || data?.running === false}
        >
          Stop
        </Button>
        <Button onClick={() => run("restart")} disabled={action.pending}>
          Restart
        </Button>
      </div>
      {action.run > 0 ? (
        <ActionLog
          name={`daemon-${verb}`}
          run={action.run}
          title={`Daemon ${verb}`}
          onFinished={() => void client.invalidateQueries({ queryKey: ["system", "daemon"] })}
        />
      ) : null}
      <ConfirmDialog
        open={stopping}
        title="Stop the daemon?"
        description="Channels disconnect and cron jobs pause until it starts again. Turns already running finish first."
        confirmLabel="Stop"
        danger
        onConfirm={() => {
          setStopping(false);
          run("stop");
        }}
        onCancel={() => setStopping(false)}
      />
      {data && !data.running && data.state !== "stopped" ? (
        <Notice tone="warn">The daemon reports {data.state}.</Notice>
      ) : null}
    </Card>
  );
}
