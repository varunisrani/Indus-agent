---
name: skill-authoring
description: Write or improve a SKILL.md for Indus Agent
version: 1.0.0
author: Indus Agent
license: MIT
metadata:
  indus:
    category: indus
    tags: [skills, authoring, skill-md, learning]
---

# Skill authoring

A skill is a folder with a `SKILL.md` that teaches the agent how to do one class of job well. The
prompt only shows each skill's name and a short description; the body is read with `skill_view`
when the job comes up. So the description decides whether a skill is ever opened, and the body
decides whether it helps.

## When to use

- The user asks you to turn a procedure, a lesson learned, or a conversation into a skill.
- An existing skill gave wrong or outdated guidance and needs a fix.
- You solved a tricky, repeatable problem and the user agrees it is worth keeping.

## Procedure

1. **Check what exists.** `skills_list`, then `skill_view` on anything close. Improving an existing
   skill, or adding a support file under a broader one, is better than a near-duplicate.
2. **Pick the scope.** One skill per *class* of job ("deploy a static site"), not per incident
   ("fix Tuesday's deploy"). Name it in lowercase with hyphens: letters, digits, `.`, `_`, `-`,
   at most 64 characters, starting with a letter or digit.
3. **Write the frontmatter.** `name`, a `description` of 60 characters or fewer that says what
   the skill does (it is cut at 60 in the index), and optional `version`, `author`, `license`,
   `metadata.indus.category` and `metadata.indus.tags`. See `references/frontmatter.md` for every
   field, including visibility conditions, required environment variables and per-skill config.
4. **Write the body in this order:**
   - one short paragraph on the purpose;
   - `## When to use`: the triggers, concrete enough to recognise;
   - `## Procedure`: numbered steps that name the tools to use (`terminal`, `read_file`,
     `patch`, `web_search`, `execute_code` and so on) and the commands to run;
   - `## Pitfalls`: the mistakes that actually happen, each with what to do instead;
   - `## Verification`: how to prove the job is done;
   - optionally `## Quick reference` last: a compact table or command list.
5. **Write rules, not stories.** Attach each lesson to the step it changes ("before step 3, check
   the lock file"), not as a narrative of what went wrong once. Leave out dates, ticket numbers,
   people's names and one-off environment quirks.
6. **Move depth out of the body.** Long reference material goes in `references/`, reusable
   starting files in `templates/`, helper programs in `scripts/`, other files in `assets/`.
   Mention each one from the body so the agent knows to open it. Inside the body,
   `${INDUS_SKILL_DIR}` expands to the skill's folder when the skill is invoked.
7. **Save it.** `skill_manage` with `create` (`name`, `content`, optional `category`), `patch` for
   targeted edits, and `write_file` for support files. Never invent commands or flags: if you
   are not sure a command exists, check it with `terminal` first.

## Pitfalls

- A vague description ("helpful tips") means the skill is never opened. Say what it does.
- A huge body. The linter flags bodies over about 24,000 characters, more than 60 files in
  `references/`, and text dense with issue or PR numbers. Split or trim.
- Copying text from documentation or other people's skills. Write it in your own words and link
  to the source instead.
- Secrets in a skill file. Declare needed variables under `required_environment_variables`; the
  user is asked for the value, which never lands in the skill.
- Editing a bundled skill: your edit is kept, but it stops receiving updates. Say so to the user.

## Verification

- `skill_view <name>` shows the new text and parses the frontmatter without errors, and the
  linter findings in the tool result are empty or understood.
- The description is 60 characters or fewer and starts with what the skill does.
- Every tool and command the procedure names exists in this setup.
