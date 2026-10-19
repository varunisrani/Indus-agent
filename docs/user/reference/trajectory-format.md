# Trajectory format

Indus Agent writes training samples ("trajectories") as JSON Lines: one JSON object per line, each
holding one conversation. Every writer uses the same converter
(`indus.research.trajectory.convert`), so a sample looks the same whether it came from
`indus research batch`, from the interactive saver (`indus chat --save-trajectories`) or from the
single-tool coding runner (`indus research swe`).

The turn shape and the tags follow an open function-calling convention that several open-weight
model families use. The wording of the system turn is our own; only the tags are part of the
convention. We do not promise byte-for-byte equality with any other tool's output.

## A batch row

`indus research batch` appends one row per finished prompt to `batch_<N>.jsonl` in the run
folder, then merges all of them into `trajectories.jsonl`:

| Field | Meaning |
|---|---|
| `prompt_index` | the prompt's position among the dataset's usable rows |
| `conversations` | the turns (below) |
| `metadata` | `batch`, `timestamp` (UTC), `model` (the model that answered), `distribution`, `toolsets` (the toolsets sampled for this prompt) |
| `completed` | the run ended with a final answer |
| `partial` | the run stopped early (turn limit, budget, repeated invalid tool names, a guardrail) but the sample is still usable |
| `api_calls` | model requests the run made |
| `tool_stats` | per tool: `count`, `success`, `failure`; every tool the agent had is listed, unused ones with zeros, so every row has the same columns |
| `tool_error_counts` | per tool: the failure count (same columns) |
| `reasoning` | `total_assistant_turns`, `turns_with_reasoning`, `turns_without_reasoning` |

A prompt dropped on purpose leaves a tombstone instead, `{"prompt_index", "discarded",
"prompt"}` (today the only reason is `no_reasoning`: no assistant turn carried any reasoning).
Tombstones let a resumed run skip the prompt; they never reach `trajectories.jsonl`. Rows whose
`tool_stats` name a tool the agent did not have (the model invented one) are left out of
`trajectories.jsonl` as corrupt.

A result is counted as a failure only when it is a JSON object with a non-null `error` (or a
nested `content` object with one), or `success: false`, or plain text that is empty or starts
with `error:`. A command that exits non-zero is not a failure.

## A saved chat turn

With `indus chat --save-trajectories` (or `research.save_trajectories: true` in the config) every
finished turn appends the conversation so far to `trajectory_samples.jsonl` in the working
directory, or to `failed_trajectories.jsonl` when the turn did not complete. A line is
`{"conversations", "timestamp", "model", "completed"}` (`timestamp` in UTC). Every value is passed
through the secret redactor before it is written, so keys and tokens are masked on disk. Writers
lock the whole file while they append, so several chats, gateway sessions or batch workers can
share one file.

## A coding-runner line

`indus research swe` appends one line per task, `{"conversations", "completed", "api_calls",
"metadata": {"model", "env", "timestamp"}}`; a task that could not run leaves
`{"conversations": [], "completed": false, "api_calls": 0, "error", "metadata"}`. The only tool
is `terminal`, and each result's `content` is `{"content": {"output", "exit_code", "error"}}`.

## Turns

`conversations` is a list of `{"from": ..., "value": ...}` objects, `from` being one of `system`,
`human`, `gpt` or `tool`. Every value has its trailing whitespace removed.

1. **`system`** (always first): our instructions, with the tools the run offered as a compact JSON
   list of `{name, description, parameters}` between `<tools>` and `</tools>`, and how to call
   them (`<tool_call>`) and read results (`<tool_response>`).
2. **`human`** (always second): the dataset prompt. Nothing that came before it in the stored
   conversation is repeated. Prefill messages are added to model requests only, never stored, so
   they never appear; neither does an ephemeral system prompt.
3. **`gpt`**: one per assistant message. It always starts with a think block,
   `<think>\n…\n</think>\n`, holding the model's reasoning; when the model gave none, the block is
   empty (`<think>\n</think>\n`). Then comes the message text, then one block per tool call:
   `<tool_call>\n{"name":…,"arguments":{…}}\n</tool_call>`. Arguments are an object; arguments
   the model sent broken are written as `{}` (the run counts them as `unparseable_arguments` in
   `statistics.json`).
4. **`tool`**: all results that answered one assistant message, as `<tool_response>` blocks joined
   by newlines. Each holds `{"tool_call_id", "name", "content"}`. The name is taken from the call
   with the same id (results can arrive in any order); only when ids are missing is it taken by
   position, and a result that matches no call is named `unknown`. Content that is a JSON object
   or array is embedded as JSON; anything else as text. Results that carried images keep their
   text only: each image becomes `[image]`, and a result with a summary keeps the summary. No
   image data is ever written.
5. **`human`** again for any later user message.

## Example

Apart from the system turn, shortened here, the turns below are exactly what the converter
produces for the test case `tests/fixtures/research/trajectories/reordered_by_id.json` (a test
checks this).

<!-- example: reordered_by_id -->
```json
{
  "conversations": [
    {
      "from": "system",
      "value": "<our preamble>\n<tools>\n[...]\n</tools>\n<how to call and read tools>"
    },
    {
      "from": "human",
      "value": "How big is notes.txt, and what is in it?"
    },
    {
      "from": "gpt",
      "value": "<think>\nTwo things to check.\n</think>\nI will look at the file and its size.\n<tool_call>\n{\"name\":\"read_file\",\"arguments\":{\"path\":\"notes.txt\"}}\n</tool_call>\n<tool_call>\n{\"name\":\"terminal\",\"arguments\":{\"command\":\"wc -c notes.txt\"}}\n</tool_call>"
    },
    {
      "from": "tool",
      "value": "<tool_response>\n{\"tool_call_id\":\"call_size\",\"name\":\"terminal\",\"content\":{\"output\":\"42 notes.txt\",\"exit_code\":0}}\n</tool_response>\n<tool_response>\n{\"tool_call_id\":\"call_read\",\"name\":\"read_file\",\"content\":\"ship on Friday\"}\n</tool_response>"
    },
    {
      "from": "gpt",
      "value": "<think>\n</think>\nnotes.txt is 42 bytes and says: ship on Friday."
    }
  ]
}
```

## Reading samples

Any JSON Lines reader works. With the Hugging Face `datasets` library:
`load_dataset("json", data_files="trajectories.jsonl")`; the fixed stat columns keep the schema
the same across rows.
