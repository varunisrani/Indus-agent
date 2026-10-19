# Trimmed OpenAI schema pieces (P15-30)

`openai_trimmed.json` holds the component schemas the API server tests validate against, and
every schema they reference, extracted from OpenAI's public OpenAPI document:

- repository: `https://github.com/openai/openai-openapi` (MIT, © OpenAI)
- commit: `33248a638234a53846ed3ab4b2a7c77069bc1030` (2026-10-19), file `openapi.json`
  (OpenAPI 3.1.0, API version 2.3.0)
- roots: `CreateChatCompletionStreamResponse`, `CreateChatCompletionResponse`,
  `ListModelsResponse`, `Response`, `ResponseCreatedEvent`, `ResponseInProgressEvent`,
  `ResponseOutputItemAddedEvent`, `ResponseContentPartAddedEvent`, `ResponseTextDeltaEvent`,
  `ResponseTextDoneEvent`, `ResponseContentPartDoneEvent`, `ResponseOutputItemDoneEvent`,
  `ResponseCompletedEvent`, `ResponseFailedEvent`, `ResponseIncompleteEvent`
- changes: `$ref`s rewritten from `#/components/schemas/X` to `#/$defs/X`; `description`,
  `title`, `example(s)` and vendor `x-*` keys dropped; `enum` lists of OpenAI model ids removed
  (the field stays a string: our server names its own model). Nothing else is edited.
