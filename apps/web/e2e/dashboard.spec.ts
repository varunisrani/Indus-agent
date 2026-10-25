// Admin pages against a real `indus dashboard` and daemon (P18-30): Sessions search and delete,
// Config save.

import { expect, test } from "./fixtures.ts";

test("Sessions: search finds a message with its snippet; delete removes the session", async ({
  launch,
  page,
}) => {
  const stack = await launch({
    turns: [[{ text: "The pelican flies north." }], [{ text: "Nothing about birds." }]],
  });
  const ids: string[] = [];
  for (const prompt of ["tell me about the pelican", "something else"]) {
    const ran = await stack.ask("indus", { args: ["chat", "-q", prompt] });
    expect(ran.code, String(ran.stderr)).toBe(0);
    const found = /session_id: (\S+)/.exec(String(ran.stderr));
    expect(found).not.toBeNull();
    ids.push(found?.[1] ?? "");
  }
  const [pelican, other] = ids as [string, string];

  await page.goto(`${stack.url}/sessions`);
  // The page opens on the Overview; the rows with actions and the search live under History.
  await page.getByRole("button", { name: "History", exact: true }).click();
  const list = page.getByRole("list", { name: "Sessions" });
  await expect(list.getByTestId(`session-${pelican}`)).toBeVisible();
  await expect(list.getByTestId(`session-${other}`)).toBeVisible();

  // Search runs as you type; the hits replace the list.
  await page.getByRole("textbox", { name: "Search sessions" }).fill("pelican");
  const results = page.getByRole("list", { name: "Search results" });
  await expect(results.getByRole("listitem")).not.toHaveCount(0);
  await expect(results.locator("mark").first()).toHaveText(/pelican/i);
  await expect(results).not.toContainText("Nothing about birds");

  await page.getByRole("button", { name: "Clear search" }).click();
  await list.getByTestId(`session-${other}`).getByRole("button", { name: "Delete" }).click();
  const dialog = page.getByRole("dialog", { name: "Delete this session?" });
  await dialog.getByRole("button", { name: "Delete" }).click();
  await expect(list.getByTestId(`session-${other}`)).toHaveCount(0);
  await expect(list.getByTestId(`session-${pelican}`)).toBeVisible();
  // Gone from the store too; the other session and its messages stay.
  expect(await stack.rows(`SELECT count(*) FROM sessions WHERE id = '${other}'`)).toEqual([[0]]);
  expect(await stack.rows(`SELECT count(*) FROM sessions WHERE id = '${pelican}'`)).toEqual([[1]]);
});

test("Config: a saved value lands in config.yaml and survives a reload", async ({
  launch,
  page,
}) => {
  const stack = await launch({ turns: [[{ text: "Hello." }]] });
  await page.goto(`${stack.url}/config`);
  // One section shows at a time: open the Agent section first.
  const sections = page.getByRole("navigation", { name: "Config sections" });
  await sections.getByRole("button", { name: /^Agent/ }).click();
  const field = page.getByRole("spinbutton", { name: "agent.max_turns" });
  await expect(field).toBeVisible();
  await field.fill("37");
  await page.getByRole("button", { name: "Save" }).click();
  await expect
    .poll(async () => String((await stack.ask("read", { path: "config.yaml" })).text))
    .toMatch(/max_turns: 37/);
  // The fake model's settings written by the harness are still there.
  const saved = String((await stack.ask("read", { path: "config.yaml" })).text);
  expect(saved).toMatch(/base_url: http:\/\/127\.0\.0\.1:\d+\/v1/);

  await page.reload();
  await sections.getByRole("button", { name: /^Agent/ }).click();
  await expect(page.getByRole("spinbutton", { name: "agent.max_turns" })).toHaveValue("37");
});

test("Logs: the gateway log's tail as the daemon wrote it", async ({ launch, page }) => {
  const stack = await launch({ turns: [[{ text: "Hello." }]] });
  await page.goto(`${stack.url}/logs`);
  await page.getByRole("group", { name: "File" }).getByRole("button", { name: "gateway" }).click();
  await expect(page.getByRole("heading", { name: "gateway.log" })).toBeVisible();
  const lines = page.getByRole("list", { name: "Log lines" }).getByRole("listitem");
  await expect(lines).not.toHaveCount(0);
  const written = String((await stack.ask("read", { path: "logs/gateway.log" })).text);
  const shown = await lines.allTextContents();
  for (const line of shown) expect(written).toContain(line);
});

test("Docs: a direct load and a reload of /docs render the in-app page", async ({
  launch,
  page,
}) => {
  const stack = await launch({ turns: [[{ text: "Hello." }]] });
  await page.goto(`${stack.url}/docs`);
  await expect(page.getByRole("heading", { level: 1, name: "Documentation" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open docs" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "Documentation" })).toBeVisible();
});
