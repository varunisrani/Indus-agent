// Pairing (P18-23): who may talk to the agent over messaging platforms that ask strangers to pair.
// Pending requests can be approved (or all cleared); a person who received a code can be approved
// by that code; approved users can be revoked. Codes are never listed, only typed in.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { api } from "../lib/api.ts";
import type { PairingApproved, PairingPending } from "../lib/restTypes.ts";
import { HeaderActions } from "../shell/PageHeader.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { Button, Card, Field, inputClass, Notice, QueryState } from "../ui/controls.tsx";
import { ConfirmDialog } from "../ui/Dialog.tsx";

function who(user: { user_id: string; user_name: string | null }): string {
  return user.user_name ? `${user.user_name} (${user.user_id})` : user.user_id;
}

export function ageText(seconds: number): string {
  if (seconds < 60) return "under a minute ago";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  return `${Math.floor(seconds / 3600)} h ago`;
}

function ApproveByCode({ platforms }: { platforms: string[] }) {
  const toast = useToast();
  const client = useQueryClient();
  const ids = useId();
  const [platform, setPlatform] = useState("");
  const [code, setCode] = useState("");
  const [problem, setProblem] = useState("");
  const approve = useMutation({
    mutationFn: () => api.pairing.approve({ platform: platform.trim(), code: code.trim() }),
    onSuccess: (reply) => {
      setCode("");
      setProblem("");
      toast.show(`${who(reply.user)} can now talk to the agent.`, "success");
      void client.invalidateQueries({ queryKey: ["pairing"] });
    },
    onError: (error) => setProblem(errorText(error)),
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!platform.trim() || !code.trim()) {
      setProblem("Give the platform and the code the person received.");
      return;
    }
    approve.mutate();
  };
  return (
    <Card>
      <h2 className="mb-3 text-sm font-semibold">Approve by code</h2>
      <form className="flex flex-wrap items-end gap-3" onSubmit={submit} noValidate>
        <Field label="Platform" htmlFor={`${ids}-platform`}>
          <input
            id={`${ids}-platform`}
            className={inputClass}
            list={`${ids}-platforms`}
            value={platform}
            onChange={(e) => setPlatform(e.currentTarget.value)}
          />
        </Field>
        <datalist id={`${ids}-platforms`}>
          {platforms.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
        <Field label="Pairing code" htmlFor={`${ids}-code`}>
          <input
            id={`${ids}-code`}
            className={`${inputClass} font-mono`}
            autoComplete="off"
            value={code}
            onChange={(e) => setCode(e.currentTarget.value)}
          />
        </Field>
        <Button type="submit" tone="primary" disabled={approve.isPending}>
          Approve
        </Button>
      </form>
      {problem ? (
        <div className="mt-2">
          <Notice tone="danger">{problem}</Notice>
        </div>
      ) : null}
    </Card>
  );
}

function PendingRow({ request }: { request: PairingPending }) {
  const toast = useToast();
  const client = useQueryClient();
  const approve = useMutation({
    mutationFn: () =>
      api.pairing.approve({ platform: request.platform, request_id: request.request_id }),
    onSuccess: () => {
      toast.show(`${who(request)} approved.`, "success");
      void client.invalidateQueries({ queryKey: ["pairing"] });
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  return (
    <li
      data-testid={`pending-${request.request_id}`}
      className="flex flex-wrap items-center gap-2 rounded-md border border-line bg-raised p-2 text-sm"
    >
      <span className="flex-1">
        <strong>{who(request)}</strong> on {request.platform} · {ageText(request.age_seconds)}
      </span>
      <Button tone="primary" onClick={() => approve.mutate()} disabled={approve.isPending}>
        Approve
      </Button>
    </li>
  );
}

function ApprovedRow({ user, onRevoke }: { user: PairingApproved; onRevoke: () => void }) {
  return (
    <li
      data-testid={`approved-${user.platform}-${user.user_id}`}
      className="flex flex-wrap items-center gap-2 rounded-md border border-line bg-raised p-2 text-sm"
    >
      <span className="flex-1">
        <strong>{who(user)}</strong> on {user.platform}
      </span>
      <Button tone="danger" onClick={onRevoke}>
        Revoke
      </Button>
    </li>
  );
}

export function PairingPage() {
  const toast = useToast();
  const client = useQueryClient();
  const [revoking, setRevoking] = useState<PairingApproved | null>(null);
  const [clearing, setClearing] = useState(false);
  const pairing = useQuery({ queryKey: ["pairing"], queryFn: () => api.pairing.list() });
  const platforms = useQuery({
    queryKey: ["messaging", "platforms"],
    queryFn: () => api.messaging.platforms(),
  });
  const revoke = useMutation({
    mutationFn: (user: PairingApproved) => api.pairing.revoke(user.platform, user.user_id),
    onSuccess: (_reply, user) => {
      setRevoking(null);
      toast.show(`${who(user)} can no longer talk to the agent.`, "success");
      void client.invalidateQueries({ queryKey: ["pairing"] });
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const clear = useMutation({
    mutationFn: () => api.pairing.clearPending(),
    onSuccess: (reply) => {
      setClearing(false);
      toast.show(`${reply.cleared} pending request(s) cleared.`, "success");
      void client.invalidateQueries({ queryKey: ["pairing"] });
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });

  const pending = pairing.data?.pending ?? [];
  const approved = pairing.data?.approved ?? [];
  const known = new Set<string>([
    ...(platforms.data?.platforms ?? []).map((p) => p.id),
    ...pending.map((p) => p.platform),
  ]);
  return (
    <div className="flex flex-col gap-4 p-4">
      <HeaderActions>
        <Button onClick={() => setClearing(true)} disabled={pending.length === 0}>
          Clear pending
        </Button>
      </HeaderActions>
      <QueryState loading={pairing.isPending} error={pairing.error}>
        <section aria-label="Pending requests" className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold">Pending requests ({pending.length})</h2>
          {pending.length === 0 ? (
            <p className="text-sm text-muted">Nobody is waiting to be paired.</p>
          ) : (
            <ul className="flex flex-col gap-1">
              {pending.map((request) => (
                <PendingRow key={`${request.platform}-${request.request_id}`} request={request} />
              ))}
            </ul>
          )}
        </section>
        <section aria-label="Approved users" className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold">Approved users ({approved.length})</h2>
          {approved.length === 0 ? (
            <p className="text-sm text-muted">No approved users yet.</p>
          ) : (
            <ul className="flex flex-col gap-1">
              {approved.map((user) => (
                <ApprovedRow
                  key={`${user.platform}-${user.user_id}`}
                  user={user}
                  onRevoke={() => setRevoking(user)}
                />
              ))}
            </ul>
          )}
        </section>
      </QueryState>
      <ApproveByCode platforms={[...known].sort()} />
      <ConfirmDialog
        open={revoking !== null}
        title={`Revoke ${revoking ? who(revoking) : ""}?`}
        description="They can no longer talk to the agent on this platform until they pair again."
        confirmLabel="Revoke"
        danger
        busy={revoke.isPending}
        onConfirm={() => {
          if (revoking) revoke.mutate(revoking);
        }}
        onCancel={() => setRevoking(null)}
      />
      <ConfirmDialog
        open={clearing}
        title="Clear all pending requests?"
        description="Everyone waiting has to ask again for a new code."
        confirmLabel="Clear"
        danger
        busy={clear.isPending}
        onConfirm={() => clear.mutate()}
        onCancel={() => setClearing(false)}
      />
    </div>
  );
}
