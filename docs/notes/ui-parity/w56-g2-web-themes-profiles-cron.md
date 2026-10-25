# Task: Web follow-ups: eight theme presets, profile skill counts, blueprint slot fields (W56-G2) Phase: ui-parity Owner: builder w56g2

Status: Built (2026-10-25, wave 56 builder G2, base local 383e3f5). Uncommitted, for the integrator.

Read: the wave 56 and wave 54 briefs, `.agents/wip/w54/README.md`, `.agents/wip/w55/README.md`,
the wave 54 gap list (web theme-menu, profiles, cron-create, cron-blueprints),
`docs/notes/ui-parity/w55-f2-backend-data.md`, `docs/notes/ui-parity/web-foundation.md` and our own
sources: `apps/web/src/theme/*`, `apps/web/src/index.css`, `apps/web/src/pages/profiles/ProfileCard.tsx`,
`apps/web/src/pages/cron/{JobModal,Blueprints}.tsx`, `src/indus/web/routes/{profiles,cron}.py`,
`src/indus/cron/blueprints.py`. Reference: `web/src/themes/presets.ts` opened only to list its
preset names and colour values so that none of ours repeats them. Evidence ids: none.

What it does (own words):

- Theme presets 4 → 8. Four new pairs of our own: Monsoon (dark: rain-cloud blue canvas, mist ink),
  Sandstone, Neem and Indigo Paper (light: a near-black tinted ink on a tinted paper canvas). That
  makes four light and four dark. A preset is still only its canvas and ink; every surface and
  ink tint is mixed from them in `index.css`, so nothing else changed. The theme menu can scroll
  when the window is too short for eight rows.
- Contrast test. `presets.test.ts` reads the mix shares from `index.css` and works out, per
  preset, the colours the page really paints (`color-mix(in srgb)` interpolates the encoded
  channels) and their WCAG contrast. Each preset must keep ink, muted ink and soft ink at 4.5:1 or
  more on the canvas, the raised surface and the wash, ink and muted ink on hovered and selected
  rows too, and canvas-on-ink (filled buttons) at 4.5:1. The faint tint (placeholders, the empty
  "No description" line) is not held to AA: at 42 % ink no light pair reaches 3:1.
- Clear Blue keeps the reference's light look: its ink is 5.07:1 on its canvas but its muted and
  soft mixes are below AA (3.7 and 2.7). The test holds it to AA for its ink only and asserts it
  still falls short on the mixes, so the exception has to be removed on purpose once it is fixed.
- Profile cards read `skill_count` from the `/api/profiles` row (W55-F2-2) instead of one
  `/api/skills` call per card; a null count (unreadable profile) shows no skills line rather
  than a zero.
- Blueprint slot fields. The blueprint route sends the prompt already filled from the slot
  defaults (a slot with no default stays `{name}`) and the slots as `fields`, but not the prompt
  template. `cron/blueprintSlots.ts` rebuilds the template from that: a `{name}` still in the
  prompt is a slot's place; a defaulted slot's place is where its default occurs, only when it
  occurs exactly once (no overlaps). `text` slots become a text box (max length from the slot),
  `choice` slots a list of their options, with the slot's help as the hint; they fill the prompt
  live. `time`, `weekdays` and `interval` slots stay with the schedule picker, which is already
  prefilled from the blueprint's schedule. A slot whose place cannot be found gets no input (the
  person edits the prompt text). Typing in the prompt detaches the fields (disabled, with "Fill
  from fields again"). Create refuses an empty required slot by its label, and any `{slot}` still
  in the prompt. Jobs are created through the same `POST /api/cron/jobs` as before.

Decisions: W56-G2-1 eight presets (four light, four dark), each tested for AA text contrast in
its own scheme against the mixes read from `index.css`; Clear Blue is a named exception held to
AA for its ink only; the faint tint is outside the AA check. W56-G2-2 profile cards use the row's
`skill_count`; null hides the line. W56-G2-3 the web job form rebuilds a blueprint's prompt
template from the prefilled prompt and the slot defaults (unique occurrence only) because the
route does not send the template; only text and choice slots become inputs.

Follow-ups: let `GET /api/cron/blueprints` also send each blueprint's prompt and schedule
templates (`form()` already has the schedule one), so every slot can be placed without the
unique-default rule and the timing slots can be shown as their own inputs; the optional slot of
the evening wrap-up gets no input today for that reason. Clear Blue's muted and soft text are
below AA (an owner call: the look follows the reference). The desktop job form could reuse the
same slot rule.

Compare checklist:
- [x] Written from our own sources; no reference code, CSS or assets; preset names and values ours.
- [x] Only data our backend already serves (no route or field invented).
- [x] Re-shot theme-menu, profiles, cron-create, cron-blueprints, the blueprint set-up dialog and
      every preset, and looked at them.
- [x] biome, tsc, web vitest, web Playwright, tooling.checks.
