// One pass over every route (light), full-page PNG + the buttons/links/inputs on it, to plan shots.
import fs from "node:fs";
import path from "node:path";
import { CONFIG_EXTRA, chromium, OUT, seed, sleep, Stack, TURNS } from "./stack.ts";

const ROUTES = ["/sessions", "/files", "/analytics", "/models", "/logs", "/cron", "/skills", "/plugins", "/mcp",
  "/channels", "/webhooks", "/pairing", "/profiles", "/profiles/new", "/config", "/env", "/system", "/nope"];
const stack = await Stack.start({ turns: TURNS, config_extra: CONFIG_EXTRA });
const browser = await chromium.launch({ headless: true });
try {
  console.log(JSON.stringify(await seed(stack)));
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  for (const r of ROUTES) {
    await page.goto(`${stack.url}${r}`);
    await page.waitForLoadState("networkidle").catch(() => undefined);
    await sleep(1500);
    const slug = r.replace(/\//g, "_");
    await page.screenshot({ path: path.join(OUT, `x${slug}.png`), fullPage: true });
    const info = await page.evaluate(() => {
      const q = (s: string) => Array.from(document.querySelectorAll(s));
      const name = (e: Element) => (e.getAttribute("aria-label") ?? e.textContent ?? "").trim().slice(0, 50);
      return {
        h: q("h1,h2,h3").map(name),
        b: q("main button, main [role=tab], main summary").map(name),
        i: q("main input, main select, main textarea").map((e) => `${e.tagName}:${e.getAttribute("type") ?? ""}:${e.getAttribute("aria-label") ?? e.id}`),
        height: document.querySelector("main")?.scrollHeight,
      };
    });
    fs.writeFileSync(path.join(OUT, `x${slug}.json`), JSON.stringify(info, null, 1));
    console.log(`page ${r} ${JSON.stringify(info).length}`);
  }
} finally {
  await browser.close();
  await stack.close();
}
process.exit(0);
