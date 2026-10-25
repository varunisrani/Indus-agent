// Labels and help lines for messaging fields (W52-k). The backend sends only the env key
// (`TELEGRAM_BOT_TOKEN`); the page shows a short label in our words, picked by the key's ending.
// An unknown key shows itself (in mono) with no help line.

import type { MessageKey } from "../../i18n/index.tsx";

export interface FieldCopy {
  readonly label: MessageKey | null;
  readonly help: MessageKey | null;
  /** The value is a comma-separated list edited one entry per input. */
  readonly list: boolean;
}

const ENDINGS: ReadonlyArray<readonly [string, MessageKey, MessageKey | null, boolean?]> = [
  ["_BOT_TOKEN", "manage.messaging.field.botToken", "manage.messaging.help.botToken"],
  ["_APP_TOKEN", "manage.messaging.field.appToken", "manage.messaging.help.appToken"],
  [
    "_ALLOWED_USERS",
    "manage.messaging.field.allowedUsers",
    "manage.messaging.help.allowedUsers",
    true,
  ],
  ["_ALLOW_ALL_USERS", "manage.messaging.field.allowAll", "manage.messaging.help.allowAll"],
  ["_HOME_CHANNEL", "manage.messaging.field.homeChannel", "manage.messaging.help.homeChannel"],
  ["_HOMESERVER", "manage.messaging.field.homeserver", null],
  ["_API_KEY", "manage.messaging.field.apiKey", "manage.messaging.help.token"],
  ["_ACCESS_TOKEN", "manage.messaging.field.token", "manage.messaging.help.token"],
  ["_TOKEN", "manage.messaging.field.token", "manage.messaging.help.token"],
  ["_PASSWORD", "manage.messaging.field.password", "manage.messaging.help.token"],
  ["_SIGNING_SECRET", "manage.messaging.field.secret", "manage.messaging.help.token"],
  ["_SECRET", "manage.messaging.field.secret", "manage.messaging.help.token"],
  ["_USER_ID", "manage.messaging.field.userId", null],
];

export function fieldCopy(key: string): FieldCopy {
  const upper = key.toUpperCase();
  for (const [ending, label, help, list] of ENDINGS) {
    if (upper.endsWith(ending)) return { label, help, list: list === true };
  }
  return { label: null, help: null, list: false };
}

/** Split a stored list value into entries (always at least one, possibly empty, entry). */
export function listEntries(value: string): string[] {
  const parts = value.split(",");
  return parts.length === 0 ? [""] : parts;
}
