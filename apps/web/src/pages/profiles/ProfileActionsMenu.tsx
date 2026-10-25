// A profile card's three-dot menu: set as active, the three editors, skills, the CLI command,
// export, then rename and delete (never offered for the default profile; disabled for the profile
// this dashboard serves).

import {
  Check,
  Cpu,
  Download,
  Package,
  Pencil,
  SquareTerminal,
  TextAlignStart,
  Trash,
} from "lucide-react";
import type { ProfileSummary } from "../../lib/restTypes.ts";
import { Menu, MenuDivider, MenuItem } from "../../ui/Menu.tsx";
import type { EditorKind } from "./ProfileEditorDialog.tsx";

export function lockedReason(profile: ProfileSummary): string | null {
  if (profile.is_default) return "The default profile cannot be renamed or deleted.";
  if (profile.is_current) return "This dashboard serves this profile; change it from elsewhere.";
  return null;
}

/** The persona item's glyph: a bold letter in an icon-sized slot. */
function PersonaGlyph() {
  return (
    <span
      aria-hidden="true"
      className="inline-flex size-4 shrink-0 items-center justify-center text-[0.8125rem] font-bold"
    >
      P
    </span>
  );
}

export function ProfileActionsMenu({
  profile,
  stickyPending,
  onSticky,
  onEdit,
  onSkills,
  onCopyCommand,
  onExport,
  onRename,
  onDelete,
}: {
  profile: ProfileSummary;
  stickyPending: boolean;
  onSticky: () => void;
  onEdit: (kind: EditorKind) => void;
  onSkills: () => void;
  onCopyCommand: () => void;
  onExport: () => void;
  onRename: () => void;
  onDelete: () => void;
}) {
  const locked = lockedReason(profile);
  return (
    <Menu label="Actions" className="-mt-1 -mr-1 ml-auto">
      {profile.is_sticky ? null : (
        <MenuItem icon={<Check />} disabled={stickyPending} onSelect={onSticky}>
          Set as active
        </MenuItem>
      )}
      <MenuItem icon={<Cpu />} onSelect={() => onEdit("model")}>
        Change model
      </MenuItem>
      <MenuItem icon={<TextAlignStart />} onSelect={() => onEdit("description")}>
        Edit description
      </MenuItem>
      <MenuItem icon={<PersonaGlyph />} onSelect={() => onEdit("persona")}>
        Edit persona
      </MenuItem>
      <MenuItem icon={<Package />} onSelect={onSkills}>
        Manage skills &amp; tools
      </MenuItem>
      <MenuItem icon={<SquareTerminal />} onSelect={onCopyCommand}>
        Copy CLI command
      </MenuItem>
      <MenuItem icon={<Download />} onSelect={onExport}>
        Export
      </MenuItem>
      {profile.is_default ? null : (
        <>
          <MenuDivider />
          <MenuItem
            icon={<Pencil />}
            disabled={locked !== null}
            title={locked ?? undefined}
            onSelect={onRename}
          >
            Rename
          </MenuItem>
          <MenuItem
            icon={<Trash />}
            danger
            disabled={locked !== null}
            title={locked ?? undefined}
            onSelect={onDelete}
          >
            Delete
          </MenuItem>
        </>
      )}
    </Menu>
  );
}
