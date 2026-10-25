// Telegram quick setup (P24-27), our own flow (no hosted bot-minting service):
//   1. "Create a bot" opens Telegram's BotFather in the system browser;
//   2. the person pastes the bot token (and the bot's @name BotFather showed) → saved for the
//      Telegram platform, the platform enabled, the gateway restarted;
//   3. the page shows the bot's link and asks the person to send it any message;
//   4. it polls the pairing list every 2 s: the first pending Telegram request carries the
//      person's numeric user id (shown, editable, digits only);
//   5. "Allow and finish" saves that id as the allowlist, approves the request and restarts the
//      gateway again.
// The wait gives up after 10 minutes. The token is sent once to the daemon (keychain/env through
// the REST route) and never kept in this component after the save.

import { useEffect, useRef, useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { type RestLike, rest } from "../../settings/settingsApi.ts";
import { PanelPill } from "../kit.tsx";

export const BOTFATHER_URL = "https://t.me/BotFather";
export const PAIRING_POLL_MS = 2_000;
export const SETUP_TIMEOUT_MS = 10 * 60_000;
const USER_ID_RE = /^\d{3,20}$/;
const BOT_NAME_RE = /^@?([A-Za-z][A-Za-z0-9_]{3,31}bot)$/i;

export interface TelegramPlatform {
  id: string;
  fields: ReadonlyArray<{ key: string; kind: string }>;
}

interface PairingPending {
  platform: string;
  request_id: string;
  user_id: string;
  user_name: string | null;
}

type Phase = "idle" | "token" | "saving" | "waiting" | "detected" | "applying" | "done";

export function tokenKey(platform: TelegramPlatform): string {
  return platform.fields.find((f) => f.kind === "credential")?.key ?? "TELEGRAM_BOT_TOKEN";
}

export function allowlistKey(platform: TelegramPlatform): string {
  return (
    platform.fields.find((f) => f.key.endsWith("ALLOWED_USERS"))?.key ?? "TELEGRAM_ALLOWED_USERS"
  );
}

export function TelegramQrSetup(props: {
  api: RestLike | null;
  platform: TelegramPlatform;
  profile: string | null;
  openExternal: (url: string) => void;
  /** The gateway was asked to restart (the page keeps its banner until a new instance). */
  onRestartAsked: () => void;
  onDone?: () => void;
  pollMs?: number;
  timeoutMs?: number;
}) {
  const t = useT();
  const [phase, setPhase] = useState<Phase>("idle");
  const [token, setToken] = useState("");
  const [botName, setBotName] = useState("");
  const [detected, setDetected] = useState<PairingPending | null>(null);
  const [userId, setUserId] = useState("");
  const [error, setError] = useState("");
  const started = useRef(0);
  const { api, platform, profile } = props;

  useEffect(() => {
    if (phase !== "waiting") return;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const poll = async () => {
      if (!live) return;
      if (Date.now() - started.current >= (props.timeoutMs ?? SETUP_TIMEOUT_MS)) {
        setPhase("token");
        setError(t("manage.telegram.timedOut"));
        return;
      }
      const answer = await rest<{ pending: PairingPending[] }>(api, "GET", "/api/pairing", {
        profile,
      });
      if (!live) return;
      const first = answer.ok
        ? answer.data.pending.find((p) => p.platform === platform.id)
        : undefined;
      if (first !== undefined) {
        setDetected(first);
        setUserId(USER_ID_RE.test(first.user_id) ? first.user_id : "");
        setPhase("detected");
        return;
      }
      setError(answer.ok ? "" : t("manage.telegram.stillWaiting"));
      timer = setTimeout(() => void poll(), props.pollMs ?? PAIRING_POLL_MS);
    };
    timer = setTimeout(() => void poll(), props.pollMs ?? PAIRING_POLL_MS);
    return () => {
      live = false;
      if (timer !== null) clearTimeout(timer);
    };
  }, [phase, api, platform.id, profile, props.pollMs, props.timeoutMs, t]);

  const restart = async () => {
    await rest(api, "POST", "/api/daemon/restart", { profile });
    props.onRestartAsked();
  };

  const saveToken = async () => {
    const clean = token.trim();
    if (clean === "") {
      setError(t("manage.telegram.needToken"));
      return;
    }
    if (botName.trim() !== "" && !BOT_NAME_RE.test(botName.trim())) {
      setError(t("manage.telegram.badBotName"));
      return;
    }
    setPhase("saving");
    setError("");
    const saved = await rest(
      api,
      "PUT",
      `/api/messaging/platforms/${encodeURIComponent(platform.id)}`,
      {
        profile,
        body: { enabled: true, env: { [tokenKey(platform)]: clean } },
      },
    );
    setToken(""); // the token is the daemon's now
    if (!saved.ok) {
      setPhase("token");
      setError(saved.message);
      return;
    }
    await restart();
    started.current = Date.now();
    setPhase("waiting");
  };

  const apply = async () => {
    if (!USER_ID_RE.test(userId.trim())) {
      setError(t("manage.telegram.numericOnly"));
      return;
    }
    setPhase("applying");
    setError("");
    const saved = await rest(
      api,
      "PUT",
      `/api/messaging/platforms/${encodeURIComponent(platform.id)}`,
      {
        profile,
        body: { env: { [allowlistKey(platform)]: userId.trim() } },
      },
    );
    if (!saved.ok) {
      setPhase("detected");
      setError(saved.message);
      return;
    }
    if (detected !== null && detected.user_id === userId.trim()) {
      await rest(api, "POST", "/api/pairing/approve", {
        profile,
        body: { platform: platform.id, request_id: detected.request_id },
      });
    }
    await restart();
    setPhase("done");
    props.onDone?.();
  };

  const link = BOT_NAME_RE.exec(botName.trim())?.[1];
  return (
    <div className="msg-quick" data-testid="telegram-setup">
      <div className="msg-quick-head">
        <h3>{t("manage.telegram.title")}</h3>
        <PanelPill tone="good">{t("manage.messaging.recommended")}</PanelPill>
      </div>
      <p className="msg-field-help">{t("manage.telegram.help")}</p>
      {error !== "" && (
        <p className="msg-error" role="alert">
          {error}
        </p>
      )}
      {phase === "idle" && (
        <div className="msg-quick-row">
          <button
            type="button"
            className="desk-btn"
            data-variant="primary"
            data-size="sm"
            onClick={() => {
              props.openExternal(BOTFATHER_URL);
              setPhase("token");
            }}
          >
            {t("manage.telegram.create")}
          </button>
          <button
            type="button"
            className="desk-btn"
            data-variant="secondary"
            data-size="sm"
            onClick={() => setPhase("token")}
          >
            {t("manage.telegram.haveToken")}
          </button>
        </div>
      )}
      {(phase === "token" || phase === "saving") && (
        <>
          <label className="msg-quick-field">
            {t("manage.telegram.token")}
            <input
              type="password"
              autoComplete="off"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </label>
          <label className="msg-quick-field">
            {t("manage.telegram.botName")}
            <input
              value={botName}
              onChange={(e) => setBotName(e.target.value)}
              placeholder="@my_helper_bot"
            />
          </label>
          <button
            type="button"
            className="desk-btn"
            data-variant="primary"
            data-size="sm"
            disabled={phase === "saving"}
            onClick={() => void saveToken()}
          >
            {t("manage.telegram.saveToken")}
          </button>
        </>
      )}
      {phase === "waiting" && (
        <div role="status">
          <p>{t("manage.telegram.sendMessage")}</p>
          {link !== undefined && <p className="msg-mono">{`https://t.me/${link}`}</p>}
        </div>
      )}
      {(phase === "detected" || phase === "applying") && (
        <>
          <p>
            {t("manage.telegram.detected", {
              name: detected?.user_name ?? detected?.user_id ?? "",
            })}
          </p>
          <label className="msg-quick-field">
            {t("manage.telegram.userId")}
            <input inputMode="numeric" value={userId} onChange={(e) => setUserId(e.target.value)} />
          </label>
          <button
            type="button"
            className="desk-btn"
            data-variant="primary"
            data-size="sm"
            disabled={phase === "applying"}
            onClick={() => void apply()}
          >
            {t("manage.telegram.apply")}
          </button>
        </>
      )}
      {phase === "done" && <p role="status">{t("manage.telegram.done")}</p>}
    </div>
  );
}
