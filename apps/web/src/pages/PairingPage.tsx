// Pairing (P18-23): who may talk to the agent over messaging platforms that ask strangers to pair.
// Pending requests can be approved (or all cleared); a person who received a code can be approved
// by that code; approved users can be revoked. Codes are never listed, only typed in.
// Look (W52-d): headed sections with counts, one outlined card per person, 80 px empty cards.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, KeyRound, ShieldCheck, Trash2, Users, X } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { api } from "../lib/api.ts";
import type { PairingApproved, PairingPending } from "../lib/restTypes.ts";
import { HeaderActions } from "../shell/PageHeader.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import {
  Badge,
  Button,
  EmptyCard,
  Field,
  IconButton,
  Input,
  PageSpinner,
  QueryState,
  SectionHeading,
} from "../ui/controls.tsx";
import { ConfirmDialog } from "../ui/Dialog.tsx";

function who(user: { user_id: string; user_name: string | null }): string {
  return user.user_name ? `${user.user_name} (${user.user_id})` : user.user_id;
}

export function ageText(seconds: number): string {
  if (seconds < 60) return "under a minute ago";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  return `${Math.floor(seconds / 3600)} h ago`;
}

const rowClass = "flex items-center gap-3 border border-line p-4";

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
    <section aria-labelledby={`${ids}-heading`} className="flex flex-col gap-3">
      <SectionHeading icon={KeyRound} id={`${ids}-heading`}>
        Approve by code
      </SectionHeading>
      <div className="flex flex-col gap-2 border border-line p-4">
        <p className="m-0 text-[0.75rem] text-ink-soft">
          Someone who messaged the bot got a short code; type it here to let them in.
        </p>
        <form className="flex flex-wrap items-end gap-3" onSubmit={submit} noValidate>
          <Field label="Platform" htmlFor={`${ids}-platform`}>
            <Input
              id={`${ids}-platform`}
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
            <Input
              id={`${ids}-code`}
              autoComplete="off"
              value={code}
              onChange={(e) => setCode(e.currentTarget.value)}
            />
          </Field>
          <Button
            type="submit"
            tone="primary"
            size="sm"
            className="mb-0.5 uppercase"
            icon={<Check />}
            disabled={approve.isPending}
          >
            Approve
          </Button>
        </form>
        {problem ? (
          <p role="alert" className="m-0 text-[0.75rem] text-danger">
            {problem}
          </p>
        ) : null}
      </div>
    </section>
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
    <li data-testid={`pending-${request.request_id}`} className={rowClass}>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <Badge>{request.platform}</Badge>
          <span className="truncate text-[0.875rem] font-medium text-ink">
            {request.user_name ?? request.user_id}
          </span>
        </div>
        <p className="m-0 mt-1 text-[0.75rem] text-ink-muted">
          {request.user_id} · {ageText(request.age_seconds)}
        </p>
      </div>
      <Button
        tone="primary"
        size="sm"
        className="uppercase"
        icon={<Check />}
        onClick={() => approve.mutate()}
        disabled={approve.isPending}
      >
        Approve
      </Button>
    </li>
  );
}

function ApprovedRow({ user, onRevoke }: { user: PairingApproved; onRevoke: () => void }) {
  return (
    <li data-testid={`approved-${user.platform}-${user.user_id}`} className={rowClass}>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <Badge>{user.platform}</Badge>
          <span className="truncate text-[0.875rem] font-medium text-ink">{user.user_id}</span>
        </div>
        {user.user_name ? (
          <p className="m-0 mt-1 text-[0.75rem] text-ink-muted">{user.user_name}</p>
        ) : null}
      </div>
      <IconButton label="Revoke" tone="danger" onClick={onRevoke}>
        <X />
      </IconButton>
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
    <div className="flex flex-col gap-6 px-4 py-[1.375rem] lg:pr-8 lg:pl-[1.4375rem]">
      <HeaderActions>
        <Button
          tone="primary"
          size="sm"
          className="uppercase"
          icon={<Trash2 />}
          // Always a live button, as on the reference; with nobody waiting it says so instead of
          // asking to clear nothing (W54-W1-8).
          onClick={() => {
            if (pending.length > 0) setClearing(true);
            else toast.show("There are no pending requests to clear.");
          }}
          disabled={pairing.isPending}
        >
          Clear pending
        </Button>
      </HeaderActions>
      {pairing.isPending ? (
        <PageSpinner />
      ) : (
        <QueryState loading={false} error={pairing.error}>
          <section aria-label="Pending requests" className="flex flex-col gap-3">
            <SectionHeading icon={Users}>Pending requests ({pending.length})</SectionHeading>
            {pending.length === 0 ? (
              <EmptyCard>Nobody is waiting to be paired.</EmptyCard>
            ) : (
              <ul className="m-0 flex list-none flex-col gap-3 p-0">
                {pending.map((request) => (
                  <PendingRow key={`${request.platform}-${request.request_id}`} request={request} />
                ))}
              </ul>
            )}
          </section>
          <section aria-label="Approved users" className="flex flex-col gap-3">
            <SectionHeading icon={ShieldCheck}>Approved users ({approved.length})</SectionHeading>
            {approved.length === 0 ? (
              <EmptyCard>No approved users yet.</EmptyCard>
            ) : (
              <ul className="m-0 flex list-none flex-col gap-3 p-0">
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
      )}
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
