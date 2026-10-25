// Settings › Gateways (W52-j): This window (connection mode cards, the remote form, the keychain
// switch, diagnostics) and Remote updates (an empty state: a remote is updated where it runs).

import { Check, FileText, Globe, Monitor, SquareTerminal } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import type { IndusRemoteTestResult, IndusSecretsPolicyView } from "../../global.d.ts";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import { useSwitcher } from "../../profiles/switcherReact.tsx";
import { attempt, useSettings } from "../context.tsx";
import {
  OutlineButton,
  PageBody,
  PrimaryButton,
  SettingRow,
  SettingsEmpty,
  Switch,
  TextButton,
} from "../primitives.tsx";
import { Note, notAvailable, SecretInput } from "./kit.tsx";

const LEG_KEYS: Readonly<Record<IndusRemoteTestResult["http"], MessageKey>> = {
  ok: "settings.remote.leg.ok",
  unauthorized: "settings.remote.leg.unauthorized",
  unreachable: "settings.remote.leg.unreachable",
  error: "settings.remote.leg.error",
};

type Mode = "local" | "remote";

function ModeCard(props: {
  icon: ReactNode;
  title: string;
  body: string;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    // biome-ignore lint/a11y/useSemanticElements: a card-sized radio, not a native radio
    <button
      type="button"
      role="radio"
      aria-checked={props.selected}
      aria-label={props.title}
      className="sb-mode"
      data-selected={props.selected ? "true" : undefined}
      onClick={props.onClick}
    >
      <span className="sb-mode-title">
        {props.icon}
        {props.title}
      </span>
      <span className="sb-mode-body">{props.body}</span>
      {props.selected && <Check aria-hidden="true" size={14} className="sb-mode-check" />}
    </button>
  );
}

/** The switch shows what this run really uses (`effective`), not the stored wish (`store`): with
 * no usable keychain the secrets sit in private files, so the switch reads off and cannot be
 * turned on, and the reason is a quiet line, not an error (W54-D2-2). */
export function storageSwitchState(policy: IndusSecretsPolicyView | null): {
  checked: boolean;
  disabled: boolean;
} {
  if (policy === null) return { checked: false, disabled: true };
  return { checked: policy.effective === "keychain", disabled: !policy.keychainUsable };
}

function StorageRow() {
  const t = useT();
  const { bridge, focusRow } = useSettings();
  const secrets = bridge?.secrets;
  const [policy, setPolicy] = useState<IndusSecretsPolicyView | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void attempt(secrets ? () => secrets.getPolicy() : null).then((answer) => {
      if (answer.ok) setPolicy(answer.value);
      else setError(answer.code);
    });
  }, [secrets]);
  const state = storageSwitchState(policy);
  return (
    <SettingRow
      rowId="secure-storage"
      highlighted={focusRow === "secure-storage"}
      title={t("settings.storage.title")}
      description={
        <>
          {t("settings.storage.body")}
          {policy !== null && !policy.keychainUsable && (
            <span className="sb-status" data-testid="keychain-unusable">
              {t("settings.storage.unusable", { reason: policy.reason })}
            </span>
          )}
        </>
      }
      control={
        <>
          <Switch
            label={t("settings.storage.title")}
            checked={state.checked}
            disabled={state.disabled}
            onChange={async (checked) => {
              const answer = await attempt(
                secrets ? () => secrets.setPolicy({ store: checked ? "keychain" : "file" }) : null,
              );
              if (answer.ok) setPolicy(answer.value);
              else setError(answer.code);
            }}
          />
          <Note text={notAvailable(t, error)} error />
        </>
      }
    />
  );
}

export function ThisWindowPage() {
  const t = useT();
  const { bridge, go, focusRow } = useSettings();
  const connection = bridge?.connection;
  const switcher = useSwitcher();
  /** A saved or forgotten remote changes the connection: a soft re-home (P20-26, R08.6). */
  const rehome = async <T,>(change: () => Promise<T>): Promise<T> => {
    if (switcher === null) return change();
    let result: { value: T } | null = null;
    const outcome = await switcher.applyConnectionChange(async () => {
      result = { value: await change() };
    });
    if (result === null) throw new Error(outcome.ok ? "no answer" : outcome.error);
    return (result as { value: T }).value;
  };
  const [mode, setMode] = useState<Mode | null>(null);
  const [chosen, setChosen] = useState<Mode | null>(null);
  const [saved, setSaved] = useState<{ url: string | null; hasToken: boolean } | null>(null);
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [result, setResult] = useState<IndusRemoteTestResult | null>(null);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const load = useCallback(() => {
    void attempt(connection ? () => connection.get() : null).then((answer) => {
      if (answer.ok) setMode(answer.value.mode === "remote" ? "remote" : "local");
    });
    void attempt(connection ? () => connection.getRemote() : null).then((answer) => {
      if (answer.ok) {
        setSaved(answer.value);
        setUrl(answer.value.url ?? "");
      } else setNote({ text: notAvailable(t, answer.code) ?? "", error: true });
    });
  }, [connection, t]);
  useEffect(load, [load]);
  const selected = chosen ?? mode ?? "local";
  const legText = (leg: IndusRemoteTestResult["http"]) => t(LEG_KEYS[leg]);
  const logs = bridge?.logs;

  const saveRemote = async () => {
    const answer = await attempt(
      connection ? () => rehome(() => connection.saveRemote({ url: url.trim(), token })) : null,
    );
    if (!answer.ok) {
      setNote({ text: answer.message, error: true });
      return;
    }
    setResult(answer.value);
    if (answer.value.saved) {
      setToken(""); // the token leaves the renderer
      setNote({ text: t("settings.remote.saved"), error: false });
      setChosen(null);
      load();
    } else setNote({ text: answer.value.message ?? t("settings.remote.notSaved"), error: true });
  };
  const forgetRemote = async () => {
    const answer = await attempt(connection ? () => rehome(() => connection.forgetRemote()) : null);
    if (answer.ok) {
      setNote({ text: t("settings.remote.forgotten"), error: false });
      setUrl("");
      setChosen(null);
      load();
    } else setNote({ text: answer.message, error: true });
  };
  const canSave =
    selected === "remote"
      ? url.trim() !== "" && token !== ""
      : saved?.url !== null && saved !== null;

  return (
    <PageBody>
      <div
        data-row="connection-mode"
        data-highlighted={focusRow === "connection-mode" ? "true" : undefined}
      >
        <p className="sb-caption">{t("settings.gateway2.mode")}</p>
        <div className="sb-modes" role="radiogroup" aria-label={t("settings.gateway2.mode")}>
          <ModeCard
            icon={<Monitor aria-hidden="true" size={14} />}
            title={t("settings.gateway2.local")}
            body={t("settings.gateway2.localBody")}
            selected={selected === "local"}
            onClick={() => setChosen("local")}
          />
          <ModeCard
            icon={<Globe aria-hidden="true" size={14} />}
            title={t("settings.gateway2.remote")}
            body={t("settings.gateway2.remoteBody")}
            selected={selected === "remote"}
            onClick={() => setChosen("remote")}
          />
          <ModeCard
            icon={<SquareTerminal aria-hidden="true" size={14} />}
            title={t("settings.gateway2.ssh")}
            body={t("settings.gateway2.sshBody")}
            selected={false}
            onClick={() => go?.({ view: "gateways", page: "connections" })}
          />
        </div>
        {selected === "remote" && (
          <div className="sb-remote">
            <input
              className="settings-input sb-full"
              aria-label={t("settings.remote.url")}
              placeholder="https://indus.example.com"
              value={url}
              spellCheck={false}
              autoComplete="off"
              onChange={(event) => setUrl(event.currentTarget.value)}
            />
            <SecretInput
              label={t("settings.remote.token")}
              placeholder={t(
                saved?.hasToken ? "settings.remote.tokenStored" : "settings.remote.token",
              )}
              value={token}
              onChange={setToken}
            />
            <span className="settings-inline">
              <OutlineButton
                disabled={url.trim() === "" || token === ""}
                onClick={async () => {
                  const answer = await attempt(
                    connection ? () => connection.testRemote({ url: url.trim(), token }) : null,
                  );
                  if (answer.ok) setResult(answer.value);
                  else setNote({ text: answer.message, error: true });
                }}
              >
                {t("settings.remote.test")}
              </OutlineButton>
              {result !== null && (
                <span className="settings-row-note" role="status">
                  {t("settings.remote.legs", {
                    http: legText(result.http),
                    ws: legText(result.ws),
                  })}
                </span>
              )}
            </span>
          </div>
        )}
        {selected === "local" && mode === "remote" && (
          <p className="settings-row-note">{t("settings.gateway2.forgetNote")}</p>
        )}
        <div className="sb-form-actions sb-end">
          <PrimaryButton
            disabled={!canSave || (selected === mode && selected === "local")}
            onClick={() => void (selected === "remote" ? saveRemote() : forgetRemote())}
          >
            {t("settings.gateway2.saveReconnect")}
          </PrimaryButton>
        </div>
        <Note text={note?.text ?? null} error={note?.error ?? false} />
      </div>
      <StorageRow />
      <SettingRow
        rowId="gateway-logs"
        highlighted={focusRow === "gateway-logs"}
        title={t("settings.gatewayDiag.title")}
        description={t("settings.gatewayDiag.body")}
        control={
          <TextButton
            icon={<FileText aria-hidden="true" size={13} />}
            disabled={logs === undefined}
            onClick={() => void attempt(logs ? () => logs.reveal() : null)}
          >
            {t("settings.gatewayDiag.open")}
          </TextButton>
        }
      />
    </PageBody>
  );
}

export function RemoteUpdatesPage() {
  const t = useT();
  return (
    <PageBody>
      <SettingsEmpty
        title={t("settings.remoteUpdates.title")}
        body={t("settings.remoteUpdates.body")}
      />
    </PageBody>
  );
}
