// TEST DOUBLE: drives the TUI's dashboard feed (apps/tui/src/transport/dashboardFeed.ts) the way the
// TUI entry does, without a terminal: the plan from the environment the dashboard gives its TUI
// child, then every event of FEED_EVENTS (a JSON list) published, then a clean close. Prints one
// line: `<state> sent=<n> leaked=<credential still in the env>`. Nothing here is product code.

import { DashboardFeed, takeFeedPlan } from "../../../apps/tui/src/transport/dashboardFeed.ts";

const plan = takeFeedPlan(process.env);
if (plan === null) {
  process.stdout.write("no-plan\n");
  process.exit(3);
}
const leaked = process.env.INDUS_TUI_SIDECAR_CREDENTIAL !== undefined;
const feed = new DashboardFeed(plan);
feed.start();
for (const event of JSON.parse(process.env.FEED_EVENTS ?? "[]")) feed.publish(event);
const settleMs = Number(process.env.FEED_SETTLE_MS ?? "10000");
await feed.settled(settleMs);
// A refused credential is closed by the server after the upgrade, so the close can land after the
// queue drained: when the test expects a state, wait for it (bounded) instead of reading it at once.
const want = process.env.FEED_WAIT_STATE;
if (want !== undefined) {
  const end = Date.now() + settleMs;
  while (feed.state !== want && Date.now() < end) await new Promise((r) => setTimeout(r, 20));
}
process.stdout.write(`${feed.state} sent=${feed.sent} leaked=${leaked}\n`);
feed.close();
