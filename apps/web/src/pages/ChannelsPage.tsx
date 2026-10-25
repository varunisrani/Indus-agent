// Channels (P18-23): one row per messaging platform the gateway knows. Each row shows the state
// the server works out, an on/off switch, a Test button and Configure, which opens that platform's
// own fields in a dialog. Credentials are password inputs that always start empty: the stored value
// never reaches the browser, only a masked preview, and a field left blank keeps what is stored.
// Any change needs a daemon restart; the banner saying so stays until the daemon itself reports a
// new process. Look (W52-d): single column of short outlined rows, notices as bordered cards.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CircleCheck,
  LoaderCircle,
  PlugZap,
  Radio,
  RotateCw,
  SlidersHorizontal,
  TriangleAlert,
  WifiOff,
} from "lucide-react";
import { type FormEvent, useCallback, useEffect, useId, useState } from "react";
import { api } from "../lib/api.ts";
import type {
  DaemonInfo,
  MessagingPlatform,
  PlatformField,
  PlatformTestResult,
  PlatformUpdate,
} from "../lib/restTypes.ts";
import { useVisiblePolling } from "../lib/visibility.ts";
import { HeaderActions } from "../shell/PageHeader.tsx";
import { useProfileScope } from "../shell/ProfileScope.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { ActionLog, useActionStarter } from "../ui/ActionLog.tsx";
import {
  Badge,
  type BadgeTone,
  Button,
  Callout,
  CodeChip,
  EmptyCard,
  Input,
  PageSpinner,
  QueryState,
} from "../ui/controls.tsx";
import { DialogActions, Modal } from "../ui/Dialog.tsx";
import { Switch } from "../ui/Switch.tsx";

export const DAEMON_POLL_MS = 5_000;
const RESTART_KEY_PREFIX = "indus.channels.restart.";

const STATES: Record<string, [string, BadgeTone]> = {
  disabled: ["Switched off", "secondary"],
  not_configured: ["Needs setup", "neutral"],
  daemon_stopped: ["Daemon stopped", "warn"],
  pending_restart: ["Restart to apply", "warn"],
  connected: ["Connected", "ok"],
  retrying: ["Reconnecting", "warn"],
  fatal: ["Failed", "danger"],
  paused: ["Paused", "secondary"],
};

export function stateBadge(state: string): [string, BadgeTone] {
  return STATES[state] ?? [state.replaceAll("_", " "), "neutral"];
}

// -- the restart mark: "settings changed while the daemon ran as pid N" ------------------------

interface RestartMark {
  /** The daemon's pid when the change was made; null when it was not running. */
  pid: number | null;
}

function markKey(profile: string): string {
  return `${RESTART_KEY_PREFIX}${profile || "(serving)"}`;
}

export function readRestartMark(profile: string): RestartMark | null {
  try {
    const raw = window.sessionStorage.getItem(markKey(profile));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { pid?: unknown };
    return { pid: typeof parsed.pid === "number" ? parsed.pid : null };
  } catch {
    return null;
  }
}

function writeRestartMark(profile: string, mark: RestartMark | null): void {
  try {
    if (mark) window.sessionStorage.setItem(markKey(profile), JSON.stringify(mark));
    else window.sessionStorage.removeItem(markKey(profile));
  } catch {
    // storage unavailable: the banner lasts only for this page view
  }
}

/** True once the daemon reports a process other than the one the change was made under. */
export function restartSeen(mark: RestartMark, daemon: DaemonInfo | undefined): boolean {
  if (!daemon?.running || daemon.pid === null) return false;
  return daemon.pid !== mark.pid;
}

function useRestartMark(daemon: DaemonInfo | undefined) {
  const { active } = useProfileScope();
  const [mark, setMark] = useState<RestartMark | null>(() => readRestartMark(active));

  useEffect(() => {
    if (mark && restartSeen(mark, daemon)) {
      writeRestartMark(active, null);
      setMark(null);
    }
  }, [mark, daemon, active]);

  const raise = useCallback(
    (current: DaemonInfo | undefined) => {
      setMark((was) => {
        // Keep the first pid: a second change under the same process needs the same restart.
        const next = was ?? { pid: current?.running ? current.pid : null };
        writeRestartMark(active, next);
        return next;
      });
    },
    [active],
  );
  return { mark, raise };
}

function RestartBanner({
  mark,
  daemon,
  onRestart,
  busy,
}: {
  mark: RestartMark;
  daemon: DaemonInfo | undefined;
  onRestart: () => void;
  busy: boolean;
}) {
  const stopped = daemon ? !daemon.running : mark.pid === null;
  return (
    <Callout
      role="status"
      data-testid="restart-banner"
      tone="warning"
      icon={TriangleAlert}
      action={
        <Button
          tone="primary"
          size="sm"
          className="uppercase"
          icon={<RotateCw />}
          onClick={onRestart}
          disabled={busy}
        >
          {stopped ? "Start daemon" : "Restart daemon"}
        </Button>
      }
    >
      {stopped
        ? "Settings saved. Start the daemon to connect with them."
        : "Settings saved. Restart the daemon to apply them; this notice stays until it has restarted."}
    </Callout>
  );
}

// -- one platform ------------------------------------------------------------------------------

function initialDrafts(fields: PlatformField[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const field of fields) out[field.key] = field.is_secret ? "" : (field.value ?? "");
  return out;
}

/** The `env` a save sends: typed credentials and changed settings; blanks keep what is stored. */
export function changedValues(
  fields: PlatformField[],
  drafts: Record<string, string>,
): Record<string, string> {
  const env: Record<string, string> = {};
  for (const field of fields) {
    const typed = (drafts[field.key] ?? "").trim();
    if (!typed) continue;
    if (!field.is_secret && typed === (field.value ?? "")) continue;
    env[field.key] = typed;
  }
  return env;
}

const KIND_HINT: Record<PlatformField["kind"], string> = {
  credential: "secret",
  setting: "setting",
  policy: "access rule",
};

function ConfigureForm({
  platform,
  onSaved,
  onCancel,
  onChanged,
}: {
  platform: MessagingPlatform;
  onSaved: () => void;
  onCancel: () => void;
  onChanged: () => void;
}) {
  const toast = useToast();
  const client = useQueryClient();
  const ids = useId();
  const [drafts, setDrafts] = useState(() => initialDrafts(platform.fields));
  const [problem, setProblem] = useState("");

  const save = useMutation({
    mutationFn: (body: PlatformUpdate) => api.messaging.update(platform.id, body),
    onSuccess: (reply, body) => {
      setProblem("");
      // A cleared field's input empties; credentials never keep what was typed.
      setDrafts((all) => {
        const next = { ...all };
        for (const field of platform.fields) {
          if (field.is_secret || body.clear_env?.includes(field.key)) next[field.key] = "";
        }
        return next;
      });
      if (reply.restart_needed) onChanged();
      toast.show(`${platform.name} saved.`, "success");
      void client.invalidateQueries({ queryKey: ["messaging"] });
      if (body.env) onSaved();
    },
    onError: (error) => setProblem(errorText(error)),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const env = changedValues(platform.fields, drafts);
    if (!Object.keys(env).length) {
      setProblem("Nothing to save: type a new value into a field first.");
      return;
    }
    save.mutate({ env });
  };

  return (
    <form className="flex flex-col gap-4" onSubmit={submit} noValidate>
      {platform.fields.map((field) => {
        const id = `${ids}-${field.key}`;
        const hint = field.is_secret
          ? field.is_set
            ? `Stored (${field.preview ?? "hidden"}). Leave blank to keep it.`
            : "Not set."
          : undefined;
        return (
          <div key={field.key} className="flex flex-col gap-1.5">
            <div className="flex items-baseline gap-2">
              <label htmlFor={id} className="font-mono text-[0.75rem] font-medium text-ink">
                {field.key}
                {field.required ? " *" : ""}
              </label>
              <span className="text-[0.75rem] text-ink-soft">{KIND_HINT[field.kind]}</span>
            </div>
            <div className="flex items-center gap-2">
              <Input
                id={id}
                type={field.is_secret ? "password" : "text"}
                autoComplete={field.is_secret ? "new-password" : "off"}
                className="min-w-0 flex-1"
                placeholder={field.is_secret && field.is_set ? (field.preview ?? "") : field.key}
                value={drafts[field.key] ?? ""}
                onChange={(e) => {
                  const value = e.currentTarget.value;
                  setDrafts((all) => ({ ...all, [field.key]: value }));
                }}
              />
              {field.is_set ? (
                <Button
                  size="sm"
                  aria-label={`Clear ${field.key}`}
                  disabled={save.isPending}
                  onClick={() => save.mutate({ clear_env: [field.key] })}
                >
                  Clear
                </Button>
              ) : null}
            </div>
            {hint ? <p className="m-0 text-[0.75rem] text-ink-soft">{hint}</p> : null}
          </div>
        );
      })}
      {problem ? (
        <p role="alert" className="m-0 text-[0.75rem] text-danger">
          {problem}
        </p>
      ) : null}
      <DialogActions>
        <Button tone="ghost" size="lg" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          type="submit"
          tone="primary"
          size="lg"
          disabled={save.isPending}
          icon={save.isPending ? <LoaderCircle className="animate-spin" /> : null}
        >
          Save
        </Button>
      </DialogActions>
    </form>
  );
}

function StateIcon({ state }: { state: string }) {
  if (state === "connected")
    return <CircleCheck aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-success" />;
  if (state === "fatal")
    return <TriangleAlert aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-danger" />;
  return <Radio aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-ink-soft" />;
}

function PlatformRow({
  platform,
  onChanged,
}: {
  platform: MessagingPlatform;
  onChanged: () => void;
}) {
  const toast = useToast();
  const client = useQueryClient();
  const [configuring, setConfiguring] = useState(false);
  const [result, setResult] = useState<PlatformTestResult | null>(null);

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.messaging.update(platform.id, { enabled }),
    onSuccess: (reply) => {
      if (reply.restart_needed) onChanged();
      toast.show(`${platform.name} saved.`, "success");
      void client.invalidateQueries({ queryKey: ["messaging"] });
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const test = useMutation({
    mutationFn: () => api.messaging.test(platform.id),
    onSuccess: (reply) => {
      setResult(reply);
      toast.show(reply.message, reply.ok ? "success" : "info");
    },
    onError: (error) => {
      const message = errorText(error);
      setResult({ ok: false, state: "error", message });
      toast.show(message, "error");
    },
  });

  const [label, tone] = stateBadge(platform.state);
  return (
    <li
      data-testid={`platform-${platform.id}`}
      className="flex flex-wrap items-center gap-x-4 gap-y-3 border border-line p-4"
    >
      <div className="flex min-w-0 flex-1 items-start gap-2.5">
        <StateIcon state={platform.state} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="type-serif m-0 text-[0.9375rem] font-normal text-ink">
              {platform.name}
            </h2>
            <Badge tone={tone}>{label}</Badge>
          </div>
          {platform.description ? (
            <p className="m-0 mt-1 text-[0.75rem] text-ink-muted">{platform.description}</p>
          ) : null}
          {!platform.installed ? (
            <p className="m-0 mt-1 text-[0.75rem] text-warning">
              Library not installed{platform.install_hint ? `: ${platform.install_hint}` : "."}
            </p>
          ) : null}
          {/* "not configured" only repeats the badge; other reasons say something new. */}
          {platform.reason && platform.state !== "not_configured" ? (
            <p className="m-0 mt-1 text-[0.75rem] text-ink-soft">{platform.reason}</p>
          ) : null}
          {result ? (
            <p
              data-testid={`test-result-${platform.id}`}
              className={`m-0 mt-1 text-[0.75rem] ${result.ok ? "text-success" : "text-warning"}`}
            >
              {result.message}
            </p>
          ) : null}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {toggle.isPending ? (
          <span className="inline-flex h-[1.125rem] w-[2.125rem] items-center justify-center">
            <LoaderCircle aria-label="Saving" className="size-4 animate-spin text-ink-soft" />
          </span>
        ) : (
          <Switch
            checked={platform.enabled && !platform.explicitly_disabled}
            onCheckedChange={(enabled) => toggle.mutate(enabled)}
            label={`Enable ${platform.name}`}
          />
        )}
        <Button
          tone="ghost"
          size="sm"
          icon={<PlugZap />}
          onClick={() => test.mutate()}
          disabled={test.isPending}
        >
          {test.isPending ? "Testing…" : "Test"}
        </Button>
        {platform.fields.length ? (
          <Button
            tone="primary"
            size="sm"
            className="uppercase"
            icon={<SlidersHorizontal />}
            onClick={() => setConfiguring(true)}
          >
            Configure
          </Button>
        ) : null}
      </div>
      <Modal
        open={configuring}
        onOpenChange={setConfiguring}
        title={`Configure ${platform.name}`}
        description={
          platform.description || `Credentials and settings ${platform.name} needs to connect.`
        }
      >
        <ConfigureForm
          platform={platform}
          onChanged={onChanged}
          onSaved={() => setConfiguring(false)}
          onCancel={() => setConfiguring(false)}
        />
      </Modal>
    </li>
  );
}

export function ChannelsPage() {
  const client = useQueryClient();
  const platforms = useQuery({
    queryKey: ["messaging", "platforms"],
    queryFn: () => api.messaging.platforms(),
  });
  const daemon = useQuery({ queryKey: ["daemon"], queryFn: () => api.daemon.get() });
  const { mark, raise } = useRestartMark(daemon.data);
  const restart = useActionStarter(() => api.daemon.restart());

  // Live status: the platform states and the daemon's process, every few seconds while visible.
  useVisiblePolling(() => {
    void daemon.refetch();
    void client.invalidateQueries({ queryKey: ["messaging"] });
  }, DAEMON_POLL_MS);

  const list = platforms.data?.platforms ?? [];
  const configured = list.filter((p) => p.configured).length;
  return (
    <div className="flex flex-col gap-6 px-4 py-[1.375rem] lg:pr-8 lg:pl-[1.4375rem]">
      <HeaderActions>
        <Button
          tone="primary"
          size="sm"
          className="uppercase"
          icon={restart.pending ? <LoaderCircle className="animate-spin" /> : <RotateCw />}
          onClick={() => restart.start()}
          disabled={restart.pending}
        >
          {restart.pending ? "Restarting…" : "Restart daemon"}
        </Button>
      </HeaderActions>
      {mark ? (
        <RestartBanner
          mark={mark}
          daemon={daemon.data}
          busy={restart.pending}
          onRestart={() => restart.start()}
        />
      ) : null}
      {restart.run > 0 ? (
        <ActionLog name="daemon-restart" run={restart.run} title="Daemon restart" />
      ) : null}
      {platforms.data && !platforms.data.daemon_running && !mark ? (
        <Callout icon={WifiOff}>
          <span className="text-ink-muted">
            The daemon is not running. Set channels up here, then start it with{" "}
            <CodeChip>indus gateway start</CodeChip> (or the button above).
          </span>
        </Callout>
      ) : null}
      {platforms.isPending ? (
        <PageSpinner />
      ) : (
        <QueryState loading={false} error={platforms.error}>
          {list.length === 0 ? (
            <EmptyCard>No messaging platforms in this install.</EmptyCard>
          ) : (
            <div className="flex flex-col gap-3">
              <p className="m-0 text-[0.75rem] text-ink-muted">
                {configured} of {list.length} channels configured. Credentials are kept in this
                profile's secret store; enabled channels connect on the daemon's next start.
              </p>
              <ul
                className="m-0 flex list-none flex-col gap-3 p-0"
                aria-label="Messaging platforms"
              >
                {list.map((platform) => (
                  <PlatformRow
                    key={platform.id}
                    platform={platform}
                    onChanged={() => raise(daemon.data)}
                  />
                ))}
              </ul>
            </div>
          )}
        </QueryState>
      )}
    </div>
  );
}
