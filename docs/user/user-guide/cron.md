# Scheduled jobs

Indus Agent can run prompts, skills and scripts on a schedule: a morning summary of your inbox, a
nightly check of a build, a reminder in two hours. Results are saved locally and, if you like,
delivered to a chat.

<!-- test -->
```console
$ indus cron list
No scheduled jobs. Create one with `indus cron create "every 30m" "check the build"`.
```

## Creating a job

```bash
indus cron create "weekdays at 9am" "Summarise new issues in the tracker" --name triage --deliver telegram
indus cron create "every 30m" --script check-disk.sh --no-agent --deliver telegram
indus cron create "in 2h" "Remind me to call the bank"
```

Schedules are written in plain words (`every 30m`, `daily at 18:00`, `weekdays at 9am`,
`mon wed fri at noon`, `every 3 days at 7am`, `in 2h`), as a date and time for a single run
(`2026-12-24T09:00`), or as a five-field cron expression (`"0 9 * * 1-5"`). Times are in `timezone` from the
settings, else the machine's.

A job can:

- run a prompt with the agent, optionally loading skills first (`--skill`);
- run a script from `<home>/scripts/` before the agent and give it the output, or on its own
  (`--no-agent`: the script's output is the result);
- be gated by a monitor (`--monitor URL_OR_SCRIPT`) that decides whether the run goes ahead;
- use its own model (`--model`, `--provider`) and toolsets (`--toolsets`);
- keep notes between runs in its notepad (cursors, last-seen ids);
- take another job's last result as context (`--context-from`).

From a chat, just ask: "every Monday at 8, send me the open pull requests". The agent's
`cronjob_manage` tool creates the job (and `/cron` manages jobs from the CLI and TUI).

## Delivery

`--deliver` says where results go, as a comma list: `local` (the default: only saved under the
profile's cron output), `origin` (back to the chat the job was created from), a platform name (its
home channel, set with `/sethome` in that chat), `platform:chat_id` (or `platform:chat_id:thread_id`)
for one chat, or `all` for every connected platform's home channel. Failures can go elsewhere with
`--failure-deliver`; repeated failures raise an incident (`indus cron incidents`) instead of a flood
of alerts.

## Who runs the jobs

The scheduler runs inside the messaging gateway daemon (`indus gateway start`). Without a gateway,
`indus cron daemon` runs the scheduler in the foreground and `indus cron tick` runs one pass, for a
system timer. `indus cron status` says whether a ticker is alive and when the next job runs.

## Safety

Nobody is there to answer approval prompts, so risky commands in a job are refused
(`approvals.cron_mode: deny`). Jobs cannot create other jobs unless `cron.allow_agent_scheduling` is
on. `indus cron pause-all` stops every scheduled run at once; `indus cron doctor` checks every job's
keys, skills, targets and scripts before they cost anything.
