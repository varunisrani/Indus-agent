Plugin folders for the P13 plugin tests. Tests copy them into a temporary profile home; none is
ever installed for real. Each folder's `plugin.yaml` / `plugin.json` describes it.

- `good`: registers one of each kind (tool, hook, command, skill, provider, prompt section,
  event listener).
- `hanging`: `register` waits on the `hold` setting (an event the test releases).
- `raising`: registers a hook, then raises.
- `shadowing`: replaces the built-in tool `read_file` (needs the operator opt-in).
- `capabilities`: portable `plugin.json` manifest that declares capabilities.
