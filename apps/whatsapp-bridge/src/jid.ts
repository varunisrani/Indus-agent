// WhatsApp address helpers shared by the session and the provider binding.
//
// A JID is `<local>[:<device>]@<server>`. People live on `s.whatsapp.net` (phone numbers), `lid`
// (linked ids) and the legacy `c.us`; groups on `g.us`; status updates and broadcast lists on
// `broadcast`; channels on `newsletter`.

const USER_SERVERS = new Set(["s.whatsapp.net", "lid", "c.us"]);

/** `123:4@s.whatsapp.net` → `123@s.whatsapp.net`; anything without a device part is unchanged. */
export function stripDevice(jid: string): string {
  const text = jid.trim();
  const at = text.indexOf("@");
  if (at < 0) return text;
  const local = text.slice(0, at);
  const colon = local.indexOf(":");
  return colon < 0 ? text : `${local.slice(0, colon)}${text.slice(at)}`;
}

function serverOf(jid: string): string {
  const at = jid.lastIndexOf("@");
  return at < 0 ? "" : jid.slice(at + 1).toLowerCase();
}

export function isGroup(jid: string): boolean {
  return serverOf(jid) === "g.us";
}

/** Status updates, broadcast lists and channels: never answered. */
export function isBroadcast(jid: string): boolean {
  const server = serverOf(jid);
  return server === "broadcast" || server === "newsletter";
}

export function isUser(jid: string): boolean {
  return USER_SERVERS.has(serverOf(jid));
}

/** The same person on both sides (device suffixes ignored); false for empty input. */
export function sameUser(a: string | null | undefined, b: string | null | undefined): boolean {
  if (!a || !b) return false;
  return stripDevice(a).toLowerCase() === stripDevice(b).toLowerCase();
}

/** A JID or number for logs: every digit but the last four hidden. */
export function mask(value: string): string {
  const digits = [...value].filter((c) => c >= "0" && c <= "9").length;
  const keep = digits > 6 ? 4 : 0;
  let seen = 0;
  let out = "";
  for (const c of value) {
    if (c >= "0" && c <= "9") {
      seen += 1;
      out += seen > digits - keep ? c : "*";
    } else {
      out += c;
    }
  }
  return out;
}
