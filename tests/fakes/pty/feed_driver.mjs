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
await feed.settled(Number(process.env.FEED_SETTLE_MS ?? "10000"));
process.stdout.write(`${feed.state} sent=${feed.sent} leaked=${leaked}\n`);
feed.close();
