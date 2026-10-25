// English strings of the provider onboarding overlay (P20-33), spread into `src/i18n/en.ts`.

export const ONBOARDING_STRINGS = {
  "onboarding.title": "Choose how Indus reaches a model",
  "onboarding.lead":
    "Indus needs one model provider before it can answer. You can change it later.",
  "onboarding.checking": "Asking Indus which providers are set up",
  "onboarding.later": "Choose later",
  "onboarding.back": "Back",
  "onboarding.cancel": "Cancel",
  "onboarding.notConnected": "Indus is not connected; try again in a moment.",

  "onboarding.device.title": "Sign in with an account",
  "onboarding.device.starting": "Asking {provider} for a sign-in code",
  "onboarding.device.instructions":
    "Open the sign-in page of {provider} and enter this code. This window updates by itself.",
  "onboarding.device.code": "Sign-in code",
  "onboarding.device.copy": "Copy the code",
  "onboarding.device.copied": "Copied",
  "onboarding.device.open": "Open the sign-in page",
  "onboarding.device.waiting": "Waiting for you to approve the sign-in",

  "onboarding.other.title": "Other ways",
  "onboarding.key.option": "Paste an API key",
  "onboarding.key.optionHint": "For any provider that gives you a key.",
  "onboarding.key.provider": "Provider",
  "onboarding.key.generic": "Another key (type its variable name)",
  "onboarding.key.name": "Variable name",
  "onboarding.key.value": "API key",
  "onboarding.key.save": "Save the key",
  "onboarding.local.option": "Use a model running on this computer",
  "onboarding.local.optionHint": "A local runtime with an OpenAI-compatible server.",
  "onboarding.local.body": "The address of the local server. Indus asks it which models it has.",
  "onboarding.custom.option": "Use a custom OpenAI-compatible endpoint",
  "onboarding.custom.optionHint": "Any server that speaks the OpenAI API.",
  "onboarding.custom.body": "The endpoint's base URL and, if it needs one, its key.",
  "onboarding.endpoint.url": "Base URL",
  "onboarding.endpoint.key": "API key (optional)",
  "onboarding.endpoint.test": "Test and continue",

  "onboarding.model.body": "Pick the model Indus uses by default. You can switch any time.",
  "onboarding.model.label": "Model",
  "onboarding.model.save": "Start chatting",

  "onboarding.error.default": "The sign-in did not finish.",
  "onboarding.error.other": "Choose another way",
  "onboarding.error.useKey": "Paste an API key instead",
  "onboarding.error.retry": "Try again",

  // W52-m: notice box, provider rows, disclosure, key screen tiles
  "onboarding.notice.firstRun":
    "No model provider is connected yet. Pick one below; you can also do this later from Settings › Providers.",
  "onboarding.notice.providerError":
    "The last reply failed at the provider or with its credentials. Connect it again or choose another provider.",
  "onboarding.device.hint": "Sign in in your browser; Indus connects by itself.",
  "onboarding.more": "Other providers",
  "onboarding.less": "Collapse",
  "onboarding.key.defaultDesc": "Paste a key from {provider}.",
  "onboarding.key.genericTile": "Another key",
  "onboarding.key.genericShort": "any variable name",
  "onboarding.key.genericDesc": "Store any key under the environment variable name it needs.",
  "onboarding.key.placeholder": "Paste API key",
  "onboarding.key.configured": "Already set up",
  "onboarding.tile.local": "Local model",
  "onboarding.tile.localShort": "on this computer",
  "onboarding.tile.custom": "Custom endpoint",
  "onboarding.tile.customShort": "OpenAI-compatible",
  "onboarding.model.caption": "Default model",
} as const satisfies Record<string, string>;
