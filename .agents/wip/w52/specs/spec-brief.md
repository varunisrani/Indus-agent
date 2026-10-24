# Wave 52 spec writers (Read + Note phase only; no code)

Context: read /home/user/indus-agent/.agents/wip/w51/brief.md first (owner decision: Indus UI must look the same as the
reference Hermes Agent; clean room still applies to code and assets). Two foundation builders are building the shared
tokens/shell/components right now (web: wt-w51a, desktop: wt-w51b); page builders in wave 52 will build each page group
from YOUR note with the reference closed. So your note must be complete enough to rebuild the pages without looking at
the reference again — but written in our words, with no reference code, CSS, class names, file contents or long UI
sentences copied (short labels of 1–4 words are fine).

Do, for your slice's slugs:
1. Look at every reference screenshot pair ($SP/compare/hermes/<surface>/<slug>--light.png / --dark.png; titles/notes in
   manifest.json) and our current pair ($SP/compare/indus/<surface>/, may be missing or named differently).
2. Read the reference source for those screens (/root/reference/hermes-agent/web/src or apps/desktop/src) to understand
   structure, states and behaviour (empty/loading/error, hover, dialogs, what each control does).
3. Read OUR code for the same screens (/home/user/indus-agent/apps/web/src or apps/desktop/src): which files render them,
   which data/API each control uses, and what our backend does not provide (gaps).
4. Write $SP/w52-specs/<slice>.md:
   - Per slug: layout (regions, widths/heights in px measured from the 1440x900 shots, spacing, alignment), component
     inventory (each element: kind, label, icon description, state), typography (size/weight/case/letter-spacing,
     monospace or not), colours for light and dark (hex sampled from the PNGs — you may use python3 to sample pixels),
     borders/radii/shadows, states and interactions, behaviour.
   - Mapping: which of OUR files change, which existing components/tests are affected, data available vs missing; for
     missing data say "show the control, wire to <our API>" or "placeholder state" — never invent backend features.
   - A build order and a checklist the builder ticks in Compare.
Rules: read-only everywhere except your spec file; no commits; never write under /root, ~, /home/user/indus-agent.
python3 with the stdlib is enough for pixel sampling (PNG decode via zlib), or use the Chromium at /opt/pw-browsers to
load the PNG in a canvas. TIME BOX ~45 minutes. Report: the spec path and a 5-line summary of the biggest differences.
