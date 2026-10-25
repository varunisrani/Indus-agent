// Labels and help lines for messaging fields (W52-k). The backend sends only the env key
// (`TELEGRAM_BOT_TOKEN`); the page shows a short label in our words, picked by the key's ending.
// W57-H1-3: every key the gateway catalogue sends has copy here (the bot-sender policy showed as a
// raw `INDUS_TELEGRAM_ALLOW_BOTS`); a key added later still gets a readable label made from its
// words (`fallbackLabel`), never the env name itself.

import type { MessageKey } from "../../i18n/index.tsx";

export interface FieldCopy {
  readonly label: MessageKey | null;
  readonly help: MessageKey | null;
  /** The value is a comma-separated list edited one entry per input. */
  readonly list: boolean;
}

// Longer endings first: the first match wins (`_PHONE_NUMBER_ID` before `_PHONE_NUMBER`).
const ENDINGS: ReadonlyArray<readonly [string, MessageKey, MessageKey | null, boolean?]> = [
  ["_BOT_TOKEN", "manage.messaging.field.botToken", "manage.messaging.help.botToken"],
  ["_APP_TOKEN", "manage.messaging.field.appToken", "manage.messaging.help.appToken"],
  [
    "_GROUP_ALLOWED_CHATS",
    "manage.messaging.field.allowedGroups",
    "manage.messaging.help.allowedGroups",
    true,
  ],
  [
    "_GROUP_ALLOWED_USERS",
    "manage.messaging.field.allowedGroupUsers",
    "manage.messaging.help.allowedGroupUsers",
    true,
  ],
  [
    "_ALLOWED_USERS",
    "manage.messaging.field.allowedUsers",
    "manage.messaging.help.allowedUsers",
    true,
  ],
  ["_ALLOW_ALL_USERS", "manage.messaging.field.allowAll", "manage.messaging.help.allowAll"],
  ["_ALLOW_BOTS", "manage.messaging.field.allowBots", "manage.messaging.help.allowBots"],
  ["_HOME_CHANNEL", "manage.messaging.field.homeChannel", "manage.messaging.help.homeChannel"],
  ["_HOMESERVER", "manage.messaging.field.homeserver", null],
  ["_SERVICE_ACCOUNT_JSON", "manage.messaging.field.serviceAccount", "manage.messaging.help.token"],
  ["_ENCODING_AES_KEY", "manage.messaging.field.aesKey", "manage.messaging.help.token"],
  ["_PRIVATE_KEY", "manage.messaging.field.privateKey", "manage.messaging.help.token"],
  ["_API_KEY", "manage.messaging.field.apiKey", "manage.messaging.help.token"],
  ["_ACCESS_TOKEN", "manage.messaging.field.token", "manage.messaging.help.token"],
  ["_TOKEN", "manage.messaging.field.token", "manage.messaging.help.token"],
  ["_PASSWORD", "manage.messaging.field.password", "manage.messaging.help.token"],
  ["_SIGNING_SECRET", "manage.messaging.field.secret", "manage.messaging.help.token"],
  ["_CLIENT_SECRET", "manage.messaging.field.clientSecret", "manage.messaging.help.token"],
  ["_APP_SECRET", "manage.messaging.field.appSecret", "manage.messaging.help.token"],
  ["_SECRET", "manage.messaging.field.secret", "manage.messaging.help.token"],
  ["_CLIENT_STATE", "manage.messaging.field.clientState", "manage.messaging.help.token"],
  ["_CLIENT_ID", "manage.messaging.field.clientId", null],
  ["_TENANT_ID", "manage.messaging.field.tenantId", null],
  ["_APP_ID", "manage.messaging.field.appId", null],
  ["_BOT_ID", "manage.messaging.field.botId", null],
  ["_CORP_ID", "manage.messaging.field.corpId", null],
  ["_ACCOUNT_SID", "manage.messaging.field.accountSid", null],
  ["_ACCOUNT_ID", "manage.messaging.field.accountId", null],
  ["_PHONE_NUMBER_ID", "manage.messaging.field.phoneNumberId", null],
  ["_PHONE_NUMBER", "manage.messaging.field.phoneNumber", "manage.messaging.help.phoneNumber"],
  ["_USER_ID", "manage.messaging.field.userId", null],
  ["_ACCOUNT", "manage.messaging.field.account", "manage.messaging.help.phoneNumber"],
  ["_ADDRESS", "manage.messaging.field.address", null],
  ["_SERVER", "manage.messaging.field.server", null],
  ["_NICKNAME", "manage.messaging.field.nickname", null],
  ["_TOPIC", "manage.messaging.field.topic", null],
  ["_WS_URL", "manage.messaging.field.wsUrl", null],
  ["_PROFILE", "manage.messaging.field.profile", null],
];

/**
 * A readable label for a key with no copy: the words after `INDUS_` and the platform's own prefix
 * (`INDUS_SIGNAL_GROUP_MODE` on Signal reads "Group mode").
 */
export function fallbackLabel(key: string, platform?: string): string {
  let rest = key.toUpperCase().replace(/^INDUS_/, "");
  const prefix = platform === undefined ? "" : `${platform.toUpperCase()}_`;
  if (prefix !== "_" && rest.startsWith(prefix) && rest.length > prefix.length) {
    rest = rest.slice(prefix.length);
  }
  const words = rest.split("_").filter((word) => word !== "");
  if (words.length === 0) return key;
  const text = words.join(" ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

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
