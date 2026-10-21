# Quickstart

This page takes you from a fresh install to a first useful conversation in a few minutes.

## 1. Pick a model

```bash
indus setup
```

`indus setup` asks, one part at a time, for the model provider and its key, where commands should
run (this machine, a Docker container, a remote host over SSH, or a cloud sandbox), which tools to
offer and a few agent defaults. Each part can be run again on its own, for example
`indus setup model`. Keys are stored in your operating system's keychain when there is one.

Already have a key in your environment, such as `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`? Indus Agent
finds it; `indus model` then lets you choose the provider and model from a list. Local servers
(Ollama, LM Studio, vLLM, llama.cpp) work without a key: see [Models and keys](../user-guide/models.md).

## 2. Chat

```bash
indus
```

Type what you want done and press Enter. The reply streams in as it is written. When the agent
wants to run a command that could change or delete something, it stops and asks; answer with the
number of a choice (allow once, allow for this session, always allow, deny). Type `/help` for the
commands you can use inside a chat and `/quit` (or Ctrl+D on an empty line) to leave.

Some first things to try:

- "What is in this folder, and how would I run the tests?"
- "Create a script that prints the ten largest files under here, then run it."
- "Remember that I prefer pytest over unittest."

## 3. Come back later

Conversations are saved. `indus -c` continues the newest one (or the one with the name you give
it), `indus -r` resumes any by id or title, and `indus sessions list` shows what is there.

## 4. One question from a script

`-z` answers one prompt and prints only the final reply, which makes Indus Agent easy to use from
shell scripts:

<!-- test script=hello -->
```console
$ indus -z "Say hello"
hello
```

Risky commands are refused in one-shot runs unless you allow them
(`approvals.single_query_mode`), because nobody is there to answer a prompt.

## Next steps

- Use the [full-screen terminal UI](../user-guide/tui.md), the [desktop app](../user-guide/desktop.md)
  or the [dashboard](../user-guide/dashboard.md).
- Connect a chat app: [Messaging](../messaging/index.md).
- Decide what the agent may do on its own: [Approvals and security](../user-guide/security.md).
