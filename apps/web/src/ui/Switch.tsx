// An on/off switch on the Radix primitive (role="switch", aria-checked): a square track whose
// thumb fills with ink when on.

import * as RadixSwitch from "@radix-ui/react-switch";

export function Switch({
  checked,
  onCheckedChange,
  label,
  disabled = false,
  id,
}: {
  checked: boolean;
  onCheckedChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
  id?: string;
}) {
  return (
    <RadixSwitch.Root
      {...(id === undefined ? {} : { id })}
      checked={checked}
      onCheckedChange={onCheckedChange}
      disabled={disabled}
      aria-label={label}
      className="relative h-[1.125rem] w-[2.125rem] shrink-0 cursor-pointer border border-line-strong bg-wash transition-colors data-[state=checked]:border-accent/60 data-[state=checked]:bg-accent/40 disabled:cursor-not-allowed disabled:opacity-50"
    >
      <RadixSwitch.Thumb className="block size-3 translate-x-[0.1875rem] bg-ink-soft transition-transform data-[state=checked]:translate-x-[1.0625rem] data-[state=checked]:bg-accent" />
    </RadixSwitch.Root>
  );
}
