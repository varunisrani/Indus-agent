// Preview annotate mode (P24-25): pins with notes on elements of the page in the preview.
//
// Two halves:
//
// - `guestApi` runs INSIDE the guest page (the webview's sandbox). It is written as one
//   self-contained function — no reference to anything outside it — so its source can be handed to
//   `executeJavaScript` (`guestInstallScript`). It picks the element the person clicks, builds a
//   short selector path and a small piece of markup, and removes every secret first: values of
//   password and hidden inputs, of fields whose name / id / autocomplete looks secret, attributes
//   whose name looks secret, and the content of `data-secret` elements. Redaction works on a clone,
//   so the live page is untouched, and nothing secret ever leaves the guest. The same function
//   serves the agent's automation (`automation.ts`): click, type, scroll, snapshot, agent pins.
// - `AnnotateSession` lives in the app's renderer. It keeps the pins and their notes, batches them
//   by DOM subtree (`groupPins`) and composes one message. It sends only when `send()` is called by
//   the person's click: switching annotate mode off, closing the pane or waiting never sends.

export const REDACTED = "[redacted]";
export const MARKUP_BUDGET = 600;
export const MAX_NOTE = 2_000;
export const MAX_PINS = 50;
/** The guest API's global name. */
export const GUEST_GLOBAL = "__indusPreview";

export interface PickRect {
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

/** What the guest reports for a clicked element (already redacted). */
export interface GuestPick {
  readonly selector: string;
  readonly html: string;
  readonly text: string;
  readonly rect: PickRect | null;
}

export interface Pin extends GuestPick {
  readonly number: number;
  readonly note: string;
}

export interface PinBatch {
  /** The shared container path, "" for pins with no element. */
  readonly key: string;
  /** The last segment of `key`, e.g. `section.pricing`. */
  readonly label: string;
  readonly pins: readonly Pin[];
}

// -- the guest side -------------------------------------------------------------------------------

/**
 * The guest page's API. Self-contained on purpose (see the module notes): keep every helper inside.
 * Returns the API object; the install script stores it on `window[GUEST_GLOBAL]`.
 */
export function guestApi(win: Window) {
  const doc = win.document;
  const REDACT = "[redacted]";
  const BUDGET = 600;
  const secretName = /pass|secret|token|key|auth|session|credential|cookie|otp|pin\b/i;

  const looksSecret = (value: string | null | undefined): boolean =>
    typeof value === "string" && value !== "" && secretName.test(value);

  const secretField = (node: Element): boolean => {
    const tag = node.tagName.toLowerCase();
    if (tag !== "input" && tag !== "textarea" && tag !== "select") return false;
    const type = (node.getAttribute("type") || "").toLowerCase();
    if (tag === "input" && (type === "password" || type === "hidden")) return true;
    return (
      looksSecret(node.getAttribute("name")) ||
      looksSecret(node.getAttribute("id")) ||
      looksSecret(node.getAttribute("autocomplete")) ||
      node.hasAttribute("data-secret")
    );
  };

  /** Scrub `copy`, a clone: secret-named attributes, secret fields' values, `data-secret` text. */
  const scrub = (copy: Element): void => {
    for (const node of [copy, ...Array.from(copy.querySelectorAll("*"))]) {
      for (const attr of Array.from(node.attributes)) {
        if (looksSecret(attr.name)) node.setAttribute(attr.name, REDACT);
      }
      const tag = node.tagName.toLowerCase();
      if (secretField(node)) {
        if (tag === "textarea") node.textContent = REDACT;
        else if (tag === "select") {
          for (const option of Array.from(node.querySelectorAll("option"))) {
            option.textContent = REDACT;
            option.setAttribute("value", REDACT);
          }
        } else node.setAttribute("value", REDACT);
      } else if (node.hasAttribute("data-secret")) {
        node.textContent = REDACT;
      }
    }
  };

  /** A scrubbed copy of `el`: everything read for the app comes from this, never the page. */
  const scrubbed = (el: Element): Element => {
    const copy = el.cloneNode(true) as Element;
    scrub(copy);
    for (const node of Array.from(copy.querySelectorAll("script,style,template"))) node.remove();
    return copy;
  };

  const markup = (copy: Element): string => {
    const html = copy.outerHTML;
    return html.length > BUDGET ? `${html.slice(0, BUDGET)}…` : html;
  };

  const segment = (el: Element): string => {
    const tag = el.tagName.toLowerCase();
    const id = el.getAttribute("id");
    if (id && /^[A-Za-z][\w-]{0,63}$/.test(id) && !looksSecret(id)) return `${tag}#${id}`;
    const classes = Array.from(el.classList)
      .filter((c) => /^[A-Za-z_][\w-]{0,40}$/.test(c))
      .slice(0, 2);
    let text = tag + classes.map((c) => `.${c}`).join("");
    const parent = el.parentElement;
    if (parent !== null) {
      const same = Array.from(parent.children).filter((c) => c.tagName === el.tagName);
      if (same.length > 1) text += `:nth-of-type(${same.indexOf(el) + 1})`;
    }
    return text;
  };

  const selectorOf = (el: Element): string => {
    const parts: string[] = [];
    let node: Element | null = el;
    while (node !== null && node !== doc.documentElement) {
      parts.unshift(segment(node));
      if (node.tagName.toLowerCase() === "body") break;
      node = node.parentElement;
    }
    return parts.join(">");
  };

  /** The text of a scrubbed copy (secret fields and `data-secret` content are already gone). */
  const textOf = (copy: Element, limit: number): string => {
    if (secretField(copy)) return "";
    const text = (copy.textContent ?? "").replace(/\s+/g, " ").trim();
    return text.length > limit ? `${text.slice(0, limit)}…` : text;
  };

  const rectOf = (el: Element) => {
    const r = el.getBoundingClientRect();
    return { x: r.left, y: r.top, width: r.width, height: r.height };
  };

  const pickOf = (el: Element) => {
    const copy = scrubbed(el);
    return {
      selector: selectorOf(el),
      html: markup(copy),
      text: textOf(copy, 200),
      rect: rectOf(el),
    };
  };

  // -- annotate mode: clicks become picks ---------------------------------------------------------
  const queue: unknown[] = [];
  let waiting: ((value: unknown) => void) | null = null;
  let listening = false;
  const deliver = (value: unknown) => {
    if (waiting !== null) {
      const resolve = waiting;
      waiting = null;
      resolve(value);
    } else queue.push(value);
  };
  const onClick = (event: MouseEvent) => {
    if (!listening) return;
    const target = event.target as Element | null;
    if (target === null || target.closest(`[data-indus-pin]`) !== null) return;
    event.preventDefault();
    event.stopPropagation();
    deliver({ type: "pick", pick: pickOf(target) });
  };

  // -- pin markers (drawn on the page, never part of a pick) --------------------------------------
  let layer: HTMLElement | null = null;
  const showPins = (pins: { number: number; selector: string; note?: string }[]) => {
    layer?.remove();
    layer = doc.createElement("div");
    layer.setAttribute("data-indus-pin", "layer");
    layer.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647";
    for (const pin of pins) {
      let el: Element | null = null;
      try {
        el = doc.querySelector(pin.selector);
      } catch {
        el = null;
      }
      if (el === null) continue;
      const r = el.getBoundingClientRect();
      const mark = doc.createElement("div");
      mark.setAttribute("data-indus-pin", String(pin.number));
      mark.textContent = String(pin.number);
      if (pin.note) mark.title = pin.note;
      mark.style.cssText = `position:fixed;left:${Math.max(0, r.left - 11)}px;top:${Math.max(0, r.top - 11)}px;width:22px;height:22px;border-radius:11px;background:#b5562f;color:#fff;font:600 12px/22px system-ui,sans-serif;text-align:center;box-shadow:0 0 0 2px rgba(181,86,47,.35)`;
      const box = doc.createElement("div");
      box.setAttribute("data-indus-pin", "outline");
      box.style.cssText = `position:fixed;left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;outline:2px solid #b5562f;outline-offset:1px`;
      layer.append(box, mark);
    }
    doc.body.append(layer);
  };

  // -- automation ----------------------------------------------------------------------------------
  const find = (selector: unknown): Element | null => {
    if (typeof selector !== "string" || selector === "") return null;
    try {
      return doc.querySelector(selector);
    } catch {
      return null;
    }
  };

  const INTERACTIVE = "a[href],button,input,select,textarea,[role=button],[role=link],summary";

  const snapshot = () => {
    const elements = Array.from(doc.querySelectorAll(INTERACTIVE))
      .slice(0, 200)
      .map((el) => {
        const tag = el.tagName.toLowerCase();
        const row: Record<string, string> = { selector: selectorOf(el), tag };
        const label =
          el.getAttribute("aria-label") ||
          el.getAttribute("placeholder") ||
          el.getAttribute("title") ||
          textOf(scrubbed(el), 80);
        if (label) row.label = label;
        if (tag === "input" || tag === "textarea" || tag === "select") {
          row.value = secretField(el) ? REDACT : String((el as HTMLInputElement).value ?? "");
        }
        if (tag === "a") row.href = (el as HTMLAnchorElement).href;
        return row;
      });
    return {
      ok: true,
      title: doc.title,
      url: win.location.href,
      text: textOf(scrubbed(doc.body), 8_000),
      elements,
    };
  };

  const act = (payload: Record<string, unknown>) => {
    const action = payload.action;
    if (action === "annotate") {
      const pins = Array.isArray(payload.pins) ? payload.pins : [];
      showPins(
        pins.map((p, i) => ({
          number: i + 1,
          selector: String((p as { selector?: unknown }).selector ?? ""),
          note: String((p as { note?: unknown }).note ?? ""),
        })),
      );
      const missing = pins.filter((p) => find((p as { selector?: unknown }).selector) === null);
      return { ok: true, shown: pins.length - missing.length, missing: missing.length };
    }
    if (action === "scroll") {
      const el = find(payload.selector);
      if (payload.selector !== undefined) {
        if (el === null) return { ok: false, error: `no element matches ${payload.selector}` };
        el.scrollIntoView({ block: "center" });
        return { ok: true };
      }
      win.scrollBy(Number(payload.dx) || 0, Number(payload.dy) || 0);
      return { ok: true, x: win.scrollX, y: win.scrollY };
    }
    const el = find(payload.selector);
    if (el === null) return { ok: false, error: `no element matches ${String(payload.selector)}` };
    if (action === "click") {
      (el as HTMLElement).click();
      return { ok: true, clicked: selectorOf(el) };
    }
    if (action === "type") {
      if (secretField(el)) return { ok: false, error: "secret fields are left to the person" };
      const field = el as HTMLInputElement;
      field.focus();
      field.value = String(payload.text ?? "");
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
      if (payload.submit === true) {
        const form = field.form;
        if (form !== null && typeof form.requestSubmit === "function") form.requestSubmit();
      }
      return { ok: true };
    }
    return { ok: false, error: `unknown action ${String(action)}` };
  };

  return {
    start(): boolean {
      if (!listening) {
        listening = true;
        doc.addEventListener("click", onClick, true);
      }
      return true;
    },
    stop(): boolean {
      listening = false;
      doc.removeEventListener("click", onClick, true);
      deliver({ type: "end" });
      layer?.remove();
      layer = null;
      return true;
    },
    next(): Promise<unknown> {
      if (queue.length > 0) return Promise.resolve(queue.shift());
      if (!listening) return Promise.resolve({ type: "end" });
      return new Promise((resolve) => {
        waiting = resolve;
      });
    },
    pick(selector: string) {
      const el = find(selector);
      return el === null ? null : pickOf(el);
    },
    showPins,
    snapshot,
    act,
  };
}

export type GuestApi = ReturnType<typeof guestApi>;

/**
 * The script that installs the guest API once per page (idempotent). Built by concatenation:
 * the function's own source must not be spliced into a template literal, or its `${…}` parts would
 * be evaluated here instead of in the guest.
 */
export function guestInstallScript(): string {
  return (
    "(function(){if(!window." +
    GUEST_GLOBAL +
    "){window." +
    GUEST_GLOBAL +
    "=(" +
    guestApi.toString() +
    ")(window);}return true;})()"
  );
}

/** A call into the guest API with JSON arguments (never spliced code). */
export function guestCall(method: keyof GuestApi, ...args: unknown[]): string {
  const list = args.map((a) => JSON.stringify(a ?? null)).join(",");
  return `window.${GUEST_GLOBAL}.${String(method)}(${list})`;
}

// -- the app side ---------------------------------------------------------------------------------

const SEP = ">";

function parts(selector: string): string[] {
  return selector.split(SEP).filter((p) => p !== "");
}

/** The pin's container path: its selector without the last step (a one-step selector is itself). */
export function containerOf(selector: string): string {
  const list = parts(selector);
  return (list.length > 1 ? list.slice(0, -1) : list).join(SEP);
}

function within(inner: string, outer: string): boolean {
  return inner === outer || inner.startsWith(`${outer}${SEP}`);
}

function sharedPrefix(selectors: readonly string[]): string {
  if (selectors.length === 0) return "";
  const lists = selectors.map(parts);
  const common: string[] = [];
  for (let i = 0; ; i++) {
    const step = lists[0]?.[i];
    if (step === undefined || lists.some((l) => l[i] !== step)) break;
    common.push(step);
  }
  return common.join(SEP);
}

/**
 * Batches by DOM subtree: two pins are in one batch when they share a container, or when one
 * pin's element contains the other's. Pins without an element form one trailing batch. Batches
 * keep the order of their first pin.
 */
export function groupPins(pins: readonly Pin[]): PinBatch[] {
  const placed = pins.filter((p) => p.selector !== "");
  const loose = pins.filter((p) => p.selector === "");
  const parent = placed.map((_, i) => i);
  const root = (i: number): number => {
    let r = i;
    while (parent[r] !== r) r = parent[r] as number;
    parent[i] = r;
    return r;
  };
  for (let i = 0; i < placed.length; i++) {
    for (let j = i + 1; j < placed.length; j++) {
      const a = placed[i] as Pin;
      const b = placed[j] as Pin;
      const joined =
        containerOf(a.selector) === containerOf(b.selector) ||
        within(a.selector, b.selector) ||
        within(b.selector, a.selector);
      if (joined) parent[root(j)] = root(i);
    }
  }
  const byRoot = new Map<number, Pin[]>();
  placed.forEach((pin, i) => {
    const key = root(i);
    byRoot.set(key, [...(byRoot.get(key) ?? []), pin]);
  });
  const batches: PinBatch[] = [...byRoot.values()].map((members) => {
    const sorted = [...members].sort((x, y) => x.number - y.number);
    const key =
      sorted.length === 1
        ? containerOf((sorted[0] as Pin).selector)
        : sharedPrefix(sorted.map((p) => p.selector)) || containerOf((sorted[0] as Pin).selector);
    const list = parts(key);
    return { key, label: list[list.length - 1] ?? "", pins: sorted };
  });
  batches.sort((x, y) => (x.pins[0]?.number ?? 0) - (y.pins[0]?.number ?? 0));
  if (loose.length > 0) {
    batches.push({ key: "", label: "", pins: [...loose].sort((x, y) => x.number - y.number) });
  }
  return batches;
}

/** The one message for a set of batches (Markdown). */
export function composeMessage(page: string, batches: readonly PinBatch[]): string {
  const lines = [`Notes on ${page || "the page in the preview"}:`];
  batches.forEach((batch, index) => {
    const title = batch.label === "" ? "elsewhere on the page" : `\`${batch.label}\``;
    lines.push("", `Group ${index + 1} (${title}):`);
    for (const pin of batch.pins) {
      lines.push(`${pin.number}. ${pin.note.trim() || "(no note)"}`);
      if (pin.selector !== "") lines.push(`   element: \`${pin.selector}\``);
      if (pin.html !== "") lines.push(`   markup: \`${pin.html.replace(/`/g, "'")}\``);
    }
  });
  return lines.join("\n");
}

export class AnnotateSession {
  private pins: Pin[] = [];
  private counter = 0;
  private readonly listeners = new Set<() => void>();
  private readonly onSend: (message: string) => void;

  constructor(options: { onSend: (message: string) => void }) {
    this.onSend = options.onSend;
  }

  list(): readonly Pin[] {
    return this.pins;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private changed(): void {
    for (const listener of [...this.listeners]) listener();
  }

  add(pick: GuestPick): Pin | null {
    if (this.pins.length >= MAX_PINS) return null;
    this.counter += 1;
    const pin: Pin = {
      number: this.counter,
      selector: pick.selector,
      html: pick.html.slice(0, MARKUP_BUDGET + 1),
      text: pick.text,
      rect: pick.rect,
      note: "",
    };
    this.pins = [...this.pins, pin];
    this.changed();
    return pin;
  }

  setNote(number: number, note: string): void {
    this.pins = this.pins.map((p) =>
      p.number === number ? { ...p, note: note.slice(0, MAX_NOTE) } : p,
    );
    this.changed();
  }

  remove(number: number): void {
    this.pins = this.pins.filter((p) => p.number !== number);
    this.changed();
  }

  batches(): PinBatch[] {
    return groupPins(this.pins);
  }

  message(page: string): string {
    return composeMessage(page, this.batches());
  }

  /** The person pressed Send: one message for every batch, then the pins are cleared. */
  send(page: string): boolean {
    if (this.pins.length === 0) return false;
    const text = this.message(page);
    this.pins = [];
    this.counter = 0;
    this.changed();
    this.onSend(text);
    return true;
  }

  /** Leave annotate mode: the pins are dropped and nothing is sent. */
  discard(): void {
    this.pins = [];
    this.counter = 0;
    this.changed();
  }
}
