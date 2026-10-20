# Wave 30 plan (lead: Agent 2)

Started now (main 0ab0d95, independent of the parked patches):
- M1 = P19-14..17 MCP serve (worktree w30-m1).

After P18 (int-p18) lands — base on that main; brief from .agents/wip/w29/brief.md + the landed P18 notes:
- R1 = P18-11 sessions, P18-12 config / env keys / effective config
- R2 = P18-13 status / logs / analytics, P18-14 cron / skills / plugins / MCP
- R3 = P18-15 channels / webhooks / pairing / daemon control, P18-16 profiles / models / files / system
  (REST shapes must match F1's contract in docs/notes/P18/rest-client.md; generate the route list that replaces
  apps/web/src/test/_stubs_f1.ts; WS creds in the subprotocol per the P18 lead decision)

After P19 (int-p19) lands:
- A2 = P19-08..13 (prompt conversion, prompt/cancel, event projection, permission bridge + clarify, slash/model/mode,
  toolset policy + editor MCP)
- P19-18 conformance once A2 and M1 are in.

Cap: 4 agents on the machine at once; no new agent from 75% usage.
