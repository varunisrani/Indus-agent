// A provider's signed-in accounts on the Keys page's OAuth card (W61-M2, D-16.1282): who is signed
// in (label, plan, when the access runs out) and Sign out, which asks first and then deletes every
// sign-in of the provider on the backend (`DELETE /api/providers/login/accounts/{provider}`). The
// answers carry metadata only; no token ever reaches the browser.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LogOut } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api.ts";
import { relativeTime } from "../lib/format.ts";
import type { ProviderOption, SignInAccount } from "../lib/restTypes.ts";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { Badge, Button, Notice } from "../ui/controls.tsx";
import { ConfirmDialog } from "../ui/Dialog.tsx";

/** Query key of a provider's sign-ins (invalidated after a sign-in or a sign-out). */
export function signInsKey(provider: string) {
  return ["models", "sign-ins", provider] as const;
}

export function useSignIns(provider: string, enabled = true) {
  return useQuery({
    queryKey: signInsKey(provider),
    queryFn: () => api.providerLogin.accounts(provider),
    enabled,
    retry: false,
  });
}

/** "plus" → "Plus"; the plan names come from the sign-in's own claims. */
function planName(plan: string): string {
  return plan.charAt(0).toUpperCase() + plan.slice(1);
}

function expiryText(account: SignInAccount, now: number): string | null {
  if (account.expires_at === null) return null;
  const when = relativeTime(account.expires_at, now);
  return account.expired
    ? `access expired ${when}; renewed on the next use`
    : `access renews by itself · expires ${when}`;
}

function AccountLine({ account, now }: { account: SignInAccount; now: number }) {
  const expiry = expiryText(account, now);
  return (
    <li
      data-testid={`sign-in-account-${account.id}`}
      className="flex flex-wrap items-center gap-2 text-[0.8125rem]"
    >
      <span className="font-medium text-ink">{account.label}</span>
      {account.plan ? <Badge tone="accent">{planName(account.plan)}</Badge> : null}
      {account.expired ? <Badge tone="warn">expired</Badge> : null}
      {account.status !== "ok" ? <Badge tone="danger">{account.status}</Badge> : null}
      {expiry ? <span className="text-[0.75rem] text-ink-muted">{expiry}</span> : null}
    </li>
  );
}

/** The signed-in accounts of `provider` and Sign out; nothing while it is not signed in. */
export function SignInAccounts({ provider }: { provider: ProviderOption }) {
  const client = useQueryClient();
  const toast = useToast();
  const [confirming, setConfirming] = useState(false);
  const [problem, setProblem] = useState("");
  const signIns = useSignIns(provider.name);
  const signOut = useMutation({
    mutationFn: () => api.providerLogin.signOut(provider.name),
    onMutate: () => setProblem(""),
    onSuccess: () => {
      setConfirming(false);
      toast.show(`Signed out of ${provider.label}.`, "success");
      void client.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (error) => {
      setConfirming(false);
      setProblem(errorText(error));
    },
  });
  const data = signIns.data;
  if (!data?.signed_in || data.accounts.length === 0) return null;
  const now = Date.now();
  return (
    <section
      aria-label={`Signed-in accounts of ${provider.label}`}
      className="flex flex-col gap-2 border border-line p-3"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <ul className="m-0 flex min-w-0 list-none flex-col gap-1.5 p-0">
          {data.accounts.map((account) => (
            <AccountLine key={account.id} account={account} now={now} />
          ))}
        </ul>
        <Button
          size="sm"
          tone="danger-outline"
          icon={<LogOut />}
          onClick={() => setConfirming(true)}
          disabled={signOut.isPending}
        >
          Sign out
        </Button>
      </div>
      {problem ? <Notice tone="danger">{problem}</Notice> : null}
      <ConfirmDialog
        open={confirming}
        title={`Sign out of ${provider.label}?`}
        description="Indus forgets this sign-in on this profile. API keys stay. You can sign in again any time."
        confirmLabel="Sign out"
        danger
        busy={signOut.isPending}
        onConfirm={() => signOut.mutate()}
        onCancel={() => setConfirming(false)}
      />
    </section>
  );
}
