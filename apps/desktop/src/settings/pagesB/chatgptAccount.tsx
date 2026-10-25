// Settings › Providers › Accounts: the ChatGPT sign-in card (W61-M2, D-16.1282; provider
// `openai-codex`). It reads the signed-in accounts from the backend, signs in with the device flow
// inside the card (a code to approve on OpenAI's page, the session polled at the backend's
// interval) and signs out after asking. Everything goes through main's REST proxy; the renderer
// only ever sees the user code, the page link, statuses and account metadata — never a token.

import { Check, CircleAlert, Copy, ExternalLink, LoaderCircle, LogOut } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { desktopBridge } from "../../app/bridge.ts";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import { attempt, useSettings } from "../context.tsx";
import { OutlineButton, Pill, PrimaryButton, TextButton } from "../primitives.tsx";
import { rest } from "../settingsApi.ts";
import { Note, relativeTime } from "./kit.tsx";

export const CHATGPT_PROVIDER = "openai-codex";

/** Never poll faster than this, whatever the backend says. */
export const MIN_POLL_S = 1;
/** Polls that fail on the wire in a row before the card gives up (the backend keeps the session). */
export const MAX_POLL_FAILURES = 10;

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

const realTimers: Timers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

interface Account {
  readonly id: string;
  readonly label: string;
  readonly source: string;
  readonly status: string;
  readonly expires_at: string | null;
  readonly expired: boolean;
  readonly plan: string | null;
}

interface SignIns {
  readonly provider: string;
  readonly label: string;
  readonly signed_in: boolean;
  readonly accounts: readonly Account[];
}

interface Started {
  readonly session_id: string;
  readonly user_code: string;
  readonly verification_url: string;
  readonly interval?: number;
}

type Phase =
  | { readonly kind: "idle" }
  | { readonly kind: "starting" }
  | {
      readonly kind: "waiting";
      readonly sessionId: string;
      readonly code: string;
      readonly url: string;
      readonly intervalS: number;
    }
  | { readonly kind: "ended"; readonly status: string; readonly message: string }
  | { readonly kind: "confirm" }
  | { readonly kind: "signing-out" };

const END_TEXT: Readonly<Record<string, MessageKey>> = {
  approved: "settings.chatgpt.approved",
  denied: "settings.chatgpt.denied",
  expired: "settings.chatgpt.expired",
  failed: "settings.chatgpt.failed",
  cancelled: "settings.chatgpt.cancelled",
};

const ACCOUNTS_PATH = `/api/providers/login/accounts/${CHATGPT_PROVIDER}`;

function planName(plan: string): string {
  return plan.charAt(0).toUpperCase() + plan.slice(1);
}

export function ChatGptAccount(props: {
  timers?: Timers;
  copy?: (text: string) => Promise<void>;
  openExternal?: (url: string) => void;
  /** Called after a sign-in or sign-out so the page can re-read its provider list. */
  onChanged?: () => void;
}) {
  const t = useT();
  const { api, profile, bridge } = useSettings();
  const timers = props.timers ?? realTimers;
  const [signIns, setSignIns] = useState<SignIns | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [problem, setProblem] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    const answer = await rest<SignIns>(api, "GET", ACCOUNTS_PATH, { profile });
    if (!live.current) return;
    if (answer.ok) {
      setSignIns(answer.data);
      setLoadError(null);
    } else setLoadError(answer.message);
  }, [api, profile]);
  useEffect(() => {
    void load();
  }, [load]);

  const start = async () => {
    setProblem(null);
    setCopied(false);
    setPhase({ kind: "starting" });
    const answer = await rest<Started>(api, "POST", "/api/providers/login/start", {
      body: { provider: CHATGPT_PROVIDER },
      profile,
    });
    if (!live.current) return;
    if (answer.ok) {
      setPhase({
        kind: "waiting",
        sessionId: answer.data.session_id,
        code: answer.data.user_code,
        url: answer.data.verification_url,
        intervalS: Math.max(MIN_POLL_S, answer.data.interval ?? 5),
      });
    } else {
      setPhase({ kind: "idle" });
      setProblem(answer.message || t("settings.chatgpt.failed"));
    }
  };

  // Poll the sign-in session at the backend's pace; leaving it unfinished cancels it.
  const pollingId = phase.kind === "waiting" ? phase.sessionId : null;
  const pollingInterval = phase.kind === "waiting" ? phase.intervalS : MIN_POLL_S;
  // biome-ignore lint/correctness/useExhaustiveDependencies: one poll loop per session
  useEffect(() => {
    if (pollingId === null) return;
    const path = `/api/providers/login/${encodeURIComponent(pollingId)}`;
    let handle: unknown = null;
    let finished = false;
    let failures = 0;
    const tick = () => {
      handle = timers.setTimeout(async () => {
        const answer = await rest<{ status: string; message: string }>(api, "GET", path, {
          profile,
        });
        if (finished || !live.current) return;
        if (answer.ok && answer.data.status === "pending") {
          failures = 0;
          tick();
          return;
        }
        // A failed poll is not an answer; only a run of them (or a gone session) ends the card.
        failures = answer.ok ? 0 : failures + 1;
        if (!answer.ok && answer.status !== 404 && failures < MAX_POLL_FAILURES) {
          tick();
          return;
        }
        finished = true;
        const status = answer.ok ? answer.data.status : "failed";
        setPhase({ kind: "ended", status, message: answer.ok ? answer.data.message : "" });
        if (status === "approved") {
          void load();
          props.onChanged?.();
        }
      }, pollingInterval * 1000);
    };
    tick();
    return () => {
      if (handle !== null) timers.clearTimeout(handle);
      if (!finished) {
        finished = true;
        void rest(api, "DELETE", path, { profile });
      }
    };
  }, [pollingId, pollingInterval, api, profile, timers]);

  const signOut = async () => {
    setProblem(null);
    setPhase({ kind: "signing-out" });
    const answer = await rest(api, "DELETE", ACCOUNTS_PATH, { profile });
    if (!live.current) return;
    setPhase({ kind: "idle" });
    if (!answer.ok) {
      setProblem(answer.message || t("settings.chatgpt.signOutFailed"));
      return;
    }
    await load();
    props.onChanged?.();
  };

  const copy = async (code: string) => {
    try {
      await (props.copy ?? ((text) => navigator.clipboard.writeText(text)))(code);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  const open = (url: string) => {
    if (props.openExternal) props.openExternal(url);
    else {
      const external = bridge?.external ?? desktopBridge()?.external;
      if (external) void attempt(() => external.open({ url }));
    }
  };

  const signedIn = signIns?.signed_in === true && signIns.accounts.length > 0;
  const now = Date.now();
  return (
    <section
      className="sb-signin"
      data-ui="settings-chatgpt-card"
      data-signed-in={signedIn ? "true" : "false"}
      aria-label={t("settings.chatgpt.title")}
    >
      <div className="sb-signin-head">
        <span className="sb-signin-text">
          <span className="sb-choice-title">
            {t("settings.chatgpt.title")}
            {signedIn && <Pill tone="accent">{t("settings.accounts.signedIn")}</Pill>}
          </span>
          <span className="sb-choice-body">{t("settings.chatgpt.body")}</span>
        </span>
        {!signedIn && (phase.kind === "idle" || phase.kind === "starting") && (
          <PrimaryButton
            disabled={phase.kind === "starting" || signIns === null}
            onClick={() => void start()}
          >
            {phase.kind === "starting"
              ? t("settings.chatgpt.starting")
              : t("settings.chatgpt.signIn")}
          </PrimaryButton>
        )}
      </div>

      {signedIn && (
        <ul className="sb-signin-accounts">
          {signIns.accounts.map((account) => {
            const when = relativeTime(t, account.expires_at, now);
            const until =
              account.expires_at === null
                ? null
                : account.expired
                  ? t("settings.chatgpt.expiredAt", { when: when ?? "" })
                  : t("settings.chatgpt.expiresAt", {
                      when: futureTime(t, account.expires_at, now) ?? "",
                    });
            return (
              <li key={account.id} data-account={account.id} className="sb-signin-account">
                <span className="sb-signin-label">{account.label}</span>
                {account.plan !== null && <Pill>{planName(account.plan)}</Pill>}
                {account.expired && <Pill tone="danger">{t("settings.chatgpt.expiredPill")}</Pill>}
                {until !== null && <span className="sb-signin-muted">{until}</span>}
              </li>
            );
          })}
        </ul>
      )}

      {signedIn && phase.kind === "idle" && (
        <div className="sb-signin-actions">
          <OutlineButton
            icon={<LogOut aria-hidden="true" size={13} />}
            onClick={() => setPhase({ kind: "confirm" })}
          >
            {t("settings.chatgpt.signOut")}
          </OutlineButton>
        </div>
      )}

      {(phase.kind === "confirm" || phase.kind === "signing-out") && (
        <div
          className="sb-signin-confirm"
          role="alertdialog"
          aria-label={t("settings.chatgpt.confirmTitle")}
        >
          <p className="sb-signin-muted">{t("settings.chatgpt.confirmBody")}</p>
          <div className="sb-signin-actions">
            <TextButton
              onClick={() => setPhase({ kind: "idle" })}
              disabled={phase.kind === "signing-out"}
            >
              {t("settings.chatgpt.keep")}
            </TextButton>
            <PrimaryButton onClick={() => void signOut()} disabled={phase.kind === "signing-out"}>
              {t("settings.chatgpt.signOutConfirm")}
            </PrimaryButton>
          </div>
        </div>
      )}

      {phase.kind === "waiting" && (
        <div className="sb-signin-flow">
          <p className="sb-signin-muted">{t("settings.chatgpt.instructions")}</p>
          <div className="sb-signin-codebox">
            <code className="sb-signin-code" title={t("settings.chatgpt.code")}>
              {phase.code}
            </code>
            <OutlineButton
              icon={<Copy aria-hidden="true" size={13} />}
              onClick={() => void copy(phase.code)}
            >
              {copied ? t("settings.chatgpt.copied") : t("settings.chatgpt.copy")}
            </OutlineButton>
          </div>
          <div className="sb-signin-actions">
            <TextButton
              onClick={() => setPhase({ kind: "ended", status: "cancelled", message: "" })}
            >
              {t("settings.chatgpt.cancel")}
            </TextButton>
            <PrimaryButton
              icon={<ExternalLink aria-hidden="true" size={13} />}
              onClick={() => open(phase.url)}
            >
              {t("settings.chatgpt.open")}
            </PrimaryButton>
          </div>
          <p className="sb-signin-status" role="status" data-status="pending">
            <LoaderCircle aria-hidden="true" size={12} className="sb-signin-spin" />
            <span>{t("settings.chatgpt.waiting")}</span>
          </p>
        </div>
      )}

      {phase.kind === "ended" && (
        <div className="sb-signin-flow">
          <p
            className="sb-signin-status"
            role="status"
            data-status={phase.status}
            data-tone={
              phase.status === "approved" ? "ok" : phase.status === "cancelled" ? "quiet" : "error"
            }
          >
            {phase.status === "approved" ? (
              <Check aria-hidden="true" size={13} />
            ) : phase.status === "cancelled" ? null : (
              <CircleAlert aria-hidden="true" size={13} />
            )}
            <span>{phase.message || t(END_TEXT[phase.status] ?? "settings.chatgpt.failed")}</span>
          </p>
          <div className="sb-signin-actions">
            <TextButton onClick={() => setPhase({ kind: "idle" })}>
              {t("settings.chatgpt.close")}
            </TextButton>
            {phase.status !== "approved" && (
              <PrimaryButton onClick={() => void start()}>
                {t("settings.chatgpt.retry")}
              </PrimaryButton>
            )}
          </div>
        </div>
      )}

      <Note text={problem} error />
      <Note text={loadError} error />
    </section>
  );
}

/** "in 9 days" style text for a time still ahead (null when unreadable). */
function futureTime(t: ReturnType<typeof useT>, when: string, now: number): string | null {
  const then = Date.parse(when);
  if (Number.isNaN(then)) return null;
  const minutes = Math.max(0, Math.round((then - now) / 60_000));
  if (minutes < 60) return t("settings.chatgpt.inMinutes", { count: minutes });
  const hours = Math.round(minutes / 60);
  if (hours < 48) return t("settings.chatgpt.inHours", { count: hours });
  return t("settings.chatgpt.inDays", { count: Math.round(hours / 24) });
}
