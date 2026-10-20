// An on/off switch on the Radix primitive (role="switch", aria-checked).

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
      className="relative h-5 w-9 shrink-0 rounded-full border border-line bg-code data-[state=checked]:bg-accent disabled:opacity-60"
    >
      <RadixSwitch.Thumb className="block h-4 w-4 translate-x-0.5 rounded-full bg-raised transition-transform data-[state=checked]:translate-x-4" />
    </RadixSwitch.Root>
  );
}
