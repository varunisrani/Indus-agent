// Models (P18-24, rebuilt W52-b): a "Model settings" card (main model through the Switch model
// dialog, side-task models through their own dialog), the usage figures for the chosen period, and
// a card per provider with its key state and device-code sign-in: Start shows the code and the page
// to enter it on, then the session is polled at the interval the provider asked for until it is
// approved, refused, expired or cancelled. No token ever reaches the browser.
// W60-L1-2: the settings card also sums up the mixture-of-agents presets (`moa` in the config; its
// Configure opens that Config section), and a card per model shows the period's share of
// `by_model` from `/api/analytics/usage` (calls always, tokens and cost behind the same flag as
// the totals).

import { useIsFetching, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  CircleAlert,
  Cpu,
  ExternalLink,
  Layers,
  LoaderCircle,
  SlidersHorizontal,
  Star,
} from "lucide-react";
import { type ComponentType, type ReactNode, useEffect, useId, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";
import { api } from "../lib/api.ts";
import { formatCost, formatCount, formatTokens } from "../lib/format.ts";
import type {
  AuxiliaryTask,
  ConfigObject,
  LoginStarted,
  LoginStatus,
  ModelUsageRow,
  ProviderOption,
} from "../lib/restTypes.ts";
import { useTokenAnalyticsFlag } from "../lib/useTokenAnalyticsFlag.ts";
import { HeaderAfterTitle } from "../shell/PageHeader.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { CopyButton } from "../ui/CopyButton.tsx";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Field,
  inputClass,
  LeaderStatList,
  Notice,
  PageSpinner,
  PeriodButtons,
  RefreshButton,
  type StatItem,
} from "../ui/controls.tsx";
import { cx } from "../ui/cx.ts";
import { ConfirmDialog, Modal } from "../ui/Dialog.tsx";
import { type ModelChoice, ModelPickerDialog } from "../ui/ModelPickerDialog.tsx";
import { RANGE_OPTIONS } from "./AnalyticsPage.tsx";

/** Never poll faster than this, whatever the provider says. */
export const MIN_POLL_S = 1;

const LOGIN_TEXT: Record<LoginStatus, string> = {
  pending: "Waiting for you to approve the sign-in…",
  approved: "Signed in; the credential was added to this profile.",
  denied: "The sign-in was refused.",
  expired: "The code expired before it was approved. Start again.",
  failed: "The sign-in failed.",
  cancelled: "The sign-in was cancelled.",
};

function useModelsChanged() {
  const client = useQueryClient();
  return () => void client.invalidateQueries({ queryKey: ["models"] });
}

const LOGIN_TONE: Record<LoginStatus, string> = {
  pending: "text-ink-muted",
  approved: "text-success",
  denied: "text-danger",
  expired: "text-danger",
  failed: "text-danger",
  cancelled: "text-ink-muted",
};

/**
 * Device-code sign-in panel; also used by the Keys page's OAuth card (W52-d-2, W61-M2). With
 * `autoStart` it asks for a code as soon as it opens (the Keys row's Sign in), else it offers
 * "Get a sign-in code" first. The code is shown large with a copy button, the verification page
 * opens in a new tab, and the session is polled at the interval the backend gave, never faster
 * than MIN_POLL_S, until it ends; a failed poll is not an answer.
 */
export function DeviceLogin({
  provider,
  onDone,
  onApproved,
  autoStart = false,
}: {
  provider: ProviderOption;
  onDone: () => void;
  onApproved?: () => void;
  autoStart?: boolean;
}) {
  const client = useQueryClient();
  const [grant, setGrant] = useState<LoginStarted | null>(null);
  const [status, setStatus] = useState<LoginStatus>("pending");
  const [message, setMessage] = useState("");
  const [problem, setProblem] = useState("");
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const asked = useRef(false);

  const stop = () => {
    if (timer.current !== null) clearInterval(timer.current);
    timer.current = null;
  };
  useEffect(
    () => () => {
      if (timer.current !== null) clearInterval(timer.current);
    },
    [],
  );

  const finish = (next: LoginStatus, text: string) => {
    stop();
    setStatus(next);
    setMessage(text);
    if (next === "approved") {
      void client.invalidateQueries({ queryKey: ["models"] });
      onApproved?.();
    }
  };

  const start = useMutation({
    mutationFn: () => api.providerLogin.start(provider.name),
    onMutate: () => {
      stop();
      setProblem("");
    },
    onSuccess: (started) => {
      setGrant(started);
      setStatus("pending");
      setMessage("");
      const every = Math.max(started.interval || MIN_POLL_S, MIN_POLL_S) * 1000;
      timer.current = setInterval(() => {
        api.providerLogin.poll(started.session_id).then(
          (reply) => {
            if (timer.current === null) return; // cancelled meanwhile
            if (reply.status !== "pending") finish(reply.status, reply.message);
          },
          () => {
            // A failed poll is not an answer: keep asking until the session says how it ended.
          },
        );
      }, every);
    },
    onError: (error) => setProblem(errorText(error)),
  });
  const cancel = useMutation({
    mutationFn: (id: string) => api.providerLogin.cancel(id),
    onMutate: () => stop(),
    onSettled: () => finish("cancelled", ""),
  });

  // biome-ignore lint/correctness/useExhaustiveDependencies: asks once, when the panel opens
  useEffect(() => {
    if (!autoStart || asked.current) return;
    asked.current = true;
    start.mutate();
  }, [autoStart]);

  const ended = grant !== null && status !== "pending";
  const failed = status === "denied" || status === "expired" || status === "failed";

  return (
    <section
      aria-label={`Sign in to ${provider.label}`}
      className="flex flex-col gap-3 border border-line p-3"
    >
      {!grant ? (
        autoStart && !problem ? (
          <p role="status" className="m-0 flex items-center gap-2 text-[0.8125rem] text-ink-muted">
            <LoaderCircle aria-hidden="true" className="size-3.5 animate-spin" />
            Asking for a sign-in code…
          </p>
        ) : (
          <div className="flex gap-2">
            <Button tone="primary" onClick={() => start.mutate()} disabled={start.isPending}>
              {start.isPending ? "Asking for a code…" : "Get a sign-in code"}
            </Button>
            <Button onClick={onDone}>Close</Button>
          </div>
        )
      ) : (
        <>
          {status === "pending" ? (
            <>
              <p className="m-0 text-[0.8125rem] text-ink-muted">
                Open the sign-in page, enter this code and approve. This updates by itself.
              </p>
              <div className="flex flex-wrap items-center justify-between gap-2 border border-line bg-selected/40 px-4 py-3">
                <code
                  data-testid="device-code"
                  className="font-mono text-2xl tracking-widest text-ink select-all"
                >
                  {grant.user_code}
                </code>
                <CopyButton value={grant.user_code} label="Copy the code" />
              </div>
              <a
                href={grant.verification_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 self-start break-all text-[0.8125rem] text-ink underline"
              >
                <ExternalLink aria-hidden="true" className="size-3.5 shrink-0" />
                {grant.verification_url}
              </a>
            </>
          ) : null}
          <p
            role="status"
            data-status={status}
            className={cx(
              "m-0 flex items-center gap-2 text-[0.8125rem]",
              LOGIN_TONE[status],
              status === "pending" && "border-t border-line pt-3",
            )}
          >
            {status === "pending" ? (
              <LoaderCircle aria-hidden="true" className="size-3.5 shrink-0 animate-spin" />
            ) : status === "approved" ? (
              <Check aria-hidden="true" className="size-4 shrink-0" />
            ) : failed ? (
              <CircleAlert aria-hidden="true" className="size-4 shrink-0" />
            ) : null}
            <span>{message || LOGIN_TEXT[status]}</span>
          </p>
          <div className="flex gap-2">
            {status === "pending" ? (
              <Button
                tone="danger"
                onClick={() => cancel.mutate(grant.session_id)}
                disabled={cancel.isPending}
              >
                Cancel
              </Button>
            ) : (
              <>
                {ended && status !== "approved" ? (
                  <Button tone="primary" onClick={() => start.mutate()} disabled={start.isPending}>
                    Try again
                  </Button>
                ) : null}
                <Button onClick={onDone}>Close</Button>
              </>
            )}
          </div>
        </>
      )}
      {problem ? (
        <>
          <Notice tone="danger">{problem}</Notice>
          {autoStart && !grant ? (
            <div className="flex gap-2">
              <Button tone="primary" onClick={() => start.mutate()} disabled={start.isPending}>
                Try again
              </Button>
              <Button onClick={onDone}>Close</Button>
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

type Period = (typeof RANGE_OPTIONS)[number]["value"];

/** A side task runs on its own model when it names a provider other than auto, or a model. */
export function isOverride(task: AuxiliaryTask): boolean {
  const provider = task.provider.trim();
  return (provider !== "" && provider !== "auto") || task.model.trim() !== "";
}

export function auxSummary(tasks: readonly AuxiliaryTask[]): string {
  if (tasks.length === 0) return "no side tasks";
  const overrides = tasks.filter(isOverride).length;
  if (overrides === 0) return `${tasks.length} task${tasks.length === 1 ? "" : "s"} · all auto`;
  return `${overrides} override${overrides === 1 ? "" : "s"} · ${tasks.length - overrides} auto`;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function slotLabel(value: unknown): string | null {
  const slot = record(value);
  const provider = typeof slot?.provider === "string" ? slot.provider.trim() : "";
  const model = typeof slot?.model === "string" ? slot.model.trim() : "";
  if (!model) return null;
  return provider ? `${provider}:${model}` : model;
}

/** The `moa` section in one line: the preset /moa uses (the default one, or the only one), its
 * turned-on reference models and its aggregator; the preset count when there are several. */
export function moaSummary(config: ConfigObject | undefined): string {
  const moa = record(config?.moa);
  const presets = Object.entries(record(moa?.presets) ?? {}).filter(
    (entry): entry is [string, Record<string, unknown>] => record(entry[1]) !== null,
  );
  if (presets.length === 0) return "no presets";
  const wanted = typeof moa?.default_preset === "string" ? moa.default_preset.trim() : "";
  const picked =
    presets.find(([name]) => name === wanted) ?? (presets.length === 1 ? presets[0] : undefined);
  const many = `${presets.length} presets`;
  if (!picked) return `${many} · no default`;
  const [name, preset] = picked;
  const refs = Array.isArray(preset.reference_models)
    ? preset.reference_models.filter((r) => slotLabel(r) !== null && record(r)?.enabled !== false)
    : [];
  const parts = [
    `${refs.length} ${refs.length === 1 ? "reference" : "references"}`,
    slotLabel(preset.aggregator) ?? "no aggregator",
  ];
  if (presets.length > 1) parts.unshift(name);
  return parts.join(" · ");
}

function routeText(task: AuxiliaryTask): string {
  if (!isOverride(task)) return "auto (main model)";
  const provider = task.provider.trim() || "auto";
  return `${provider} · ${task.model.trim() || "main model"}`;
}

/** One tinted row of the settings card: icon + label, a mono value, a button on the right. */
function SettingRow({
  icon: Icon,
  iconClass,
  label,
  value,
  valueTestId,
  action,
}: {
  icon: ComponentType<{ className?: string }>;
  iconClass: string;
  label: string;
  value: ReactNode;
  valueTestId?: string;
  action: ReactNode;
}) {
  return (
    <div className="flex items-center gap-3 border border-line/70 bg-surface px-3 py-2">
      <div className="min-w-0 flex-1">
        <p className="m-0 flex items-center gap-2 text-[0.75rem] font-medium tracking-[0.08em] text-ink uppercase">
          <Icon aria-hidden="true" className={cx("size-3 shrink-0", iconClass)} />
          {label}
        </p>
        <p
          {...(valueTestId ? { "data-testid": valueTestId } : {})}
          className="m-0 mt-0.5 truncate font-mono text-[0.75rem] text-ink-muted"
        >
          {value}
        </p>
      </div>
      {action}
    </div>
  );
}

function AuxRow({ task }: { task: AuxiliaryTask }) {
  const toast = useToast();
  const changed = useModelsChanged();
  const ids = useId();
  const [editing, setEditing] = useState(false);
  const [provider, setProvider] = useState(task.provider);
  const [model, setModel] = useState(task.model);
  const save = useMutation({
    mutationFn: () =>
      api.model.set({
        scope: "auxiliary",
        task: task.task,
        provider: provider.trim(),
        model: model.trim(),
      }),
    onSuccess: () => {
      toast.show(`${task.task} model saved.`, "success");
      setEditing(false);
      changed();
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  return (
    <li
      data-testid={`aux-${task.task}`}
      className="flex flex-col gap-3 border border-line px-3 py-2.5"
    >
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="m-0 text-[0.75rem] font-medium text-ink">
            {task.task}
            {task.description ? (
              <span className="ml-2 font-normal text-ink-faint">{task.description}</span>
            ) : null}
          </p>
          <p className="m-0 mt-0.5 truncate font-mono text-[0.75rem] text-ink-muted">
            {routeText(task)}
          </p>
        </div>
        <Button
          size="sm"
          className="uppercase"
          aria-expanded={editing}
          onClick={() => setEditing((was) => !was)}
        >
          Change
        </Button>
      </div>
      {editing ? (
        <div className="flex flex-wrap items-end gap-2">
          <Field label="Provider" htmlFor={`${ids}-provider`}>
            <input
              id={`${ids}-provider`}
              className={inputClass}
              value={provider}
              onChange={(e) => setProvider(e.currentTarget.value)}
            />
          </Field>
          <Field label="Model" htmlFor={`${ids}-model`}>
            <input
              id={`${ids}-model`}
              className={inputClass}
              placeholder="(the main model)"
              value={model}
              onChange={(e) => setModel(e.currentTarget.value)}
            />
          </Field>
          <Button tone="primary" onClick={() => save.mutate()} disabled={save.isPending}>
            Save
          </Button>
        </div>
      ) : null}
    </li>
  );
}

function AuxDialog({
  open,
  onOpenChange,
  tasks,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  tasks: readonly AuxiliaryTask[];
}) {
  const toast = useToast();
  const changed = useModelsChanged();
  const [confirming, setConfirming] = useState(false);
  const overridden = tasks.filter(isOverride);
  const reset = useMutation({
    mutationFn: async () => {
      for (const task of overridden) {
        await api.model.set({ scope: "auxiliary", task: task.task, provider: "auto", model: "" });
      }
    },
    onSuccess: () => toast.show("Every side task now follows the main model.", "success"),
    onError: (error) => toast.show(errorText(error), "error"),
    onSettled: () => {
      setConfirming(false);
      changed();
    },
  });
  return (
    <>
      <Modal
        open={open}
        onOpenChange={onOpenChange}
        size="lg"
        title="Auxiliary tasks"
        description="Smaller jobs such as summaries, titles and vision can run on their own model; auto uses the main one."
      >
        <div className="flex flex-col gap-3">
          <div className="flex justify-end">
            <Button
              size="sm"
              className="uppercase"
              disabled={overridden.length === 0 || reset.isPending}
              onClick={() => setConfirming(true)}
            >
              Reset all to auto
            </Button>
          </div>
          {tasks.length === 0 ? (
            <p className="m-0 py-6 text-center text-[0.8125rem] text-ink-soft">No side tasks.</p>
          ) : (
            <ul
              aria-label="Side tasks"
              className="m-0 flex max-h-[60vh] list-none flex-col gap-2 overflow-y-auto p-0"
            >
              {tasks.map((task) => (
                <AuxRow key={`${task.task}:${task.provider}:${task.model}`} task={task} />
              ))}
            </ul>
          )}
        </div>
      </Modal>
      <ConfirmDialog
        open={confirming}
        title="Reset side tasks"
        description={`Set ${overridden.length} overridden task${overridden.length === 1 ? "" : "s"} back to auto (the main model)?`}
        confirmLabel="Reset"
        busy={reset.isPending}
        onConfirm={() => reset.mutate()}
        onCancel={() => setConfirming(false)}
      />
    </>
  );
}

function ModelSettings() {
  const toast = useToast();
  const changed = useModelsChanged();
  const navigate = useNavigate();
  const info = useQuery({ queryKey: ["models", "info"], queryFn: () => api.model.info() });
  const aux = useQuery({ queryKey: ["models", "auxiliary"], queryFn: () => api.model.auxiliary() });
  const config = useQuery({ queryKey: ["config", "file"], queryFn: () => api.config.get() });
  const [picking, setPicking] = useState(false);
  const [configuring, setConfiguring] = useState(false);
  const save = useMutation({
    mutationFn: (choice: ModelChoice) => api.model.set({ scope: "main", ...choice }),
    onSuccess: () => {
      toast.show("Main model saved; new sessions use it.", "success");
      setPicking(false);
      changed();
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const data = info.data;
  const mainValue = data?.model
    ? [
        data.provider,
        data.model,
        data.effective_context_length
          ? `${formatTokens(data.effective_context_length).replace(/\.0(?=[KM])/, "")} ctx`
          : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : "(unset)";
  const tasks = aux.data?.tasks ?? [];
  return (
    <Card padded={false}>
      <CardHeader
        icon={SlidersHorizontal}
        title="Model settings"
        subtitle="applies to new sessions"
      />
      <div className="flex flex-col gap-3 px-4 pt-3 pb-4">
        {info.error ? (
          <ErrorState message={errorText(info.error)} onRetry={() => void info.refetch()} />
        ) : (
          <SettingRow
            icon={Star}
            iconClass="text-ink"
            label="Main model"
            value={info.isPending ? "…" : mainValue}
            valueTestId="current-model"
            action={
              <Button
                tone="primary"
                size="sm"
                className="uppercase"
                onClick={() => setPicking(true)}
              >
                Change
              </Button>
            }
          />
        )}
        {aux.error ? (
          <ErrorState message={errorText(aux.error)} onRetry={() => void aux.refetch()} />
        ) : (
          <SettingRow
            icon={Cpu}
            iconClass="text-ink-soft"
            label="Auxiliary tasks"
            value={aux.isPending ? "…" : auxSummary(tasks)}
            valueTestId="aux-summary"
            action={
              <Button size="sm" className="uppercase" onClick={() => setConfiguring(true)}>
                Configure
              </Button>
            }
          />
        )}
        {config.error ? (
          <ErrorState message={errorText(config.error)} onRetry={() => void config.refetch()} />
        ) : (
          <SettingRow
            icon={Layers}
            iconClass="text-ink-soft"
            label="Mixture of agents"
            value={config.isPending ? "…" : moaSummary(config.data)}
            valueTestId="moa-summary"
            action={
              <Button
                size="sm"
                className="uppercase"
                aria-label="Configure mixture of agents"
                onClick={() => void navigate("/config?section=moa")}
              >
                Configure
              </Button>
            }
          />
        )}
      </div>
      <ModelPickerDialog
        open={picking}
        onOpenChange={setPicking}
        busy={save.isPending}
        onApply={(choice) => save.mutate(choice)}
      />
      <AuxDialog open={configuring} onOpenChange={setConfiguring} tasks={tasks} />
    </Card>
  );
}

function UsageFigures({ days }: { days: Period }) {
  const flag = useTokenAnalyticsFlag();
  const usage = useQuery({
    queryKey: ["analytics", days],
    queryFn: () => api.analytics.usage(days),
  });
  const totals = usage.data?.totals;
  const items: StatItem[] = !totals
    ? []
    : flag.enabled
      ? [
          {
            label: "Total tokens",
            value: formatTokens(totals.input_tokens + totals.output_tokens),
          },
          { label: "Input", value: formatTokens(totals.input_tokens) },
          { label: "Output", value: formatTokens(totals.output_tokens) },
          { label: "Est. cost", value: formatCost(totals.cost) },
          { label: "Total sessions", value: formatCount(totals.sessions) },
        ]
      : [{ label: "Total sessions", value: formatCount(totals.sessions) }];
  return (
    <Card className="px-4 py-6">
      {usage.isPending ? (
        <PageSpinner />
      ) : usage.error ? (
        <ErrorState message={errorText(usage.error)} onRetry={() => void usage.refetch()} />
      ) : (
        <div className="flex flex-col gap-4">
          <LeaderStatList label="Model usage" items={items} />
          {flag.enabled ? null : (
            <p className="m-0 text-[0.75rem] leading-relaxed text-ink-faint">
              Token and cost figures are hidden. Turn on{" "}
              <code className="type-data text-[0.75rem]">dashboard.show_token_analytics</code> on
              the{" "}
              <Link to="/config" className="underline">
                Config
              </Link>{" "}
              page to show them.
            </p>
          )}
        </div>
      )}
    </Card>
  );
}

function ModelUsageCard({
  row,
  rank,
  main,
  tokens,
}: {
  row: ModelUsageRow;
  rank: number;
  main: boolean;
  tokens: boolean;
}) {
  return (
    <li
      data-testid={`model-usage-${row.model}`}
      className={cx("flex flex-col border border-line", main && "ring-1 ring-ink/40")}
    >
      <div className="flex items-start gap-3 px-4 pt-4 pb-3">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <span className="shrink-0 font-mono text-[0.75rem] text-ink-faint">#{rank}</span>
          <h3 className="m-0 truncate font-mono text-[0.8125rem] font-bold tracking-[0.12em] text-ink uppercase">
            {row.model}
          </h3>
          {main ? (
            <span className="inline-flex shrink-0 items-center gap-1 bg-ink/15 px-1.5 py-0.5 font-mono text-[0.6875rem] text-ink">
              <Star aria-hidden="true" className="size-2.5" />
              main
            </span>
          ) : null}
        </div>
        <div className="shrink-0 text-right">
          <p className="m-0 font-mono text-[0.8125rem] font-bold text-ink">
            {formatCount(row.api_calls)}
          </p>
          <p className="m-0 text-[0.75rem] text-ink-muted">
            {row.api_calls === 1 ? "API call" : "API calls"}
          </p>
        </div>
      </div>
      {tokens ? (
        <p className="m-0 mx-4 border-t border-line/60 py-2 text-right font-mono text-[0.75rem] text-ink-muted">
          {`${formatTokens(row.input_tokens)} in · ${formatTokens(row.output_tokens)} out · ${formatCost(row.cost)}`}
        </p>
      ) : null}
    </li>
  );
}

/** One card per model the period's calls used (`by_model`), most tokens first. */
function ModelUsageCards({ days }: { days: Period }) {
  const flag = useTokenAnalyticsFlag();
  const usage = useQuery({
    queryKey: ["analytics", days],
    queryFn: () => api.analytics.usage(days),
  });
  const info = useQuery({ queryKey: ["models", "info"], queryFn: () => api.model.info() });
  // The figures card above shows the loading and error states of the same request.
  if (!usage.data) return null;
  const rows = usage.data.by_model ?? [];
  if (rows.length === 0) {
    return (
      <Card padded={false}>
        <EmptyState icon={Cpu}>No model calls in this period.</EmptyState>
      </Card>
    );
  }
  const main = info.data?.model ?? null;
  return (
    <ul
      aria-label="Usage by model"
      className="m-0 grid list-none grid-cols-1 gap-4 p-0 md:grid-cols-2 xl:grid-cols-3"
    >
      {rows.map((row, at) => (
        <ModelUsageCard
          key={row.model}
          row={row}
          rank={at + 1}
          main={row.model === main}
          tokens={flag.enabled}
        />
      ))}
    </ul>
  );
}

function ProviderCard({
  provider,
  main,
  signingIn,
  onSignIn,
  onDone,
}: {
  provider: ProviderOption;
  main: boolean;
  signingIn: boolean;
  onSignIn: () => void;
  onDone: () => void;
}) {
  return (
    <li
      data-testid={`provider-${provider.name}`}
      className={cx("flex flex-col border border-line", main && "ring-1 ring-ink/40")}
    >
      <div className="flex items-start gap-3 border-b border-line px-4 pt-4 pb-3">
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <h3 className="m-0 truncate font-mono text-[0.8125rem] font-bold tracking-[0.12em] text-ink uppercase">
              {provider.label}
            </h3>
            {main ? (
              <span className="inline-flex shrink-0 items-center gap-1 bg-ink/15 px-1.5 py-0.5 font-mono text-[0.6875rem] text-ink">
                <Star aria-hidden="true" className="size-2.5" />
                main
              </span>
            ) : null}
          </div>
          <Badge className="mt-2 bg-selected">{provider.name}</Badge>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1.5">
          <span
            className={cx(
              "font-mono text-[0.75rem] font-bold",
              provider.configured ? "text-success" : "text-ink-soft",
            )}
          >
            {provider.configured ? "configured" : "no key"}
          </span>
          {provider.device_login && !signingIn ? (
            <Button size="sm" className="h-[1.375rem] uppercase" onClick={onSignIn}>
              Sign in
            </Button>
          ) : null}
        </div>
      </div>
      <div className="flex flex-1 flex-col gap-3 p-4">
        {provider.description ? (
          <p className="m-0 text-[0.75rem] text-ink-muted">{provider.description}</p>
        ) : null}
        {signingIn ? <DeviceLogin provider={provider} onDone={onDone} /> : null}
        <p className="m-0 mt-auto border-t border-line/60 pt-2 text-[0.75rem] text-ink-muted">
          {[provider.family, provider.auth_type].filter(Boolean).join(" · ")}
        </p>
      </div>
    </li>
  );
}

function Providers() {
  const [signingIn, setSigningIn] = useState<string | null>(null);
  const options = useQuery({ queryKey: ["models", "options"], queryFn: () => api.model.options() });
  const list = options.data?.providers ?? [];
  const mainProvider = options.data?.current.provider ?? null;
  if (options.isPending) return <PageSpinner />;
  if (options.error) {
    return <ErrorState message={errorText(options.error)} onRetry={() => void options.refetch()} />;
  }
  if (list.length === 0) {
    return (
      <Card padded={false}>
        <EmptyState icon={Cpu}>No providers are known to this install.</EmptyState>
      </Card>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <ul
        aria-label="Providers"
        className="m-0 grid list-none grid-cols-1 gap-4 p-0 md:grid-cols-2 xl:grid-cols-3"
      >
        {list.map((p) => (
          <ProviderCard
            key={p.name}
            provider={p}
            main={p.name === mainProvider}
            signingIn={signingIn === p.name}
            onSignIn={() => setSigningIn(p.name)}
            onDone={() => setSigningIn(null)}
          />
        ))}
      </ul>
      <p className="m-0 text-[0.75rem] text-ink-faint">
        Providers without a sign-in take an API key: add it on the Keys page or in the credential
        pool on the System page.
      </p>
    </div>
  );
}

export function ModelsPage() {
  const client = useQueryClient();
  const [days, setDays] = useState<Period>(30);
  const fetching =
    useIsFetching({ queryKey: ["models"] }) + useIsFetching({ queryKey: ["analytics", days] });
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["models"] });
    void client.invalidateQueries({ queryKey: ["analytics", days] });
    void client.invalidateQueries({ queryKey: ["config", "file"] });
  };
  return (
    <div className="flex flex-col gap-[1.4375rem] px-4 py-[1.4375rem] lg:px-[1.4375rem]">
      <HeaderAfterTitle>
        <PeriodButtons upper value={days} options={RANGE_OPTIONS} onChange={setDays} />
        <RefreshButton onClick={refresh} busy={fetching > 0} />
      </HeaderAfterTitle>
      <div className="grid grid-cols-1 gap-[1.4375rem] lg:grid-cols-2">
        <ModelSettings />
        <UsageFigures days={days} />
      </div>
      <ModelUsageCards days={days} />
      <Providers />
    </div>
  );
}
