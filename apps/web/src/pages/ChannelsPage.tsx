// Channels (P18-23): one card per messaging platform the gateway knows. Each card shows the state
// the server works out, an on/off switch, that platform's own fields and a Test button. Credentials
// are password inputs that always start empty: the stored value never reaches the browser, only a
// masked preview, and a field left blank keeps what is stored. Any change needs a daemon restart;
// the banner saying so stays until the daemon itself reports a new process.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  Card,
  Field,
  inputClass,
  Notice,
  QueryState,
} from "../ui/controls.tsx";
import { Switch } from "../ui/Switch.tsx";

export const DAEMON_POLL_MS = 5_000;
const RESTART_KEY_PREFIX = "indus.channels.restart.";

const STATES: Record<string, [string, BadgeTone]> = {
  disabled: ["Switched off", "neutral"],
  not_configured: ["Needs setup", "warn"],
  daemon_stopped: ["Daemon stopped", "neutral"],
  pending_restart: ["Restart to apply", "warn"],
  connected: ["Connected", "ok"],
  retrying: ["Reconnecting", "warn"],
  fatal: ["Failed", "danger"],
  paused: ["Paused", "neutral"],
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
    <div
      role="status"
      data-testid="restart-banner"
      className="flex flex-wrap items-center gap-3 rounded-md border border-warn px-3 py-2 text-sm text-warn"
    >
      <span className="flex-1">
        {stopped
          ? "Settings saved. Start the daemon to connect with them."
          : "Settings saved. Restart the daemon to apply them; this notice stays until it has restarted."}
      </span>
      <Button tone="primary" onClick={onRestart} disabled={busy}>
        {stopped ? "Start daemon" : "Restart daemon"}
      </Button>
    </div>
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

function PlatformCard({
  platform,
  onChanged,
}: {
  platform: MessagingPlatform;
  onChanged: () => void;
}) {
  const toast = useToast();
  const client = useQueryClient();
  const ids = useId();
  const [drafts, setDrafts] = useState(() => initialDrafts(platform.fields));
  const [problem, setProblem] = useState("");
  const [result, setResult] = useState<PlatformTestResult | null>(null);

  const save = useMutation({
    mutationFn: (body: PlatformUpdate) => api.messaging.update(platform.id, body),
    onSuccess: (reply, body) => {
      setProblem("");
      // Credential inputs go back to empty; a cleared field's input empties too. Settings keep
      // what was typed, which is now what is stored.
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
    },
    onError: (error) => setProblem(errorText(error)),
  });
  const test = useMutation({
    mutationFn: () => api.messaging.test(platform.id),
    onSuccess: setResult,
    onError: (error) => setResult({ ok: false, state: "error", message: errorText(error) }),
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

  const [label, tone] = stateBadge(platform.state);
  return (
    <li data-testid={`platform-${platform.id}`}>
      <Card className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="flex-1 text-sm font-semibold">{platform.name}</h2>
          <Badge tone={tone}>{label}</Badge>
          <Switch
            checked={platform.enabled && !platform.explicitly_disabled}
            onCheckedChange={(enabled) => save.mutate({ enabled })}
            label={`Enable ${platform.name}`}
            disabled={save.isPending}
          />
        </div>
        {platform.description ? <p className="text-xs text-muted">{platform.description}</p> : null}
        {!platform.installed ? (
          <Notice tone="warn">
            Its library is not installed{platform.install_hint ? `: ${platform.install_hint}` : "."}
          </Notice>
        ) : null}
        {platform.reason ? <p className="text-xs text-muted">{platform.reason}</p> : null}
        <form className="flex flex-col gap-3" onSubmit={submit} noValidate>
          {platform.fields.map((field) => {
            const id = `${ids}-${field.key}`;
            const hint = field.is_secret
              ? field.is_set
                ? `Stored (${field.preview ?? "hidden"}). Leave blank to keep it.`
                : "Not set."
              : undefined;
            return (
              <div key={field.key} className="flex items-end gap-2">
                <div className="flex-1">
                  <Field
                    label={`${field.key}${field.required ? " (required)" : ""} · ${KIND_HINT[field.kind]}`}
                    htmlFor={id}
                    {...(hint ? { hint } : {})}
                  >
                    <input
                      id={id}
                      type={field.is_secret ? "password" : "text"}
                      autoComplete={field.is_secret ? "new-password" : "off"}
                      className={`${inputClass} font-mono`}
                      value={drafts[field.key] ?? ""}
                      onChange={(e) => {
                        const value = e.currentTarget.value;
                        setDrafts((all) => ({ ...all, [field.key]: value }));
                      }}
                    />
                  </Field>
                </div>
                {field.is_set ? (
                  <Button
                    aria-label={`Clear ${field.key}`}
                    disabled={save.isPending}
                    onClick={() => save.mutate({ clear_env: [field.key] })}
                  >
                    Clear
                  </Button>
                ) : null}
              </div>
            );
          })}
          {problem ? <Notice tone="danger">{problem}</Notice> : null}
          <div className="flex gap-2">
            {platform.fields.length ? (
              <Button type="submit" tone="primary" disabled={save.isPending}>
                Save
              </Button>
            ) : null}
            <Button onClick={() => test.mutate()} disabled={test.isPending}>
              {test.isPending ? "Testing…" : "Test"}
            </Button>
          </div>
        </form>
        {result ? (
          <Notice tone={result.ok ? "neutral" : "warn"}>
            <span data-testid={`test-result-${platform.id}`}>{result.message}</span>
          </Notice>
        ) : null}
      </Card>
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
  return (
    <div className="flex flex-col gap-4 p-4">
      <HeaderActions>
        <Button onClick={() => restart.start()} disabled={restart.pending}>
          Restart daemon
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
        <Notice>
          The daemon is not running. Enabled channels connect when it starts (System page, or `indus
          gateway start`).
        </Notice>
      ) : null}
      <QueryState
        loading={platforms.isPending}
        error={platforms.error}
        empty={list.length === 0}
        emptyText="No messaging platforms are available in this install."
      >
        <ul className="grid gap-3 lg:grid-cols-2" aria-label="Messaging platforms">
          {list.map((platform) => (
            <PlatformCard
              key={platform.id}
              platform={platform}
              onChanged={() => raise(daemon.data)}
            />
          ))}
        </ul>
      </QueryState>
    </div>
  );
}
