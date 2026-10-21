# Models and keys

Indus Agent talks to one main model at a time, through a provider. The
[provider list](../reference/providers.md) shows the ones it knows without setup; any other
OpenAI-compatible endpoint can be added.

## Choosing

- `indus model` lists providers and their models and saves your choice (`model.provider`,
  `model.default`).
- `/model <name>` in a chat switches for that conversation; `/model <name> --global` also saves it.
- `-m MODEL --provider NAME` on the command line applies to one run.

With no provider set, Indus Agent picks one whose key it finds.

## Keys

<!-- test -->
```console
$ indus auth list
No credentials yet. Add one with `indus auth add PROVIDER`.
```

`indus auth add <provider>` stores a key (asked for, hidden) or signs in, for providers that offer a
browser or device-code sign-in (`--type oauth`). Keys go to the operating system's keychain when
there is one (`security.secret_store`: `auto`, `keychain` or `file`). A key in the environment or in
the profile's `.env` file under the provider's usual name (for example `OPENAI_API_KEY`) works too;
[Environment variables](../reference/environment-variables.md#model-provider-keys) lists the names.

A provider can hold several credentials, a *pool*: `indus auth list` shows them, `indus auth
priority` orders them, and `credential_pool_strategies.<provider>` decides how the next one is picked
(`fill_first`, `round_robin`, `least_used`, `random`). A credential that keeps failing (an exhausted
quota, a revoked key) is benched for a while; `indus auth reset` puts it back.

## Local models

Ollama, LM Studio, vLLM and llama.cpp servers on their usual ports are known without a key:

```bash
indus config set model.provider ollama
indus config set model.default llama3.2
```

## Your own endpoint

Name a custom OpenAI-compatible provider under `providers:` in `config.yaml`:

```yaml
providers:
  lab:
    base_url: https://llm.lab.example/v1
    key_env: LAB_API_KEY        # the variable (or keychain entry) holding its key
    models: [lab-large, lab-small]
model:
  provider: lab
  default: lab-large
```

Other keys an entry may carry include `api_mode`, `context_length`, `aliases`, `display_name` and
`request_timeout_seconds`.

## When the model fails

`fallback_providers` is a list of routes tried in order when the main one fails (an outage, a rate
limit that will not reset soon, a refused key): `indus fallback add` adds one interactively, `indus
fallback list` shows the chain.

Smaller jobs (titling a conversation, compressing old turns, judging an approval in smart mode,
looking at images) use the main model unless you give them their own under `auxiliary.<task>`, for
example a cheaper model for titles:

```bash
indus config set auxiliary.title.model gpt-4o-mini
```

## Reasoning effort

`/reasoning <level>` (none, minimal, low, medium, high, xhigh, max, ultra) asks the model for more
or less thinking where the provider supports it; `/reasoning show` and `hide` control whether the
reasoning is displayed.
