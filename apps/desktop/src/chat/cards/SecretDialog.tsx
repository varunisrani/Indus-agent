// The two masked-input requests (P20-22). Both are dialogs over an UNBLURRED backdrop, so the
// conversation behind stays readable.
// - Sudo: shows in full, selectable, what the password is for; the password goes only into the
//   answer.
// - Secret: names the variable and the agent's prompt; the value goes only into the answer (the
//   backend stores it in the profile's secret store). Nothing is kept here or in any storage.

import type { SecretRequest, SudoRequest } from "@indus/protocol";
import { type FormEvent, useState } from "react";
import type { OpenServerRequest } from "../../gateway/serverRequests.ts";
import { useT } from "../../i18n/index.tsx";

function Backdrop(props: { label: string; kind: string; id: string; children: React.ReactNode }) {
  return (
    // No backdrop-filter: the transcript behind stays sharp (the reference blurs; we do not).
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/30" data-backdrop>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={props.label}
        data-card={props.kind}
        data-request-id={props.id}
        className="w-[min(32rem,calc(100vw-2rem))] rounded-lg border border-line bg-raised p-4 space-y-3 shadow-lg"
      >
        {props.children}
      </div>
    </div>
  );
}

export function SudoDialog(props: { open: OpenServerRequest }) {
  const t = useT();
  const request = props.open.request as SudoRequest;
  const [password, setPassword] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    props.open.answer({ kind: "sudo", password });
    setPassword("");
  };

  return (
    <Backdrop label={t("sudo.title")} kind="sudo" id={props.open.id}>
      <form onSubmit={submit} className="space-y-3">
        <header className="text-sm font-semibold">{t("sudo.title")}</header>
        <pre
          data-testid="sudo-reason"
          className="font-mono text-xs whitespace-pre-wrap break-all select-text bg-code rounded p-2"
        >
          {request.reason}
        </pre>
        <input
          type="password"
          autoComplete="off"
          aria-label={t("sudo.password")}
          className="w-full rounded border border-line bg-bg px-2 py-1"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          // biome-ignore lint/a11y/noAutofocus: the dialog exists to take this one value
          autoFocus
        />
        <div className="flex justify-end gap-2">
          <button
            type="button"
            className="rounded border border-line px-3 py-1 text-sm"
            onClick={() => props.open.answer({ kind: "sudo" })}
          >
            {t("sudo.cancel")}
          </button>
          <button type="submit" className="rounded bg-accent text-accent-fg px-3 py-1 text-sm">
            {t("sudo.submit")}
          </button>
        </div>
      </form>
    </Backdrop>
  );
}

export function SecretDialog(props: { open: OpenServerRequest }) {
  const t = useT();
  const request = props.open.request as SecretRequest;
  const [value, setValue] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    props.open.answer({ kind: "secret", value });
    setValue("");
  };

  return (
    <Backdrop label={t("secret.title", { name: request.name })} kind="secret" id={props.open.id}>
      <form onSubmit={submit} className="space-y-3">
        <header className="text-sm font-semibold">
          {t("secret.title", { name: request.name })}
        </header>
        <p className="text-sm text-muted select-text">{request.prompt}</p>
        <input
          type="password"
          autoComplete="off"
          aria-label={t("secret.value", { name: request.name })}
          className="w-full rounded border border-line bg-bg px-2 py-1 font-mono"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          // biome-ignore lint/a11y/noAutofocus: the dialog exists to take this one value
          autoFocus
        />
        <p className="text-xs text-muted">{t("secret.where")}</p>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            className="rounded border border-line px-3 py-1 text-sm"
            onClick={() => props.open.answer({ kind: "secret" })}
          >
            {t("secret.skip")}
          </button>
          <button
            type="submit"
            disabled={value === ""}
            className="rounded bg-accent text-accent-fg px-3 py-1 text-sm"
          >
            {t("secret.save")}
          </button>
        </div>
      </form>
    </Backdrop>
  );
}
