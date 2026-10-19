# SKILL.md frontmatter fields

The frontmatter is a YAML block between two `---` lines at the very top of `SKILL.md`. Only
`name` and `description` are required.

## Top-level fields

| Field | Type | Meaning |
|---|---|---|
| `name` | text | Skill id: lowercase letters, digits, `.`, `_`, `-`; starts with a letter or digit; at most 64 characters. Matches the folder name. |
| `description` | text | What the skill does. Up to 1,024 characters are accepted, but the prompt index shows only the first 60, so keep it to 60. |
| `version` | text | Your own version label, e.g. `1.2.0`. |
| `author` | text | Who wrote it. |
| `license` | text | Licence of the skill text, e.g. `MIT`. |
| `platforms` | list | Operating systems it works on: `macos`, `linux`, `windows`. Hidden elsewhere. |
| `environments` | list | Environment tags; the skill is hidden unless one of them is active. |
| `requires_apps` | list | Apps that must all be available, or the skill is hidden. |
| `required_environment_variables` | list | Variables the skill needs (see below). |
| `metadata` | map | Extra data; Indus Agent reads `metadata.indus`. |

## `required_environment_variables` entries

| Key | Meaning |
|---|---|
| `name` | Variable name, e.g. `WEATHER_API_KEY`. |
| `prompt` | The question shown when the user is asked for it. |
| `help` | Where to get the value. |
| `required_for` | What stops working without it. |

In the local CLI a missing variable is asked for when the skill is opened and stored as a secret;
on messaging surfaces the agent points the user at local setup instead.

## `metadata.indus` fields

| Field | Meaning |
|---|---|
| `category` | Group shown in the skills index. |
| `tags` | Free keywords for search and listing. |
| `requires_toolsets` / `requires_tools` | Hide the skill unless these are available. |
| `fallback_for_toolsets` / `fallback_for_tools` | Hide the skill when these are available (it is a fallback). |
| `session_platforms` | Limit the skill to these surfaces (for example `cli`). |
| `config` | Settings the skill reads: a list of `{key, description, default, prompt}`; values are passed in a "skill config" block when it runs. |
| `blueprint` | A suggested scheduled job built from the skill. |

## Folder layout

```
<category>/<skill-name>/
  SKILL.md
  references/   longer notes the body points to
  templates/    starting files to copy
  scripts/      helper programs
  assets/       anything else
```

Support files are limited to 1 MiB each, and `SKILL.md` to 100,000 characters.

## Example

```yaml
---
name: release-notes
description: Draft release notes from merged pull requests
version: 1.0.0
author: Docs Team
license: MIT
metadata:
  indus:
    category: writing
    tags: [release, changelog]
    requires_toolsets: [terminal]
---
```
